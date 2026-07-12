from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback as ha_callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.dt as dt_util

from .config import Config, CONFIG_SCHEMA
from .const import CONF_APPTOKEN, CONF_MQTTPORT, CONF_MQTTPSW, CONF_MQTTSERVER, CONF_MQTTUSER, CONF_P1DEVICE, CONF_WIFIPSW, CONF_WIFISSID, DOMAIN, SchemaAction
from .device import EmDevice, PowerDevice
from .distribution import Distribution
from .solver.solver import LpSolver, SolverEntry
from .solver.forecast import Forecast, SolarPanel
from .solver.baseload import BaseLoad
from .solver.dynamicprice import DynamicPrice

_LOGGER = logging.getLogger(__name__)

_DEVICE_ENTITIES = ["soc", "solar", "home_input", "home_output", "temp", "available_kwh", "home_input_aggr", "home_output_aggr", "connection"]
_CONTROL_ENTITIES = [
    "optimize",
    "mode",
    "manualpower",
    "soc",
    "capacity_kwh",
    "available_kwh",
    "charge_power",
    "discharge_power",
    "solar_power",
    "house_load",
    "grid_import",
    "grid_export",
    "cost_today",
    "savings_today",
    "autarky",
]
_SMARTMETER_ENTITIES = [
    "totalPower",
    "phase1_power",
    "phase2_power",
    "phase3_power",
    "phase1_current",
    "phase2_current",
    "phase3_current",
    "phase1_voltage",
    "phase2_voltage",
    "phase3_voltage",
]


