"""Consumers and solar panels mirror the config entry's subentries: build, apply, persist, repair and drop.
Their settings are entities on the consumer's device: the attribute follows the entity, the LP follows the attribute."""

import asyncio
from types import MappingProxyType, SimpleNamespace

from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.button import HaButton
from custom_components.zenmaster.coordinator import Coordinator, field_values, title_taken
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.solver.consumers.appliance import Appliance
from custom_components.zenmaster.solver.solver import EntryStatus
from custom_components.zenmaster.switch import HaSwitch
from custom_components.zenmaster.time import HaTime

for cls in (HaSensor, HaNumber, HaBinarySensor, HaSelect, HaSwitch, HaButton, HaTime):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


class _Entry:
    # the config entry as the coordinator sees it: a subentries mapping and an update listener hook
    def __init__(self) -> None:
        self.entry_id = "entry"
        self.subentries: dict = {}
        self.listeners: list = []

    def add_update_listener(self, fn):
        self.listeners.append(fn)
        return lambda: None

    def async_on_unload(self, fn) -> None:
        pass


class _ConfigEntries:
    # HA's subentry API over the fake entry; a subentry is replaced as a whole, like HA's frozen dataclass
    def async_add_subentry(self, entry, sub) -> bool:
        entry.subentries[sub.subentry_id] = sub
        return True

    def async_update_subentry(self, entry, sub, *, data=None, title=None, unique_id=None) -> bool:
        new = SimpleNamespace(subentry_id=sub.subentry_id, subentry_type=sub.subentry_type, title=sub.title if title is None else title, data=MappingProxyType(dict(sub.data if data is None else data)), unique_id=sub.unique_id)
        changed = dict(new.data) != dict(sub.data) or new.title != sub.title
        entry.subentries[sub.subentry_id] = new
        return changed

    def async_remove_subentry(self, entry, subentry_id) -> bool:
        return entry.subentries.pop(subentry_id, None) is not None

    def async_entries(self, domain=None) -> list:
        return []  # no entry here, so the device lookup takes the registry-wide path


def _sub(sid: str, stype: str, title: str, **data) -> SimpleNamespace:
    return SimpleNamespace(subentry_id=sid, subentry_type=stype, title=title, data=MappingProxyType(data), unique_id=None)


def _coord(*subs) -> tuple[Coordinator, _Entry]:
    calls: list = []
    hass = SimpleNamespace(loop=SimpleNamespace(is_running=lambda: False), config_entries=_ConfigEntries(), states=SimpleNamespace(get=lambda eid: None), config=SimpleNamespace(latitude=52.0, longitude=5.0, time_zone="UTC"), async_create_task=lambda x: None, services=SimpleNamespace(async_call=lambda *a, **k: calls.append((a, k))), calls=calls)
    entry = _Entry()
    for s in subs:
        entry.subentries[s.subentry_id] = s
    coord = Coordinator(hass, entry)
    from custom_components.zenmaster.solver.forecast import Forecast

    coord.forecast = Forecast(hass=hass)
    return coord, entry


def test_consumers_are_built_from_the_subentries() -> None:
    coord, entry = _coord(_sub("01A", "appliance", "Wasmachine", kind="washingmachine", power_entity="sensor.wm"), _sub("01B", "boiler_heat_pump", "Boiler", temp_entity="sensor.tank"))
    assert asyncio.run(coord.sync_subentries()) is True
    wm, boiler = coord.consumers
    assert (wm.id, wm.name, wm.kind, wm.power_entity) == ("01A", "Wasmachine", "washingmachine", "sensor.wm")
    assert (boiler.id, boiler.name, boiler.temp_entity) == ("01B", "Boiler", "sensor.tank")
    assert wm.entities["state"].state == "none", "the state sensor exists from the start"
    assert wm.entry is entry and wm.subentry_id == "01A", "its entities are filed under its subentry"
    assert entry.subentries["01A"].data["armed"] is False, "the restored state is written back so both sides agree"
    assert asyncio.run(coord.sync_subentries()) is False, "a second pass finds nothing to do"


