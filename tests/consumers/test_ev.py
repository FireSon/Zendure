import asyncio
import time
from datetime import datetime
from math import sqrt
from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.zenmaster.solver.consumers.ev import ChargeMode, ElectricVehicle
from custom_components.zenmaster.solver.solver import EntryStatus, LpSolver

_NOW = datetime(2026, 11, 11, 0, 0)  # midnight: departure 08:00 is 32 quarters away, slots 12..43
_AMPERE_W = 400 * sqrt(3)  # W per A on all phases, as watt_factor converts


class _Done:
    # the service call is recorded synchronously; awaiting it is a no-op
    def __await__(self):
        return iter(())


class _Hass:
    # the cable state and the charger number's unit, and every set_value written to the charger
    def __init__(self, plugged: bool, unit: str = "A") -> None:
        self.written: list[float] = []
        entities = {"binary_sensor.plug": SimpleNamespace(state="on" if plugged else "off", attributes={}),
                    "number.charge_current": SimpleNamespace(state="0", attributes={"unit_of_measurement": unit})}
        self.states = SimpleNamespace(get=entities.get)
        self.services = self

    def async_call(self, domain: str, service: str, data: dict, blocking: bool = False) -> _Done:
        self.written.append(data["value"])
        return _Done()

    def async_create_task(self, _awaitable) -> None:
        return None


def _lp(solar: float = 0.0, buy: float = 0.30, sell: float = 0.05) -> LpSolver:
    # a solver with only what the EV plan reads: forecast, prices and empty phase loads with ample room
    lp = LpSolver(track=lambda *a, **k: None, horizon=96)
    lp.solar, lp.price_buy, lp.price_sell = (lp.data(name, unit, 2) for name, unit in (("solar", "kW"), ("price_buy", "euro"), ("price_sell", "euro")))
    lp.solar.data[:], lp.price_buy.data[:], lp.price_sell.data[:] = solar, buy, sell
    lp.l1, lp.l2, lp.l3 = (np.zeros(96) for _ in range(3))
    lp.loads = [[lp.l1, lp.l2, lp.l3], [lp.l1], [lp.l2], [lp.l3]]
    lp.limit = 10.0  # kW per phase
    return lp


def _ev(lp: LpSolver, mode: ChargeMode, soc: float = 0.5, target: float = 80.0, plugged: bool = True, unit: str = "A") -> ElectricVehicle:
    # a 60 kWh car on an 11 kW three-phase charger, configured the way the coordinator does it
    ev = ElectricVehicle()
    ev.id, ev.capacity_kwh, ev.max_power_kw, ev.ev_max_soc, ev.charge_compensation_eur_kwh = "ev", 60.0, 11.0, target, 0.20
    ev.departure_time, ev.connected_entity, ev.charge_power_entity = "08:00", "binary_sensor.plug", "number.charge_current"
    ev.ev_charge_mode, ev.hass, ev.entities = mode, _Hass(plugged, unit), {}
    lp.hass = ev.hass
    ev.entry_config(lp)
    ev.current_soc = soc
    return ev


def _plan(ev: ElectricVehicle, lp: LpSolver) -> np.ndarray:
    # one quarter run: the plan in kW for slots 12..43
    asyncio.run(ev.entry_prerun(lp, _NOW))
    return ev.ev_power.data[12:44]


def test_maximum_charges_the_first_quarters_at_full_power_to_the_target() -> None:
    lp = _lp()
    ev = _ev(lp, ChargeMode.maximum)  # 18 kWh to go: six quarters of 2.75 kWh and a remainder of 1.5 kWh
    plan = _plan(ev, lp)
    assert plan[:6] == pytest.approx([11.0] * 6)
    assert plan[6] == pytest.approx(6.0)
    assert not plan[7:].any()


def test_a_remainder_under_the_charger_minimum_is_left_out() -> None:
    lp = _lp()
    ev = _ev(lp, ChargeMode.maximum, target=60.0)  # 6 kWh to go: two full quarters and 0.5 kWh, under the 1.05 kWh of 4.2 kW
    plan = _plan(ev, lp)
    assert plan[:2] == pytest.approx([11.0, 11.0])
    assert not plan[2:].any()


def test_ready_by_reaches_its_own_target_whatever_the_price() -> None:
    lp = _lp(buy=0.50, sell=0.40)  # every quarter above the price limit of 0.20
    ev = _ev(lp, ChargeMode.ready_by)
    ev.soc_target = 50.0 + 5.5 / 60 * 100  # 5.5 kWh: two full quarters, under the upper bound of 80 %
    plan = _plan(ev, lp)
    assert plan.sum() * 0.25 == pytest.approx(5.5)