class Coordinator:
    # The one coordinator: builds drivers + solver entries from the Store config and runs the LP each quarter; offline (tests/tools) hass is None.

    def __init__(self, hass: HomeAssistant, config: dict, entry: ConfigEntry | None = None) -> None:
        self.hass = hass
        self.config = Config(hass.config.path(DOMAIN) + "/config.json")
        self._store = Store(hass, 1, DOMAIN)
        self.entry = entry
        self.devices: list[EmDevice] = []  # HA entity registration (driver fills named devices)
        self.power_devices: list[PowerDevice] = []  # HA entity registration (driver fills named devices)
        self.solver = LpSolver(self.track, self.state)
        self.baseload = BaseLoad()
        self.forecast = Forecast()
        self.prices = DynamicPrice()
        self.distribution = Distribution(hass, self.power_devices)
        self.drivers: list = []  # battery drivers, polymorphic collection; empty on a clean install (nothing loaded)
        self.consumers: list[SolverEntry] = []  # HA entity registration (driver fills named devices)
        self._unsub_tick = async_track_time_change(self.hass, self._tick, minute=[0, 15, 30, 45], second=0)
        self._unsubs: list = []
        self._solve_lock = asyncio.Lock()  # a requested solve queues neatly behind a running one
        self.solver.globals["request_solve"] = lambda: self.hass.async_create_task(self._tick(dt_util.now()))  # entries trigger a fresh solve after learning (e.g. calibration)

    async def async_setup(self) -> None:
        # unsubscribe all entities
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

        # Load the config payload from the HA Store (the coordinator owns the section <-> object mapping)
        self.config.payload = await self._store.async_load() or {}
        await self._migrate_entry_to_config()
        self.config.read(self.solver)
        self.config.read(self.baseload)
        for name, obj in self._sections().items():
            self.config.read(obj, name=name)
        self.config.readlist(self.consumers, self.config.payload.get("consumers") or [])
        self.forecast = Forecast(self.config.payload, self.hass)

        # the feeders (price, baseload, forecast, heating) need hass for the recorder/Store/HTTP background work
        self.solver.hass = self.hass
        self.prices.hass = self.hass
        for c in self.consumers:
            c.hass = self.hass

        # restore persisted params (calibration, profiles) into each entry before init() bakes the coefficients
        for e in self.consumers:
            await e.restore(self.hass)

        # Drivers: build from the persisted list (empty on a clean install -> nothing loaded, no MQTT).
        driver_items = {it.get("id"): it for it in (self.config.payload.get("drivers") or [])}
        self.config.readlist(self.drivers, self.config.payload.get("drivers") or [])  # finds-or-creates by is_key (id)
        for d in self.drivers:
            d.attach(self.hass, self.devices, self.power_devices, self.distribution.device_update, self._save_config)
            d.restore(driver_items.get(getattr(d, "id", None), {}))
        for d in self.drivers:
            await d.connect()
            await d.cloud_connect()

        # restore persisted entity states (mode, soc knobs) into the devices, then start each device task
        await self.distribution.load_state()
        for d in self.drivers:
            for dev in d.devices.values():
                await dev.load_state()
                dev.start_task()

        # Initialize the Solver + SolverEntries
        self.solver.init([self.prices, self.forecast, self.baseload, self.distribution, *self.consumers])

        # Add listeners for the solar power
        self.distribution.solar = []
        for p in self.forecast.solar_panels:
            idx = len(self.distribution.solar)
            self.distribution.solar.append(0.0)
            self.track(p.power_entity, lambda watts, idx=idx: self.distribution.solar.__setitem__(idx, watts))

        # P1 from the panel config (system.p1_device): every power entity feeds total/L1/L2/L3 by name
        if self.distribution.p1_device:
            self._track_p1(self.distribution.p1_device)

        # baseload reconstructs the true house load from P1 + AC roof PV + battery net (P1 alone is polluted by the battery)
        dev_solar = {getattr(getattr(d, "solar", None), "entity_id", None) for d in self.power_devices}
        self.solver.globals["solar_ac"] = [p.power_entity for p in self.forecast.solar_panels if p.power_entity and p.power_entity not in dev_solar]
        self.solver.globals["batteries"] = self.power_devices

        # solve once now so the panel has a plan without waiting for the next quarter tick
        await self._tick(dt_util.now())

    async def unload(self) -> None:
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        for d in self.drivers:
            for dev in d.devices.values():
                dev.stop_task()
            await d.disconnect()

    def _save_config(self) -> None:
        # thread-safe (driver calls this from the MQTT thread on device discovery): debounced save to the Store
        self.hass.loop.call_soon_threadsafe(self._store.async_delay_save, self.serialize, 10)

    async def _tick(self, now) -> None:
        # the 15-min solve tick; on a successful solve, signal the panel to re-fetch the plan
        async with self._solve_lock:
            x = await self.solver.run(now)
        if x is not None:
            async_dispatcher_send(self.hass, f"{DOMAIN}_plan")

    def track(self, entity_id: str, callback: Callable[[float], None], factor: float = 1) -> None:
        # the coordinator owns every entity-event registration; parses the state to float (x factor) so entries only implement the value handler
        from homeassistant.helpers.event import async_track_state_change_event

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
            self._unsubs.append(async_track_state_change_event(self.hass, [entity_id], handler))

    def state(self, entity_id: str, callback: Callable[[str], None]) -> None:
        # like track(), but hands the raw state string to the callback (for binary/enum entities)
        from homeassistant.helpers.event import async_track_state_change_event

        @ha_callback
        def handler(event: Event) -> None:
            new = event.data.get("new_state")
            if new is not None:
                callback(new.state)

        if entity_id != "":
            self._unsubs.append(async_track_state_change_event(self.hass, [entity_id], handler))

    def _track_p1(self, device_id: str) -> None:
        # All power-class entities on the chosen P1 device: name -> slot + sign, uom -> factor; a net total wins over the consumed/produced pair.
        entities = [e for e in er.async_entries_for_device(er.async_get(self.hass), device_id) if (e.original_device_class or e.device_class) == "power"]
        slots: dict[str, list] = {"total": [], "l1": [], "l2": [], "l3": []}
        totals = []
        for e in entities:
            eid = e.entity_id
            factor = 1000 if e.unit_of_measurement in ("kW", "kilowatt", "kilowatts") else 1
            if (phase := next((n for n in (1, 2, 3) if f"phase_{n}" in eid or f"phase{n}" in eid or f"_l{n}" in eid), 0) - 1) >= 0:
                sign = -1 if "produc" in eid else 1
                slots[f"l{phase + 1}"].append((eid, sign))
                self.track(eid, lambda w, p=phase, s=sign: self.distribution.power.__setitem__(p, s * w), factor)
            else:
                totals.append((eid, factor))

        has_net = any("produc" not in eid and "consum" not in eid for eid, _ in totals)
        for eid, factor in totals:
            if "produc" in eid and not has_net:
                slots["total"].append((eid, -1))
                self.track(eid, lambda w: self.hass.async_create_task(self.distribution._process_p1(-int(w), now=time.time())), factor)
            elif "produc" not in eid and ("consum" not in eid or not has_net):
                slots["total"].append((eid, 1))
                self.track(eid, lambda w: self.hass.async_create_task(self.distribution._process_p1(int(w), now=time.time())), factor)
        self.solver.globals["p1"] = slots  # per-slot (entity_id, sign) lists; baseload + serialize read this

    async def _migrate_entry_to_config(self) -> None:
        # One-time: convert legacy config-entry creds + "zendure" section into a Zendure driver item + system.p1_device
        if "drivers" in self.config.payload:
            return
        data = self.entry.data if self.entry else {}
        legacy = self.config.payload.get("zendure", {}) or {}
        if not (data.get(CONF_APPTOKEN) or data.get(CONF_MQTTSERVER) or legacy):
            return
        item = {
            "type": "ZendureDriver",
            "id": "zendure",
            "apptoken": data.get(CONF_APPTOKEN, ""),
            "mqtt_host": data.get(CONF_MQTTSERVER, ""),
            "mqtt_port": data.get(CONF_MQTTPORT, 1883),
            "mqtt_user": data.get(CONF_MQTTUSER, ""),
            "mqtt_psw": data.get(CONF_MQTTPSW, ""),
            "wifi_ssid": data.get(CONF_WIFISSID, ""),
            "wifi_psw": data.get(CONF_WIFIPSW, ""),
            "apikeys": legacy.get("apikeys", {}),
            "cloud_url": legacy.get("cloud_url", ""),
            "devices": legacy.get("devices", {}),
            "smartmeters": legacy.get("smartmeters", {}),
        }
        self.config.payload["drivers"] = [item]
        self.config.payload.pop("zendure", None)
        if data.get(CONF_P1DEVICE):
            self.config.payload.setdefault("system", {})["p1_device"] = data[CONF_P1DEVICE]
        await self._store.async_save(self.config.payload)
        _LOGGER.info("Migrated legacy config entry to panel config (Zendure driver + p1_device)")

    def _sections(self) -> dict:
        # config section name -> live object; the coordinator owns this mapping (read, serialize, save).
        return {"system": self.distribution, "price": self.prices}

    def schema(self) -> dict:
        # UI-keyed field-defs: singleton groups, per-consumer-id, per-device-id, per-solar-panel.
        out = {
            "system": CONFIG_SCHEMA.get(Distribution, []),
            "price": CONFIG_SCHEMA.get(DynamicPrice, []),
            "forecast": CONFIG_SCHEMA.get(Forecast, []),
        }
        for c in self.consumers:
            if cid := getattr(c, "id", None):
                out[cid] = CONFIG_SCHEMA.get(type(c), [])
        for drv in self.drivers:
            if did := getattr(drv, "id", None):
                out[did] = CONFIG_SCHEMA.get(type(drv), [])
            for dev_id, dev in drv.devices.items():
                out[dev_id] = CONFIG_SCHEMA.get(type(dev), [])
        for i in range(len(self.forecast.solar_panels)):
            out[f"solar_panel:{i}"] = CONFIG_SCHEMA.get(SolarPanel, [])
        return out

    def serialize(self) -> dict:
        # Build the config payload from the live objects (no file read). Unmanaged sections are preserved.
        cfg = Config(self.config.path)
        cfg.payload = dict(self.config.payload)
        for name, obj in self._sections().items():
            cfg.payload.pop(name, None)
            cfg.write(obj, name=name)
        # expose the Distribution's live entity ids so the panel can read hass.states + drive the editors
        cfg.payload.setdefault("system", {})["entities"] = {a: getattr(getattr(self.distribution, a, None), "entity_id", None) for a in _CONTROL_ENTITIES}
        cfg.payload["consumers"] = []
        for c in self.consumers:
            item = {"type": type(c).__name__}
            cfg.write(c, payload=item)
            cfg.payload["consumers"].append(item)
        cfg.payload["solar_panels"] = []
        for p in self.forecast.solar_panels:
            item = {}
            cfg.write(p, payload=item)
            cfg.payload["solar_panels"].append(item)
        cfg.payload.pop("zendure", None)  # legacy singleton section, superseded by the drivers list
        cfg.payload["drivers"] = []
        for drv in self.drivers:
            item = {"type": type(drv).__name__}
            cfg.write(drv, payload=item)  # id + creds scalar fields (apptoken, mqtt_*, wifi_*)
            item["apikeys"] = drv._config.get("apikeys", {})
            item["cloud_url"] = drv._config.get("cloud_url", "")
            devices = {}
            for dev_id, dev in drv.devices.items():
                entry = {}
                cfg.write(dev, payload=entry)
                entry["entities"] = {a: getattr(getattr(dev, a, None), "entity_id", None) for a in _DEVICE_ENTITIES}
                entry["bleName"] = getattr(dev, "ble_name", "")
                devices[dev_id] = entry
            item["devices"] = devices
            sms = {}
            for sm_id, sm in drv.smartmeters.items():
                sms[sm_id] = {"name": sm.name, "prodkey": getattr(sm, "prodkey", ""), "model_name": getattr(sm, "model", "")}
                sms[sm_id]["entities"] = {a: getattr(getattr(sm, a, None), "entity_id", None) for a in _SMARTMETER_ENTITIES}
            item["smartmeters"] = sms
            cfg.payload["drivers"].append(item)
        p1 = self.solver.globals.get("p1") or {}
        if p1.get("total"):
            cfg.payload["entities"] = {"p1_total": p1["total"][0][0]}
        return cfg.payload

    async def save_schema(self, action: str, section: str, payload: dict | None = None) -> None:
        # Apply one add/edit/delete to the live objects (section = singleton or collection, payload carries the fields + id), then persist and reload
        payload = payload or {}
        singleton = self._sections().get(section)
        if singleton is not None:
            if action == SchemaAction.edit:
                self.config.read(singleton, payload=payload)
        elif section == "consumers":
            if action == SchemaAction.delete:
                self.consumers[:] = [c for c in self.consumers if getattr(c, "id", None) != payload.get("id")]
            else:
                self.config.readlist(self.consumers, [payload])  # readlist finds-or-creates by is_key
        elif section == "drivers":
            if action == SchemaAction.delete:
                self.drivers[:] = [d for d in self.drivers if getattr(d, "id", None) != payload.get("id")]
            else:
                self.config.readlist(self.drivers, [payload])  # finds-or-creates by is_key (id)
        elif section == "devices":
            dev_id = payload.get("id")
            owner = next((d for d in self.drivers if dev_id in d.devices), None)
            if action == SchemaAction.add:
                target = next((d for d in self.drivers if getattr(d, "id", None) == payload.get("driver")), None) or (self.drivers[0] if self.drivers else None)
                if target is not None:
                    target.add_device(dev_id, payload)
            elif action == SchemaAction.delete:
                if owner is not None:
                    owner.devices.pop(dev_id, None)
            elif owner is not None:
                self.config.read(owner.devices[dev_id], payload=payload)
        elif section.startswith("solar_panel:"):
            i = int(section.split(":")[1])
            panels = self.forecast.solar_panels
            if action == SchemaAction.add:
                p = SolarPanel(name=payload.get("name", ""))
                self.config.read(p, payload=payload)
                panels.append(p)
            elif action == SchemaAction.delete:
                if 0 <= i < len(panels):
                    panels.pop(i)
            elif 0 <= i < len(panels):
                self.config.read(panels[i], payload=payload)
        elif action == SchemaAction.edit:
            # zm-group edits a collection item by its id (consumer / driver / device)
            obj = (
                next((c for c in self.consumers if getattr(c, "id", None) == section), None)
                or next((d for d in self.drivers if getattr(d, "id", None) == section), None)
                or next((d.devices[section] for d in self.drivers if section in d.devices), None)
            )
            if obj is not None:
                self.config.read(obj, payload=payload)
        self.config.payload = self.serialize()
        await self._store.async_save(self.config.payload)
        await self.async_setup()