def test_the_settings_are_entities_on_the_device() -> None:
    coord, _ = _coord(_sub("01A", "appliance", "Wasmachine"), _sub("01B", "boiler_heat_pump", "Boiler"))
    asyncio.run(coord.sync_subentries())
    wm, boiler = coord.consumers
    assert isinstance(wm.entities["manage"], HaSwitch) and isinstance(wm.entities["latest_start_time"], HaTime)
    assert isinstance(wm.entities["power_rating"], HaNumber) and isinstance(wm.entities["phase"], HaSelect)
    assert isinstance(wm.entities["start"], HaButton) and "stop" in wm.entities and "skip" in wm.entities
    assert isinstance(boiler.entities["t_min"], HaNumber) and isinstance(boiler.entities["legionella"], HaSwitch)
    for not_an_entity in ("kind", "armed", "power_entity", "name", "id", "detect_phase"):
        assert not_an_entity not in wm.entities
    assert wm.entities["power_rating"]._attr_native_value == 0.0 and boiler.entities["t_min"]._attr_native_value == 38.0, "the defaults show until HA restores a value"


def test_a_stored_setting_comes_back() -> None:
    coord, _ = _coord(_sub("01A", "appliance", "Wasmachine"))
    coord.config.payload["devices"] = {"01A": {"wasmachine_power_rating": 2300.0, "wasmachine_manage": False}}
    asyncio.run(coord.sync_subentries())
    wm = coord.consumers[0]
    assert wm.power_rating == 2300.0 and wm.manage is False, "device_load puts the stored settings back on the attributes"


def test_a_setting_entity_moves_the_attribute() -> None:
    coord, _ = _coord(_sub("01A", "appliance", "Wasmachine"), _sub("01B", "boiler_heat_pump", "Boiler"))
    asyncio.run(coord.sync_subentries())
    wm, boiler = coord.consumers
    boiler.entities["t_min"].set_state("42.5", restore=True)
    assert boiler.t_min == 42.5, "a float knob keeps its half degree"
    wm.entities["latest_start_time"].set_state("13:30:00", restore=True)
    assert wm.latest_start_time == "13:30", "HA's HH:MM:SS lands as the HH:MM the appliance reads"
    wm.entities["phase"].set_state("L2", restore=True)
    assert wm.phase == 2, "the option label maps back to the phase number"
    wm.entities["cycle_duration_slots"].set_state("4.0", restore=True)
    assert wm.cycle_duration_slots == 4 and isinstance(wm.cycle_duration_slots, int)


def test_manage_off_takes_the_consumer_out_of_the_plan() -> None:
    coord, _ = _coord(_sub("01A", "appliance", "Wasmachine"), _sub("01B", "boiler_heat_pump", "Boiler", setpoint_entity="number.boiler_setpoint"))
    asyncio.run(coord.sync_subentries())
    wm, boiler = coord.consumers
    wm.armed = True
    wm.entities["manage"].set_state("off", restore=True)
    assert wm.manage is False and wm.status is EntryStatus.unmanaged and wm.armed is False, "hands off: nothing waits for a window"
    boiler._target = 50.0
    boiler.manage = False
    assert boiler._target == 0.0 and coord.hass.calls[-1][0][:2] == ("number", "set_value"), "the tank goes back to its own thermostat"
    built: list = []
    coord.solver.init = lambda entries: built.extend(entries)
    coord.rebuild_solver()
    assert wm not in built and boiler not in built, "an unmanaged consumer is not even in the LP"
    wm.manage = True
    assert wm.status is EntryStatus.none


def test_a_changed_link_is_applied_but_owned_state_is_not() -> None:
    coord, entry = _coord(_sub("01A", "appliance", "Wasmachine", power_entity="sensor.wm"))
    asyncio.run(coord.sync_subentries())
    wm = coord.consumers[0]
    wm.armed = True  # runtime state, never overwritten from stale subentry data
    sub = entry.subentries["01A"]
    coord.hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, "power_entity": "sensor.wm2", "armed": False})
    assert asyncio.run(coord.sync_subentries()) is True
    assert wm.power_entity == "sensor.wm2" and wm.armed is True


