"""HaSensor kWh aggregation tests on an injected monotonic clock."""

import pytest

from conftest import fake_hass
import custom_components.zenmaster.sensor as sensor_module
from custom_components.zenmaster.entity import EntityDevice
from custom_components.zenmaster.sensor import HaSensor

HaSensor.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


class PowerDevice(EntityDevice):
    def entity_create(self) -> None:
        HaSensor(self, "power", uom="W", device_class="power", state_class="measurement", aggr=True)


def make_device(monkeypatch, clock: list[float]) -> tuple[HaSensor, HaSensor]:
    monkeypatch.setattr(sensor_module.time, "monotonic", lambda: clock[0])
    d = PowerDevice(fake_hass(), "aggr_test")
    for e in d.entities.values():
        e.hass = None
    return d.entities["power"], d.entities["power_aggr"]


def test_aggregate_integrates_watts_to_kwh(monkeypatch) -> None:
    clock = [1000.0]
    power, aggr = make_device(monkeypatch, clock)

    power.update_value(1000)  # first report only primes
    assert aggr.state is None

    clock[0] += 180.0
    power.update_value(1000)  # 1000 W over 180 s; unchanged values must integrate too
    assert aggr.state == pytest.approx(0.05)

    clock[0] += 180.0
    power.update_value(500)  # the previous 1000 W applies to the elapsed interval
    assert aggr.state == pytest.approx(0.10)


def test_aggregate_skips_long_gaps(monkeypatch) -> None:
    clock = [1000.0]
    power, aggr = make_device(monkeypatch, clock)

    power.update_value(1000)
    clock[0] += 180.0
    power.update_value(1000)
    assert aggr.state == pytest.approx(0.05)

    clock[0] += 400.0
    power.update_value(800)  # gap above AGGREGATE_MAX_GAP_S: re-prime only
    assert aggr.state == pytest.approx(0.05)

    clock[0] += 90.0
    power.update_value(0)  # 800 W over 90 s after the re-prime
    assert aggr.state == pytest.approx(0.07)