def test_price_charges_only_under_the_price_limit() -> None:
    lp = _lp()
    lp.price_buy.data[20:24] = 0.15  # the only quarters under the limit of 0.20
    ev = _ev(lp, ChargeMode.price)
    plan = _plan(ev, lp)
    assert list(np.flatnonzero(plan) + 12) == [20, 21, 22, 23]


def test_solar_plans_only_quarters_with_enough_surplus_after_the_house_load() -> None:
    lp = _lp()
    lp.solar.data[20:24], lp.solar.data[24:28] = 6.0, 3.0  # 3 kW stays under the 4.2 kW minimum
    lp.l1[20 - 12] = 0.5  # the planned house load takes its share of the sun first
    ev = _ev(lp, ChargeMode.solar)
    plan = _plan(ev, lp)
    assert plan[20 - 12] == pytest.approx(5.5)
    assert plan[21 - 12:24 - 12] == pytest.approx([6.0] * 3)
    assert not plan[24 - 12:].any() and not plan[:20 - 12].any()


def test_unplugging_puts_the_mode_back_to_price_and_the_charger_to_zero() -> None:
    lp = _lp()
    ev = _ev(lp, ChargeMode.maximum, plugged=False)
    ev.entities["ev_charge_mode"] = SimpleNamespace(update_value=lambda value: setattr(ev, "ev_charge_mode", value))
    plan = _plan(ev, lp)
    assert ev.ev_charge_mode is ChargeMode.price
    assert not plan.any() and ev.hass.written == [0.0]


@pytest.mark.parametrize(("mode", "soc", "plugged", "status"), [
    (ChargeMode.maximum, 0.5, True, EntryStatus.running),
    (ChargeMode.price, 0.5, True, EntryStatus.planned),
    (ChargeMode.price, 0.8, True, EntryStatus.ready),
    (ChargeMode.maximum, 0.5, False, EntryStatus.none),
])
def test_the_status_follows_the_plan(mode: ChargeMode, soc: float, plugged: bool, status: EntryStatus) -> None:
    lp = _lp()
    lp.price_buy.data[20:24] = 0.15  # Price finds cheap quarters later on, not now
    ev = _ev(lp, mode, soc=soc, plugged=plugged)
    _plan(ev, lp)
    assert ev.status is status


def test_solar_follows_the_surplus_at_most_once_per_step_and_never_under_the_minimum() -> None:
    lp = _lp()
    ev = _ev(lp, ChargeMode.solar)
    ev.ev_power.data[12], ev._written_kw = 6.0, 6.0  # a planned quarter, the charger at 6 kW

    # 2 kW surplus on top of the 6 kW the car draws
    ev.on_surplus(2000.0)
    assert ev.hass.written == [round(8000 / _AMPERE_W, 2)]

    # within 30 s nothing is written
    ev.on_surplus(-5000.0)
    assert len(ev.hass.written) == 1

    # after the step a shortfall lowers the car, but not under 4.2 kW
    ev._stepped -= 31
    ev.on_surplus(-5000.0)
    assert ev.hass.written[-1] == round(4200 / _AMPERE_W, 2)


def test_solar_steps_on_the_mean_surplus_of_the_step() -> None:
    lp = _lp()
    ev = _ev(lp, ChargeMode.solar)
    ev.ev_power.data[12], ev._written_kw = 6.0, 6.0
    ev._stepped = time.time()  # a step just happened: the readings collect

    # 1000 W and 3000 W within the step, then the step is due: the car gets the mean of 2 kW on top
    ev.on_surplus(1000.0)
    ev._stepped -= 31
    ev.on_surplus(3000.0)
    assert ev.hass.written == [round(8000 / _AMPERE_W, 2)]


def test_only_solar_follows_the_surplus() -> None:
    lp = _lp()
    ev = _ev(lp, ChargeMode.price)
    ev.ev_power.data[12] = 6.0
    ev.on_surplus(2000.0)
    assert ev.hass.written == []


@pytest.mark.parametrize(("unit", "value"), [("A", round(4200 / _AMPERE_W, 2)), ("kW", 4.2), ("W", 4200.0)])
def test_write_converts_kw_to_the_unit_of_the_charger(unit: str, value: float) -> None:
    lp = _lp()
    ev = _ev(lp, ChargeMode.price, unit=unit)
    ev._write(4.2)  # the fake records the call at once
    assert ev.hass.written == [value]
