"""Coordinator._track_p1 unit tests: entity_id pattern -> slot/sign/factor mapping."""

import asyncio
from types import SimpleNamespace

from conftest import fake_hass
import custom_components.zenmaster.coordinator as coordinator_module
from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.const import Phase
from custom_components.zenmaster.coordinator import Coordinator
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor

for cls in (HaSensor, HaNumber, HaBinarySensor, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


def power_entity(eid: str, uom: str = "W", platform: str = "") -> SimpleNamespace:
    return SimpleNamespace(entity_id=eid, original_device_class="power", device_class=None, unit_of_measurement=uom, platform=platform)


def make_coordinator(monkeypatch, entities: list) -> tuple[Coordinator, list]:
    hass = fake_hass()
    coord = Coordinator(hass)
    calls: list[tuple] = []
    coord.track = lambda eid, cb, factor=1, **kw: calls.append((eid, factor, cb))  # capture instead of HA event tracking
    monkeypatch.setattr(coordinator_module.er, "async_entries_for_device", lambda reg, did: entities)
    return coord, calls


def test_track_p1_net_total_wins_and_phases_map(monkeypatch) -> None:
    entities = [
        power_entity("sensor.p1_power"),  # net total
        power_entity("sensor.p1_power_produced"),  # skipped: the net total wins over the pair
        power_entity("sensor.p1_power_consumed"),
        power_entity("sensor.p1_power_phase_1"),
        power_entity("sensor.p1_power_produced_phase_2"),
        power_entity("sensor.p1_power_l3"),
        SimpleNamespace(entity_id="sensor.p1_energy", original_device_class="energy", device_class=None, unit_of_measurement="kWh", platform=""),  # not power: filtered out
    ]
    coord, calls = make_coordinator(monkeypatch, entities)
    coord._track_p1("device")
    assert coord.p1_slots["total"] == [("sensor.p1_power", 1)]
    assert coord.p1_slots["l1"] == [("sensor.p1_power_phase_1", 1)]
    assert coord.p1_slots["l2"] == [("sensor.p1_power_produced_phase_2", -1)]
    assert coord.p1_slots["l3"] == [("sensor.p1_power_l3", 1)]

    # only the total is tracked; the phases are read along with it, a produced phase through its negative factor
    assert [e for e, _, _ in calls] == ["sensor.p1_power"]
    assert coord.p1_entities[Phase.l2] == [("sensor.p1_power_produced_phase_2", -1)]
    values = {"sensor.p1_power": "1200", "sensor.p1_power_phase_1": "500", "sensor.p1_power_produced_phase_2": "400", "sensor.p1_power_l3": "300"}
    coord.hass.states = SimpleNamespace(get=lambda eid: SimpleNamespace(state=values[eid]))
    got = {}

    async def process(p1, power, now):
        got["p1"], got["power"] = p1, power

    coord.distribution._process_p1 = process
    coord.hass.async_create_task = asyncio.run
    coord._p1_changed(0)
    assert (got["p1"], got["power"]) == (1200, [0.0, 500, -400, 300])


def test_track_p1_pair_without_net(monkeypatch) -> None:
    entities = [power_entity("sensor.grid_power_consumed"), power_entity("sensor.grid_power_produced", uom="kW")]
    coord, calls = make_coordinator(monkeypatch, entities)
    coord._track_p1("device")
    assert coord.p1_slots["total"] == [("sensor.grid_power_consumed", 1), ("sensor.grid_power_produced", -1000)]  # the solver gets the factor too, for the base load
    assert coord.p1_entities[Phase.all] == [("sensor.grid_power_consumed", 1), ("sensor.grid_power_produced", -1000)]  # sign and kW -> W factor in one
    assert [e for e, _, _ in calls] == ["sensor.grid_power_consumed", "sensor.grid_power_produced"]


def test_track_p1_phase_letters_map(monkeypatch) -> None:
    # Shelly and Huawei name their phases "Phase A/B/C", so the entity id carries phase_a/b/c, not a digit
    entities = [
        power_entity("sensor.grid_total_power"),  # a net total so no notification is raised
        power_entity("sensor.shelly_phase_a_power"),
        power_entity("sensor.huawei_phase_b_active_power"),
        power_entity("sensor.meter_phase_c_power"),
    ]
    coord, _ = make_coordinator(monkeypatch, entities)
    coord._track_p1("device")
    assert coord.p1_slots["l1"] == [("sensor.shelly_phase_a_power", 1)]
    assert coord.p1_slots["l2"] == [("sensor.huawei_phase_b_active_power", 1)]
    assert coord.p1_slots["l3"] == [("sensor.meter_phase_c_power", 1)]


def test_track_p1_huawei_phase_sign_flips(monkeypatch) -> None:
    # Huawei signs the grid export-positive, so its phases are negated against the import-positive convention
    entities = [
        power_entity("sensor.power_meter_active_power", platform="huawei_solar"),  # a total so no notification is raised
        power_entity("sensor.inverter_phase_a_active_power", platform="huawei_solar"),
    ]
    coord, _ = make_coordinator(monkeypatch, entities)
    coord._track_p1("device")
    assert coord.p1_slots["l1"] == [("sensor.inverter_phase_a_active_power", -1)]
    assert coord.p1_slots["total"] == [("sensor.power_meter_active_power", -1)]
