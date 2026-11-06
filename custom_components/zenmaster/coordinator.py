from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import Event, HomeAssistant, callback as ha_callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .config import FIELDS, Config
from .solver.baseload import BaseLoad
from .solver.battery import DistributionEntry
from .solver.boiler import BoilerElectric, BoilerHeatPump
from .solver.consumers.appliance import Appliance
from .solver.consumers.ev import ElectricVehicle
from .solver.dynamicprice import DynamicPrice
from .solver.forecast import Forecast, SolarPanel
from .solver.heating import HeatingHeatPump, PoolHeatPump
from .solver.solver import LpSolver
from .zendure.driver import ZendureDriver
from .const import DOMAIN, PANEL_TITLE, Phase, watt_factor
from .entity import EntityDevice, snakecase, unique_name
from .device import DistributionDevice
from .distribution import Distribution

MANAGER = "energy_manager"  # the subentry that carries the distribution device and the lists the panel edits

# subentry_type -> consumer class. The type string is what HA stores on the subentry and what the panel and the flows key on.
SUBENTRY_TYPES = {
    "appliance": Appliance,
    "electric_vehicle": ElectricVehicle,
    "heating_heat_pump": HeatingHeatPump,
    "boiler_heat_pump": BoilerHeatPump,
    "boiler_electric": BoilerElectric,
    "pool_heat_pump": PoolHeatPump,
}

# per integration how its grid total is read: ("enum", power fragment, direction fragment, the state that means export),
# or a list of (name fragment, sign) for the total and the phases; absent means the default produced/consumed name rules
P1_PROFILES: dict[str, Any] = {
    "solaredge": ("enum", "grid_power", "grid_flow_direction", "export"),
    "enphase_envoy": [("net_consumption", 1)],
    "sma": [("metering_power_absorbed", 1), ("metering_power_supplied", -1), ("feed", -1), ("draw", 1)],
}
PHASE_FRAGS = ((), ("phase_1", "phase1", "_l1", "phase_a"), ("phase_2", "phase2", "_l2", "phase_b"), ("phase_3", "phase3", "_l3", "phase_c"))  # indexed on Phase: the name fragments that put a P1 entity on that phase
NOT_METER = ("demand", "peak")  # P1 entities in W or kW that are no reading of the grid: the capacity tariff's monthly peak and quarter average (HomeWizard, DSMR in Belgium)


def phase_of(eid: str) -> Phase:
    # the phase a P1 entity name points at; Phase.all is a grid total
    return next((p for p in (Phase.l1, Phase.l2, Phase.l3) if any(f in eid for f in PHASE_FRAGS[p])), Phase.all)


def title_taken(entry, title: str, skip: str = "") -> bool:
    # names must stay unique as slugs: HA derives the entity ids from them
    slug = snakecase(title)
    return any(snakecase(sub.title) == slug for sid, sub in entry.subentries.items() if sid != skip)


def field_values(obj, fields) -> dict:
    # the object's persisted fields in the shape the forms store them: a select as its option string, no Nones
    out = {}
    for field in fields:
        if field.cast is None or field.transient or field.is_key or (value := getattr(obj, field.id, None)) is None:
            continue
        out[field.id] = str(int(value)) if "select" in field.selector and isinstance(value, int) else value
    return out