def test_a_rename_follows_without_touching_the_structure() -> None:
    coord, entry = _coord(_sub("01A", "appliance", "Wasmachine"))
    asyncio.run(coord.sync_subentries())
    coord.hass.config_entries.async_update_subentry(entry, entry.subentries["01A"], title="Wasmachine zolder")
    assert asyncio.run(coord.sync_subentries()) is False
    assert coord.consumers[0].name == "Wasmachine zolder"


def test_a_duplicate_name_gets_a_suffix() -> None:
    coord, entry = _coord(_sub("01A", "appliance", "Wasmachine"), _sub("01B", "appliance", "wasmachine"))
    asyncio.run(coord.sync_subentries())
    assert entry.subentries["01B"].title == "wasmachine 2" and coord.consumers[1].name == "wasmachine 2"
    assert title_taken(entry, "WASMACHINE") and not title_taken(entry, "Wasmachine", skip="01A")


def test_a_removed_subentry_drops_its_consumer() -> None:
    coord, entry = _coord(_sub("01A", "appliance", "Wasmachine"), _sub("01B", "appliance", "Droger"))
    asyncio.run(coord.sync_subentries())
    coord.hass.config_entries.async_remove_subentry(entry, "01A")
    assert asyncio.run(coord.sync_subentries()) is True
    assert [c.id for c in coord.consumers] == ["01B"]


def test_persist_writes_the_links_and_the_restart_state() -> None:
    coord, entry = _coord(_sub("01A", "appliance", "Wasmachine"))
    asyncio.run(coord.sync_subentries())
    wm = coord.consumers[0]
    wm.armed, wm.armed_at, wm.phase = True, 1788100000.0, 2
    coord.persist_consumer(wm)
    data = entry.subentries["01A"].data
    assert data["armed"] is True and data["armed_at"] == 1788100000.0 and data["kind"] == "custom"
    for setting in ("phase", "manage", "power_rating", "id", "name", "state"):
        assert setting not in data, "settings are entities, HA keeps those; the identity is the subentry itself"

    # a restart: the consumer is rebuilt from the same subentry and comes back armed
    fresh, _ = _coord(entry.subentries["01A"])
    asyncio.run(fresh.sync_subentries())
    assert fresh.consumers[0].armed is True and fresh.consumers[0].armed_at == 1788100000.0


def test_learned_params_live_in_the_subentry() -> None:
    coord, entry = _coord(_sub("01A", "appliance", "Wasmachine", learned={"profile": [1.0, 1.0], "quiet_s": 3600.0}))
    asyncio.run(coord.sync_subentries())
    wm = coord.consumers[0]
    assert wm._quiet_s == 3600.0 and wm.cycle_duration_slots == 2
    asyncio.run(wm.save_params(coord.hass, {"profile": [2.0], "quiet_s": 2700.0}))
    assert entry.subentries["01A"].data["learned"] == {"profile": [2.0], "quiet_s": 2700.0}
    assert entry.subentries["01A"].data["power_entity"] == "", "the other data stays"


def test_solar_panels_follow_the_manager_list() -> None:
    # the solar groups are rows in the manager list, and the list is what counts
    coord, _ = _coord()
    coord.manager_save("solar_panels", [{"id": "01S", "name": "Zuid", "power_entity": "sensor.pv_zuid", "derate_entity": ""}])
    assert asyncio.run(coord.sync_subentries()) is True
    assert [(p.name, p.power_entity) for p in coord.forecast.solar_panels] == [("Zuid", "sensor.pv_zuid")]
    coord.manager_save("solar_panels", [])
    assert asyncio.run(coord.sync_subentries()) is True and coord.forecast.solar_panels == []


def test_field_values_holds_only_the_persisted_fields() -> None:
    d = Appliance()
    d.id, d.phase, d.armed_at = "01A", 3, 5.0
    values = field_values(d, Appliance.CONFIG)
    assert values["armed_at"] == 5.0 and values["kind"] == "custom" and values["armed"] is False
    for absent in ("id", "state", "detect_phase", "phase", "manage", "name"):
        assert absent not in values
