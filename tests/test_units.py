"""Unit conversion: what a power sensor reads, in watts."""

from types import SimpleNamespace

import pytest

from custom_components.zenmaster.const import Phase, watt_factor


def _state(unit: str | None):
    return SimpleNamespace(attributes={"unit_of_measurement": unit} if unit else {})


def test_watt_factor_per_unit() -> None:
    assert watt_factor(_state("W")) == 1.0
    assert watt_factor(_state("kW")) == 1000.0
    assert watt_factor(None) == 1.0, "geen entiteit: laat de waarde staan"
    assert watt_factor(_state(None)) == 1.0, "geen eenheid: watt aangenomen"


def test_watt_factor_of_a_current_follows_the_phase() -> None:
    assert watt_factor(_state("A"), Phase.l1) == pytest.approx(230.0)
    assert watt_factor(_state("A"), Phase.all) == pytest.approx(692.82, abs=0.01)  # 400 V over three phases