class Coordinator:
    # The one coordinator: builds the drivers + distribution from the Store config; offline (tests/tools) hass is None.

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id if entry else ""
        self.devices: list[EntityDevice] = []
        # the manager lives in its own subentry, so its device and the solar groups sit on one card; created once for entries that predate it
        sub = next((s for s in entry.subentries.values() if s.subentry_type == MANAGER), None) if entry else None
        if entry is not None and sub is None:
            sub = ConfigSubentry(data=MappingProxyType({}), subentry_type=MANAGER, title="EnergyManager", unique_id=MANAGER)
            hass.config_entries.async_add_subentry(entry, sub)
        self.manager_id = sub.subentry_id if sub else ""
        self.distribution = Distribution(hass, self.entry_id, self.manager_id or None)
        self.distribution.id = "distribution"  # the key the card and the payload address it by
        self.distribution.on_save = self._store_save
        self.distribution.on_capacity = self.capacity_changed
        self.config = Config(store=Store(hass, version=1, key=f"{DOMAIN}.devices", atomic_writes=True))
        self.zendure = ZendureDriver(hass, self.entry_id, self.devices, self.distribution)
        self.zendure.on_discovered = self.discovered
        self.zendure.on_zeroconf = self.zeroconf
        self.battery = DistributionEntry(self.distribution)  # the fleet in the LP; owns the plan-driven mode and the savings
        self.zeroconf_ip: dict[str, str] = {}  # serial -> ip from mDNS for devices that are not built yet
        self.prices = DynamicPrice()
        self.prices.on_save = self._store_save  # the price entry persists a fresh token itself
        self.baseload = BaseLoad()
        self.forecast = Forecast()
        self.consumers: list = []
        self.distribution.consumers = self.consumers  # the same list, so the phase detection sees every consumer
        self.solver = LpSolver(self.track, self.state)
        self._solve_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._unsubs: list = []  # registrations for the life of the coordinator: the timers
        self._p1_unsubs: list = []  # the P1 meter's registrations; they go when another meter is chosen
        self._track_unsubs: list = []  # registrations the solver entries make; they go with every rebuild
        self._settle = None  # the pending rebuild after a setting entity changed
        self._resolve = None  # the pending solve after the fleet capacity changed
        self.p1_entities: list[list[tuple[str, float]]] = [[], [], [], []]  # indexed on Phase: the meter entities of the total (0) and each phase, one net or a consumed/produced pair, as (entity_id, signed watt factor)
        self.p1_direction = ("", "")  # the direction enum entity and its export state, for a meter that reports the total as a magnitude (SolarEdge)
        self.p1_slots: dict = {}  # per slot the (entity_id, sign) pairs, derived from p1_entities; baseload reads this

    async def async_setup(self) -> None:
        # the fleet device, then the vendor devices from the entry's subentries
        self.devices.clear()
        self.devices.append(self.distribution)
        await self.config.load()
        self.distribution.device_load(self.config.section("devices", self.distribution.device_id))
        self.battery.load(self.config.section("devices", "solver"))
        self.config.deserialize(self.distribution, FIELDS[Distribution])
        self.config.deserialize(self.zendure, FIELDS[ZendureDriver])
        self.sync_devices()
        if self.entry is not None:
            self.entry.async_on_unload(self.entry.add_update_listener(self._entry_updated))
        self.config.deserialize(self.prices, self.prices.CONFIG)
        await self.prices.restore(self.hass)  # the Zonneplan token, kept out of the config form
        self.prices.hass = self.hass
        self.prices.country = self.hass.config.country or "NL"  # picks the tariffs and the fields the price card shows
        self.forecast = Forecast(hass=self.hass)

        # consumers and solar panels live in the config entry's subentries; HA's own dialogs change those too
        await self.sync_subentries()
        await self.zendure.connect()

        # periodic dump: without it the kWh counters only reach the Store when a user happens to move a knob
        self._unsubs.append(async_track_time_interval(self.hass, self.timer_elapsed, timedelta(minutes=15)))

        # solver wiring: the structure, then the blackboard, the quarter tick and one solve
        self.solver.hass = self.hass
        self.rebuild_solver()
        if self.distribution.p1_device:  # every power entity on the P1 device feeds total/L1/L2/L3 by name
            self._track_p1(self.distribution.p1_device)
        self._unsubs.append(async_track_time_change(self.hass, self._tick, minute=[0, 15, 30, 45], second=0))
        await self._tick()

    async def _entry_updated(self, _hass, _entry) -> None:  # noqa: ANN001
        # HA hands the listener a coroutine to schedule; discovery and the subentry dialogs land here
        await self.resync(self.sync_devices())

    def sync_devices(self) -> bool:
        # the Zendure devices mirror the entry's subentries: build what is new, drop what is gone. A subentry's data is the driver's business.
        # True when the set changed: every battery is its own unit in the LP, so the structure has to follow
        if self.entry is None:
            return False
        changed = False
        built = {d.subentry_id: d for d in self.devices if d.subentry_id}
        for sid, sub in self.entry.subentries.items():
            if sid in built or sub.subentry_type != ZendureDriver.id:
                continue
            if (device := self.zendure.init_device(sid, sub.title, dict(sub.data))) is None:
                continue
            changed = True
            if isinstance(device, DistributionDevice):
                device.on_phase = lambda: self.setting_changed(None)  # its phase is fixed in the LP structure
            device.on_config_changed = self.persist_device
            device.device_load(self.config.section("devices", device.device_id))
            if (ip := self.zeroconf_ip.pop(getattr(device, "sn", ""), None) or self.zeroconf_ip.pop(getattr(device, "device_key", ""), None)) and not getattr(device, "ip", None):
                device.ip = ip
                self.persist_device(device)
        for sid, device in built.items():
            if sid not in self.entry.subentries:
                self.zendure.remove_device(device)  # HA already cleared its registry device and entities along with the subentry
                changed = True
        return changed

    def discovered(self, data: dict[str, Any]) -> None:
        # the driver found a device: it becomes a subentry with a name that never changes again; the update listener builds it from there
        sn = data["sn"]
        if self.entry is None or any(s.data.get("sn") == sn for s in self.entry.subentries.values()):
            return
        family = data.pop("family")
        taken = {s.title for s in self.entry.subentries.values()} | {d.name for d in self.devices}
        name = unique_name(family, sn, taken)
        if (ip := self.zeroconf_ip.pop(sn, None)) and not data.get("ip"):
            data["ip"] = ip
        self.hass.config_entries.async_add_subentry(
            self.entry, ConfigSubentry(data=MappingProxyType(data), subentry_type=ZendureDriver.id, title=name, unique_id=f"{ZendureDriver.id}_{sn}")
        )

    def persist_device(self, device: EntityDevice) -> None:
        # ip, blemac and the pack names back into the device's subentry; a no-op when nothing changed
        if self.entry is None or (sub := self.entry.subentries.get(device.subentry_id or "")) is None:
            return
        data = {**sub.data, **device.subentry_data()}
        if dict(sub.data) != data:
            self.hass.config_entries.async_update_subentry(self.entry, sub, data=data)

    def zeroconf(self, find: dict[str, Any]) -> None:
        # the driver's mDNS find: the ip of a device we know, or kept until the device shows up through its own transport
        for d in self.zendure.devices.values():
            if d.sn == find["sn"] or (find["device_id"] and d.device_key == find["device_id"]):
                if (reader := find.get("reader_sn")) and getattr(d, "reader_sn", "") != reader:
                    d.reader_sn = reader
                if getattr(d, "ip", None) != find["ip"]:
                    d.ip = find["ip"]
                self.persist_device(d)  # a no-op when nothing changed
                return
        self.zeroconf_ip[find["sn"] or find["device_id"]] = find["ip"]  # a find without a serial is only matchable on its MQTT address

    def manager_list(self, key: str) -> list[dict]:
        # the manager subentry carries the lists the panel edits: a solar group has no device of its own to hang them on
        sub = self.entry.subentries.get(self.manager_id) if self.entry and self.manager_id else None
        return list({str(row["id"]): dict(row) for row in sub.data.get(key, [])}.values()) if sub else []

    def manager_save(self, key: str, rows: list[dict]) -> None:
        if self.entry is not None and (sub := self.entry.subentries.get(self.manager_id)) is not None:
            self.hass.config_entries.async_update_subentry(self.entry, sub, data={**sub.data, key: rows})

    async def sync_subentries(self) -> bool:
        # Consumers and solar panels mirror the entry's subentries: create what is new, apply what changed, drop what is
        # gone. Returns whether the solver structure changed. Idempotent, so the update listener and the websocket
        # handlers can both call it; the lock keeps two callers from creating the same consumer twice.
        if self.entry is None:
            return False

        async with self._sync_lock:
            seen: set[str] = set()
            for sid in sorted(self.entry.subentries):
                # HA's own rename dialog checks nothing, so a duplicate name is repaired here with a suffix
                sub = self.entry.subentries[sid]
                title, n = sub.title, 1
                while snakecase(title) in seen:
                    n += 1
                    title = f"{sub.title} {n}"
                if title != sub.title:
                    self.hass.config_entries.async_update_subentry(self.entry, sub, title=title)
                seen.add(snakecase(title))
            # the consumers follow their subentries, then the solar panels follow the manager list
            changed = await self.sync_consumers()
            panels = [
                SolarPanel(name=str(row.get("name") or ""), power_entity=str(row.get("power_entity") or ""), derate_entity=str(row.get("derate_entity") or ""))
                for row in self.manager_list("solar_panels")
            ]
            if panels != self.forecast.solar_panels:
                self.forecast.solar_panels = panels
                changed = True
            self.distribution.solar_panels = [(p.power_entity, p.derate_entity) for p in panels]
            return changed

    async def sync_consumers(self) -> bool:
        # create the consumer of a new subentry, apply what changed in an existing one, drop the ones whose subentry is gone
        changed = False
        existing = {c.id: c for c in self.consumers}
        for sid, sub in self.entry.subentries.items():
            if (cls := SUBENTRY_TYPES.get(sub.subentry_type)) is None:
                continue
            if (consumer := existing.pop(sid, None)) is None:
                consumer = cls()
                consumer.id, consumer.name, consumer.hass, consumer.entry = sid, sub.title, self.hass, self.entry
                consumer.on_setting, consumer.on_action = self.setting_changed, self.consumer_command
                for field in cls.CONFIG:
                    if field.cast is not None and not field.transient and not field.is_key and field.id in sub.data:
                        setattr(consumer, field.id, field.cast(sub.data[field.id]))
                dr.async_get(self.hass).async_get_or_create(
                    config_entry_id=self.entry_id,
                    config_subentry_id=sid,
                    identifiers={(DOMAIN, sid)},
                    name=sub.title,
                    manufacturer=PANEL_TITLE,
                    model=cls.__name__,
                    via_device=(DOMAIN, PANEL_TITLE),  # the ZenMaster device's identifier
                )
                EntityDevice.__init__(consumer, self.hass, sid, sub.title, subentry_id=sid)  # its own device: status, buttons and settings
                consumer.device_load(self.config.section("devices", consumer.device_id))  # the settings as they stood before the restart
                await consumer.restore(self.hass)
                self.persist_consumer(consumer)  # the restored state back into the subentry, so both sides agree from the start
                self.consumers.append(consumer)
                changed = True
                continue
            # hidden fields are state the consumer owns (armed, legionella_at): only the links follow the subentry
            for field in cls.CONFIG:
                if field.cast is None or field.transient or field.is_key or field.hidden or field.id not in sub.data:
                    continue
                if getattr(consumer, field.id, None) != (value := field.cast(sub.data[field.id])):
                    setattr(consumer, field.id, value)
                    changed = True
            if consumer.name != sub.title:
                consumer.name = sub.title
                dr.async_get(self.hass).async_get_or_create(config_entry_id=self.entry_id, config_subentry_id=sid, identifiers={(DOMAIN, sid)}, name=sub.title)
        for gone in existing.values():
            self.consumers.remove(gone)  # HA already cleared its device and entities along with the subentry
            changed = True
        return changed

    async def resync(self, devices_changed: bool = False) -> None:
        # after any subentry change, from HA's dialogs or our own handlers: rebuild the LP when the structure moved,
        # and let the panel re-read its config either way
        if await self.sync_subentries() or devices_changed:
            self.rebuild_solver()
            await self._tick()
        async_dispatcher_send(self.hass, f"{DOMAIN}_plan")

    def persist_consumer(self, consumer) -> None:
        # the links plus the state that must survive a restart (armed, legionella_at) go into the consumer's
        # subentry; a no-op when nothing changed. The settings are entities, HA keeps those.
        if self.entry is None or (sub := self.entry.subentries.get(consumer.id)) is None:
            return
        self.hass.config_entries.async_update_subentry(self.entry, sub, data={**sub.data, **field_values(consumer, type(consumer).CONFIG)})

    def setting_changed(self, _consumer) -> None:
        # a setting entity changed: one rebuild and one solve for everything that changes within the same second
        if self._settle is None:
            self._settle = async_call_later(self.hass, 1, self._settled)

    def capacity_changed(self) -> None:
        # the batteries in the regulation changed: one solve for every change within ten seconds, so a start with all devices reporting solves once
        if self._resolve is None:
            self._resolve = async_call_later(self.hass, 10, self._resolved)

    async def _resolved(self, _now) -> None:
        self._resolve = None
        await self._tick()

    async def _settled(self, _now) -> None:
        self._settle = None
        self.rebuild_solver()
        await self._tick()
        await self._store_save()  # the new setting into the Store, so a restart finds it back

    async def consumer_command(self, consumer, action: str) -> None:
        # a row action, from the panel or from the button entity: the consumer acts, its state is saved, the plan follows
        await consumer.command(self.hass, action)
        self.persist_consumer(consumer)
        self.hass.async_create_task(self._tick())  # a row action changes no structure, so a fresh solve is enough

    def rebuild_solver(self) -> None:
        # Columns and rows are built once from the config, so a changed setting only lands after this runs.
        # init() clears the blackboard, so everything that lives there goes back right after. The entries register
        # their entity tracking in entry_config, so the registrations of the previous structure go first.
        self.distribution.solver = self.prices.provider != "none"  # a dynamic price provider is what the solver needs
        for unsub in self._track_unsubs:
            unsub()
        self._track_unsubs.clear()
        # the history over the rebuild, at the first build the one the Store kept over the restart
        history = self.solver.history() or self.config.section("devices", "solver_history")
        self.solver.init([self.prices, self.forecast, self.baseload, self.battery, *[c for c in self.consumers if c.manage]])
        self.solver.restore(history)
        self.solver.globals["request_solve"] = lambda: self.hass.async_create_task(self._tick())
        dev_solar = {getattr(d.entities.get("solar"), "entity_id", None) for d in self.distribution.devices}
        self.solver.globals["solar_ac"] = [p.power_entity for p in self.forecast.solar_panels if p.power_entity and p.power_entity not in dev_solar]

        # the live solar of the panels: every reading lands as the sum of all panel power entities on the distribution
        solar: dict[str, float] = {}

        def solar_sum(eid: str, watts: float) -> None:
            solar[eid] = watts
            self.distribution.solar = int(sum(solar.values()))

        for eid in {power_entity for power_entity, _ in self.distribution.solar_panels if power_entity}:
            self.track(eid, lambda w, e=eid: solar_sum(e, w))
        self.solver.globals["batteries"] = self.distribution.devices
        self.solver.globals["p1"] = self.p1_slots

    async def _tick(self, now=None) -> None:
        # the 15-min solve tick; on a successful solve, signal the panel to re-fetch the plan
        if not self.distribution.solver:  # the solver is off: no plan, the fleet runs on its own control
            return
        async with self._solve_lock:
            x = await self.solver.run(now or dt_util.now())
        if x is not None:
            async_dispatcher_send(self.hass, f"{DOMAIN}_plan")

    async def unload(self) -> None:
        await self._store_save()
        if self._settle is not None:
            self._settle()
        if self._resolve is not None:
            self._resolve()
        for unsub in (*self._unsubs, *self._p1_unsubs, *self._track_unsubs):
            unsub()
        self._unsubs = []
        self._p1_unsubs = []
        self._track_unsubs = []
        # stop the fleet and hand HEMS back, so no device keeps running on our last setpoint once we are gone
        for d in self.distribution.devices:
            d.power_off()
        for d in self.devices:
            d.unload()
        await self.zendure.unload()

    async def timer_elapsed(self, _now=None) -> None:
        await self._store_save()

    async def _store_save(self) -> None:
        # the devices key is replaced as a whole, so removed devices drop out; every other setting in the payload stays.
        # Consumers and solar panels are no longer in this Store: their subentries hold them.
        devices = {}
        for d in self.devices:
            d.device_save(devices.setdefault(d.device_id, {}))
        self.battery.save(devices.setdefault("solver", {}))
        devices["solver_history"] = self.solver.history()
        self.config.payload["devices"] = devices
        self.config.payload.pop("consumers", None)
        self.config.payload.pop("solar_panels", None)
        for c in self.consumers:
            c.device_save(devices.setdefault(c.device_id, {}))
            self.persist_consumer(c)
        await self.config.save()

    def track(self, entity_id: str, callback: Callable[[float], None], factor: float | None = None, phase: Phase = Phase.all, keep: bool = False) -> None:
        # the coordinator owns every entity-event registration; parses the state to float (x factor) so the caller only implements the value handler.
        # Without a factor the unit decides: kW becomes watts, and a current is converted with the voltage that follows from the phase.
        # keep: for the life of the coordinator (the P1 meter); otherwise until the next rebuild, like the entries that ask for it.
        if factor is None:
            factor = watt_factor(self.hass.states.get(entity_id), phase)

        @ha_callback
        def handler(event: Event) -> None:
            new = event.data.get("new_state")
            if new is None:
                return
            try:
                value = float(new.state) * factor
            except (ValueError, TypeError):
                return
            callback(value)

        if entity_id != "":
            (self._p1_unsubs if keep else self._track_unsubs).append(async_track_state_change_event(self.hass, [entity_id], handler))
        # the current value at once, so a restart does not leave the entry at 0; the P1 meter (keep) waits for its first real reading
        if entity_id != "" and not keep and (state := self.hass.states.get(entity_id)) is not None:
            handler(Event("state_changed", {"new_state": state}))

    def state(self, entity_id: str, callback: Callable[[str], None], keep: bool = False) -> None:
        # like track(), but hands the raw state string to the callback (for binary/enum entities)
        @ha_callback
        def handler(event: Event) -> None:
            new = event.data.get("new_state")
            if new is not None:
                callback(new.state)

        if entity_id != "":
            (self._p1_unsubs if keep else self._track_unsubs).append(async_track_state_change_event(self.hass, [entity_id], handler))

    def _track_p1(self, device_id: str) -> None:
        # the meter chosen before lets go first, so a new choice works without a restart
        for unsub in self._p1_unsubs:
            unsub()
        self._p1_unsubs.clear()
        # every power entity on the P1 device goes on its phase (0 = the grid total) with a signed watt factor, except the capacity tariff's peak and average; only the total entities trigger
        entries = er.async_entries_for_device(er.async_get(self.hass), device_id)
        states = getattr(self.hass, "states", None)  # absent offline (tests/tools), then the registry is all there is
        entities = []
        for e in entries:
            attrs = getattr(states.get(e.entity_id) if states else None, "attributes", None) or {}
            unit = attrs.get("unit_of_measurement") or e.unit_of_measurement  # the integration sets both at runtime, so the state comes first
            if not any(f in e.entity_id for f in NOT_METER) and ((attrs.get("device_class") or e.original_device_class or e.device_class) == "power" or unit in ("W", "kW", "kilowatt", "kilowatts")):
                entities.append((e.entity_id, 1000 if unit in ("kW", "kilowatt", "kilowatts") else 1))
        platform = entries[0].platform if entries else ""
        flip = -1 if platform == "huawei_solar" else 1  # Huawei signs the grid the other way: positive is export

        # the sign of a total: the profile names it, else produced/consumed in the name does, and a net wins over the pair
        totals = [eid for eid, _ in entities if phase_of(eid) == Phase.all]
        has_net = any("produc" not in eid and "consum" not in eid for eid in totals)
        profile = P1_PROFILES.get(platform)
        if profile and profile[0] == "enum":
            # SolarEdge: an unsigned magnitude, its sign comes live from a separate direction enum; without that enum the magnitude is useless
            self.p1_direction = (next((e.entity_id for e in entries if profile[2] in e.entity_id), ""), profile[3])
            profile = [(profile[1], 1 if self.p1_direction[0] else 0)]

        # every entity on its phase with its signed factor: the profile signs what it names, else a phase entity is consumed unless its name says produced
        self.p1_entities = [[], [], [], []]
        for eid, factor in entities:
            phase = phase_of(eid)
            if profile and (match := next((s for frag, s in profile if frag in eid), 0)):
                sign = match
            elif phase != Phase.all:
                sign = -1 if "produc" in eid else 1
            elif profile:
                sign = 0
            elif "produc" in eid:
                sign = 0 if has_net else -1
            else:
                sign = 0 if has_net and "consum" in eid else 1
            if sign:
                self.p1_entities[phase].append((eid, sign * flip * factor))
        self.distribution.p1_entities = self.p1_entities  # export_replay reads the total entities from it

        # only the total entities trigger; the phases are read along with them
        for eid, _ in self.p1_entities[Phase.all]:
            self.track(eid, self._p1_changed, keep=True)

        # the regulator never runs without a total: name the meter that gave none
        if not self.p1_entities[Phase.all]:
            persistent_notification.async_create(self.hass, f"P1 meter '{platform}' delivered no usable grid total", PANEL_TITLE, f"{DOMAIN}_p1")
        self.p1_slots = {"total" if p == Phase.all else p.name: list(self.p1_entities[p]) for p in Phase}  # each entity with its signed watt factor, so the base load reads a kW meter right
        self.solver.globals["p1"] = self.p1_slots

    def _p1_changed(self, _: float) -> None:
        # a grid total entity reported: read the meter, each value its one net entity or consumed and produced added through their signed factors, and hand total and phases to the regulator
        power = [0.0] * 4
        for phase in Phase:
            for eid, factor in self.p1_entities[phase]:
                if (state := self.hass.states.get(eid)) and state.state not in ("unknown", "unavailable"):
                    power[phase] += float(state.state) * factor
        # the total leaves slot 0, which stays 0 for the distribution; a meter that reports a magnitude (SolarEdge) gets its sign from the direction enum
        p1 = power[Phase.all]
        power[Phase.all] = 0.0
        direction, export = self.p1_direction
        if direction and (state := self.hass.states.get(direction)) and state.state == export:
            p1 = -p1
        self.hass.async_create_task(self.distribution._process_p1(int(p1), power, now=time.time()))
