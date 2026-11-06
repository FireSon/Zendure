"""ZendureBattery: sn prefix -> model/capacity mapping."""

from types import SimpleNamespace

import pytest

from conftest import fake_hass
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.zendure.battery import ZendureBattery

HaSensor.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


@pytest.mark.parametrize(
    ("sn", "model", "kwh"),
    [
        ("AO13456789", "AIO2400", 2.4),
        ("AO10456789", "AB1000", 0.96),
        ("BO10456789", "AB1000S", 0.96),
        ("CO1F456789", "AB2000S", 1.92),
        ("CO1E456789", "AB2000X", 1.92),
        ("CO10456789", "AB2000", 1.92),
        ("FO10456789", "AB3000", 2.88),
        ("GO10456789", "AB3000L", 2.88),
        ("JO2A456789", "I2400", 2.4),
        ("XO10456789", "Unknown", 0.0),
    ],
)
def test_battery_model_mapping(sn: str, model: str, kwh: float) -> None:
    bat = ZendureBattery(fake_hass(), sn, SimpleNamespace(device_id="parent", driver=None, subentry_id=None, battery_names={}))
    assert bat.model == model
    assert bat.kWh == kwh
    assert bat.device_id == f"{model}_{sn[-8:]}"
    ZendureBattery._registry.clear()
