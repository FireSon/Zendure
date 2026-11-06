import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.zenmaster.const import Phase
from custom_components.zenmaster.solver.solver import LpSolver, SolverEntry
from custom_components.zenmaster.solver.boiler import BoilerHeatPump, at, curve
from custom_components.zenmaster.solver.heating import Heating, HeatingHeatPump, PoolHeatPump, _cop, fit_house


class _Grid(SolverEntry):
    # minimal priced grid supply so the solver has a source to balance the heating draw against
    def __init__(self, price: list[float]) -> None:
        super().__init__()
        self.price = price

    def entry_config(self, lp: LpSolver) -> None:
        lp.globals["phase_limit_kw"] = 17 / 3
        self.out = lp.data("grid_buy", "kW")
        self.buy = lp.column(tags=("supply",), dest=self.out)

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        self.buy.costs[:] = self.price


def _house() -> Heating:
    h = HeatingHeatPump()  # soft comfort floor, loses heat to the outdoor forecast
    h.name = "house"
    h.id = "house"
    h.phase = Phase.l1
    h.temp_entity = ""
    h.max_power = 3.0
    h.t_min = 20.0
    h.t_max = 22.0
    h.t_min_night = 18.0
    h._temp0 = 20.5
    return h


def _house_lp(price: list[float], outdoor: float, now: datetime):
    grid = _Grid(price)
    h = _house()
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)
    lp.hass = None
    lp.temp, lp.wind, lp.sun = (lp.data(n, u, 7, source="meteo") for n, u in (("temp", "C"), ("wind", "m/s"), ("sun", "W/m2")))  # the weather the house loses heat to
    lp.temp.data[12:108] = outdoor
    lp.init([grid, h])
    x = asyncio.run(lp.run(now))
    return lp, grid, h, x


def test_house_holds_comfort_and_preheats() -> None:
    price = [0.1] * 48 + [1.0] * 48  # cheap first half, expensive second half
    lp, grid, h, x = _house_lp(price, 5.0, datetime(2026, 6, 28, 12, 0))

    assert x is not None and lp._last_lp_status == 0
    temp = h.temp.dest.data[12:108]
    assert temp[0] == pytest.approx(20.5)  # slot 0 pinned to the live temperature
    assert np.all(temp >= h.t_min - 3.0 - 1e-6)  # the hard floor, 3 degC under t_min, never breached

    slack = x[h.slack.index:h.slack.index + 96]
    assert np.all(slack < 1e-3)  # comfort floor held, no shortfall needed

    power = x[h.power.index:h.power.index + 96]
    assert power[:48].sum() > power[48:].sum()  # pre-heats the building mass while energy is cheap


def test_house_night_setback() -> None:
    price = [0.3] * 96
    lp, grid, h, x = _house_lp(price, 5.0, datetime(2026, 6, 28, 0, 0))  # now = midnight UTC

    assert x is not None and lp._last_lp_status == 0
    assert h._tmin[0] == pytest.approx(18.0)  # 00:00 -> night floor
    assert h._tmin[24] == pytest.approx(18.0)  # 06:00 -> still night
    assert h._tmin[40] == pytest.approx(18.5)  # 10:00 -> day floor
    assert h._tmin[72] == pytest.approx(20.0)  # 18:00 -> evening floor, t_min
    assert h._tmin[92] == pytest.approx(18.0)  # 23:00 -> night floor


def test_the_nameplate_sets_the_electrical_range() -> None:
    # FDC100VNA-W: 4,0 to 12,5 kW heat at COP 3,88, so the solver may draw 1,03 to 3,22 kW
    h = PoolHeatPump()  # the nameplate lives on the pool and the house; a tank is capped by max_power
    h.name = h.id = "pool"
    h.phase = Phase.l1
    h.temp_entity = ""
    h.heat_max, h.cop = 12.5, 3.88
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)
    lp.hass = None
    lp.init([_Grid([0.1] * 96), h])

    assert h.power.hi == pytest.approx(12.5 / 3.88, abs=0.01)  # heat divided by COP is the electrical cap


class _Prices(SolverEntry):
    # publishes the series the boiler reads outside the LP: buy, sell, the solar and the outdoor forecast, all from now
    def __init__(self, buy: list[float], solar: list[float] | None = None, outside: list[float] | None = None) -> None:
        super().__init__()
        self.buy = buy
        self.solar = solar or []
        self.outside = outside or []

    def entry_config(self, lp: LpSolver) -> None:
        self.price_buy = lp.price_buy = lp.data("price_buy", "euro", 7)
        self.price_sell = lp.price_sell = lp.data("price_sell", "euro", 7)
        self.solar_data = lp.solar = lp.data("solar", "kW", 7)
        self.temp = lp.temp = lp.data("temp", "C", 7)

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        self.price_buy.data[12 : 12 + len(self.buy)] = self.buy
        self.price_sell.data[12 : 12 + len(self.buy)] = self.buy
        self.solar_data.data[12 : 12 + len(self.solar)] = self.solar
        self.temp.data[12 : 12 + len(self.outside)] = self.outside


def _boiler(temp: float = 38.0) -> BoilerHeatPump:
    # the measured tank: 500 l at 3.14 kW, the level of its two runs, the top never under 38, and its first reading
    h = BoilerHeatPump()
    h.name = h.id = "boiler"
    h.phase = Phase.l1
    h.temp_entity = h.power_entity = ""
    h.ambient = 20.0
    h.volume = 500.0
    h.max_power = 3.14
    h.level = 0.54
    h.on_temp(temp)
    return h


def _run(h: BoilerHeatPump, buy: list[float], now: datetime, solar: list[float] | None = None, outside: list[float] | None = None) -> None:
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)
    lp.hass = None  # offline: skips calibration in entry_prerun
    lp.init([_Grid([0.1] * 96), _Prices(buy, solar, outside), h])
    asyncio.run(lp.run(now))


def _week(bargain: int | None = None, cheap_day: int | None = None) -> list[float]:
    # seven days of quarters from now: flat, with one bargain quarter and/or one cheap day, both as offsets
    price = [0.30] * 672
    if cheap_day is not None:
        price[cheap_day * 96 : (cheap_day + 1) * 96] = [0.10] * 96
    if bargain is not None:
        price[bargain] = 0.05
    return price


def test_the_fill_waits_for_the_cheapest_block_of_the_day() -> None:
    # at midnight the fill goes to the cheap afternoon, and starts when that block comes
    h = _boiler()
    price = _week()
    price[52:60] = [0.20] * 8
    _run(h, price, datetime(2026, 6, 28, 0, 0))
    assert h.fill_at == datetime(2026, 6, 28, 13, 0).timestamp() and h._target == 0.0, "the block at 13:00 is the cheapest, so it waits"
    assert h.data.data[12 + 52] == pytest.approx(3.14) and h.data.data[12 + 51] == 0.0, "the block is in the series, so the panel shows it"

    _run(h, price[52:], datetime(2026, 6, 28, 13, 0))
    assert h._target > 38.0, "its block has come, so it fills"


def test_a_warmer_afternoon_wins_over_the_same_price_at_night() -> None:
    # flat prices: the block goes where the outside air gives the most heat per kWh
    h = _boiler()
    outside = [5.0] * 96
    outside[56:64] = [15.0] * 8
    _run(h, _week(), datetime(2026, 6, 28, 0, 0), outside=outside)
    assert h.fill_at == datetime(2026, 6, 28, 14, 0).timestamp(), "the warm hours from 14:00 make the cheapest heat"


def test_the_fill_is_only_planned_at_midnight() -> None:
    # made or restarted in the afternoon: no plan for the rest of the day, even with the cheapest quarter right now
    h = _boiler()
    price = _week()
    price[0] = 0.05
    _run(h, price, datetime(2026, 6, 28, 16, 15))
    assert h.fill_at == 0.0 and h._fill == 0.0 and h._target == 0.0, "the day was planned at midnight or not at all"


def test_a_block_that_has_passed_is_not_made_up() -> None:
    # a restart after the block of the day: that block is gone, and nothing takes its place later
    h = _boiler()
    h.fill_at = datetime(2026, 6, 28, 13, 0).timestamp()
    _run(h, _week(), datetime(2026, 6, 28, 16, 15))
    assert h.fill_at == 0.0 and h._fill == 0.0 and h._target == 0.0, "the block of 13:00 is not run at 16:15"

    # a restart before the block keeps it
    h = _boiler()
    h.fill_at = datetime(2026, 6, 28, 17, 0).timestamp()
    _run(h, _week(), datetime(2026, 6, 28, 16, 15))
    assert h.fill_at == datetime(2026, 6, 28, 17, 0).timestamp() and h._target == 0.0, "the block of 17:00 still comes"
    _run(h, _week(), datetime(2026, 6, 28, 17, 0))
    assert h._target > 38.0, "and at 17:00 it fills"


def test_a_layered_tank_that_holds_until_tomorrow_does_not_fill() -> None:
    # 44 at the sensor, but a warm top: the top holds 38 until the block of tomorrow, so no fill
    h = _boiler(44.0)
    h._layers = np.linspace(32.0, 48.0, 10)
    _run(h, _week(), datetime(2026, 6, 28, 0, 0))
    assert h._fill == 0.0 and h._target == 0.0, "the top is warm enough, so nothing runs"


def test_the_level_comes_from_the_model() -> None:
    # a mixed tank at 36 at the block: heated in the model until its top holds 38 for a day, that is 42.4 at the sensor, not the 46.6 of a mixed sum with the day's draw at the end
    h = _boiler(36.0)
    _run(h, _week(), datetime(2026, 6, 28, 0, 0))
    assert h._fill == pytest.approx(42.4, abs=0.3), "the top holds 38 until tomorrow from 42.4"


def test_the_layers_are_kept_over_a_restart() -> None:
    # the model of before the restart comes back with its warm top, moved to where the sensor stands now
    h = BoilerHeatPump()
    h.layers = list(np.linspace(30.0, 48.0, 10))
    h.on_temp(42.0)
    assert at(h._layers, 0.5) == pytest.approx(42.0) and h._layers[-1] - h._layers[0] == pytest.approx(18.0), "the shape is kept, the level follows the sensor"


def test_a_fill_the_unit_does_not_start_is_dropped() -> None:
    # the unit keeps its compressor off for a quarter: the fill is dropped, it does not run later in the day
    h = _boiler()
    _run(h, _week(), datetime(2026, 6, 28, 0, 0))
    assert h._fill > 38.0, "the block is now, so it fills"
    _run(h, _week(), datetime(2026, 6, 28, 0, 15))
    assert h._fill == 0.0 and h._target == 0.0, "not started within a quarter, so dropped"

    # the unit starts: the fill goes on
    h = _boiler()
    _run(h, _week(), datetime(2026, 6, 28, 0, 0))
    h.on_power(3000.0, datetime(2026, 6, 28, 0, 5).timestamp())
    _run(h, _week(), datetime(2026, 6, 28, 0, 15))
    assert h._fill > 38.0 and h._target > 38.0, "it runs, so it goes on"


def test_the_loss_per_layer_follows_the_sensors() -> None:
    # 31 hours measured at the bottom, 110 at the middle, 213 at the top: the layers in between along a line, the ends held
    h = _boiler()
    h.tau_bottom, h.tau_top = 31.0, 213.0
    taus = h.taus()
    assert taus[1] == pytest.approx(31.0) and taus[8] == pytest.approx(213.0), "the layers at the bottom and the top sensor carry their tau"
    assert taus[0] == pytest.approx(31.0) and taus[-1] == pytest.approx(213.0), "the ends hold the tau of the nearest sensor"
    assert taus[4] < 110.0 < taus[5], "the middle sensor lies on the line between the two layers around it"
    h.tau_bottom = h.tau_top = 0.0
    assert np.all(h.taus() == 110.0), "without other sensors one tau for the whole tank"


def test_a_slow_top_holds_a_day_longer() -> None:
    # a tank at 42 with one tau of 110 would be at 37.7 tomorrow and fills; a top that loses at 213 hours is at 39.6 and does not
    h = _boiler(42.0)
    _run(h, _week(), datetime(2026, 6, 28, 0, 0))
    assert h._fill > 38.0, "one tau for the whole tank: the top would not hold, so it fills"
    h = _boiler(42.0)
    h.tau_top = 213.0
    _run(h, _week(), datetime(2026, 6, 28, 0, 0))
    assert h._fill == 0.0 and h._target == 0.0, "the slow top holds until tomorrow, so no fill"


def test_the_top_sensor_pulls_the_layers_over_the_middle() -> None:
    # the model mixed at 44, the top sensor reads 47.7: the layers up there follow it, the middle stays
    h = _boiler(44.0)
    h._top = 47.7
    _run(h, _week(), datetime(2026, 6, 28, 10, 0))
    assert at(h._layers, 0.85) == pytest.approx(47.7, abs=0.1) and at(h._layers, 0.5) == pytest.approx(44.0, abs=0.5), "the top follows its sensor"


def test_the_daily_energy_follows_what_was_drawn() -> None:
    # 1 kWh drawn since the last block moves a daily energy of 2 a quarter of the way: 1.75, and the count starts again
    h = _boiler()
    h._filled, h._tapped = True, 1.0
    _run(h, _week(), datetime(2026, 6, 28, 0, 0))
    assert h.draw == pytest.approx(1.75) and h._tapped == 0.0, "the weighted mean of what the house drew"


def test_the_bottom_sensor_pulls_the_layers_under_the_middle() -> None:
    # the model mixed at 44, the bottom sensor reads 38: the layers down there follow it, the middle stays, and the heat that left counts as drawn
    h = _boiler(44.0)
    h._bottom = 38.0
    _run(h, _week(), datetime(2026, 6, 28, 10, 0))
    assert at(h._layers, 0.15) == pytest.approx(38.0, abs=0.1) and at(h._layers, 0.5) == pytest.approx(44.0, abs=0.5), "the bottom follows its sensor"
    assert h._tapped > 0.5, "the heat that left the bottom is what the house drew"


def test_a_tap_takes_the_water_from_the_top() -> None:
    # the sensor in the middle falls 5 K with the unit off: the model draws from the top until its middle follows
    h = _boiler(45.0)
    _run(h, _week(), datetime(2026, 6, 28, 13, 0))
    h.on_temp(40.0)
    _run(h, _week(), datetime(2026, 6, 28, 13, 15))
    assert at(h._layers, 0.5) <= 40.0 and h._layers[-1] > 44.0, "the middle follows the sensor, the top still holds its heat"
    assert h._tapped > 1.0, "the heat of the tap is counted as drawn"


def test_a_long_run_measures_the_level_and_mixes_the_model() -> None:
    # 45 minutes at 3 kW lift the tank from 38 to 50: that heat against the energy on the curve is the level
    h = _boiler()
    t0, t1 = datetime(2026, 6, 28, 13, 0), datetime(2026, 6, 28, 13, 45)
    h.on_power(3000.0, t0.timestamp())
    _run(h, _week(), t0)
    h.on_power(0.0, t1.timestamp())
    h.on_temp(50.0)
    _run(h, _week(), t1)
    heat = 500.0 * 4.2 / 3600.0 * (12.0 + (44.0 - 20.0) * 0.75 / 110.0)
    assert heat / (2.25 * curve(0.0, 38.0)) <= h.level <= heat / (2.25 * curve(0.0, 50.0)), "the curve is taken at the tank between 38 and 50"
    assert np.all(h._layers == 50.0), "after a long run the tank is mixed"


class _Hass:
    # the few calls the setpoint makes: the state of the entity and the service that writes it
    def __init__(self, value: str) -> None:
        self.value, self.calls = value, []
        self.states = self.services = self

    def get(self, entity_id: str) -> SimpleNamespace:
        return SimpleNamespace(state=self.value, attributes={"min": 20.0, "step": 1.0})

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = False) -> None:
        self.calls.append((domain, service, data["value"]))

    def async_create_task(self, coro) -> None:
        asyncio.run(coro)


def test_a_setpoint_someone_else_changed_goes_back() -> None:
    # parked at 20, then someone sets 50: the next write puts 20 back; a setpoint that already holds is left alone
    h = _boiler()
    h.setpoint_entity, h.hass = "number.pw_setpoint_hot_water", _Hass("50")
    h.setpoint(None)
    assert h.hass.calls == [("number", "set_value", 20.0)], "50 is not what EM wants, so 20 goes back"
    h.hass.value, h.hass.calls = "20.0", []
    h.setpoint(None)
    assert h.hass.calls == [], "20 already holds, nothing is written"


def _balance_kwh(sun: float, wind: float) -> float:
    # the house as air and mass from the practice values of heating season 2025-26, a flat price, 8 C outside, over the
    # production window of 192 quarters (a list sized for 96 crashed there): planned kWh over the two days
    h = _house()
    h.ua, h.ua_wind, h.sun_gain, h.q_int, h.ci, h.cm, h.him = 0.174, 0.0086, 0.0066, 0.39, 8.0, 12.5, 2.4
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None)
    lp.hass = None
    lp.temp, lp.wind, lp.sun = (lp.data(n, u, 7, source="meteo") for n, u in (("temp", "C"), ("wind", "m/s"), ("sun", "W/m2")))
    lp.temp.data[12:204], lp.wind.data[12:204], lp.sun.data[12:204] = 8.0, wind, sun
    lp.init([_Grid([0.3] * lp.horizon), h])
    x = asyncio.run(lp.run(datetime(2026, 11, 2, 12, 0)))
    assert x is not None and lp._last_lp_status == 0
    return float(x[h.power.index:h.power.index + lp.horizon].sum() * 0.25)


def test_house_balance_follows_sun_and_wind() -> None:
    # the same day costs less with sun on the house and more with wind on it
    grey = _balance_kwh(0.0, 3.0)
    assert grey > 0.0, "a grey day at 8 C needs heat"
    assert _balance_kwh(200.0, 3.0) < grey, "sun on the house saves heat"
    assert _balance_kwh(0.0, 10.0) > grey, "wind on the house costs heat"


def test_heat_pump_writes_the_plan_in_its_control_type() -> None:
    # 1.2 kW of 2.4 and a room planned 1.5 K over the floor: as percent, as watts, as the delta and as the setpoint inside the entity's range
    lp, grid, h, x = _house_lp([0.3] * 96, 5.0, datetime(2026, 6, 28, 12, 0))
    h.control_entity, lp.hass = "number.heat_pump", _Hass("0")
    h.power.dest.data[12:108], h.temp.dest.data[13], h.max_kw, h._tmin[0] = 0.0, 20.5, 2.4, 19.0
    h.power.dest.data[12:14] = 1.2  # a run of half an hour, the shortest the MHI makes
    for control, attributes, want in (("capacity", {"step": 1}, 50.0), ("power", {"step": 1, "unit_of_measurement": "W"}, 1200.0), ("delta", {"step": 0.5}, 1.5), ("temperature", {"step": 0.5, "max": 20.0}, 20.0)):
        h.control, lp.hass.calls = control, []
        lp.hass.get = lambda entity_id, a=attributes: SimpleNamespace(state="0", attributes=a)
        h.entry_postrun(lp)
        assert lp.hass.calls == [("number", "set_value", want)], f"{control} must write {want}"
    # every quarter is written, also when the value did not change: a device falls back to its own curve without it
    h.entry_postrun(lp)
    assert lp.hass.calls == [("number", "set_value", 20.0)] * 2


def test_the_plan_only_keeps_runs_the_heat_pump_can_make() -> None:
    # a gap of one quarter is closed, small blocks go to fewer quarters at 0.6 kW or more with the same energy, a lone small quarter drops out
    p = np.array([0.0, 0.9, 0.9, 0.0, 0.9, 0.3, 0.0, 0.0, 0.5, 0.5, 0.4, 0.0, 0.0, 0.2, 0.0])
    out = HeatingHeatPump()._runs(p, np.full(len(p), 0.6))
    assert out[1:6] == pytest.approx([0.6] * 5)  # 3.0 kW-quarters over 0.9 0.9 0 0.9 0.3: the gap closed, five quarters of 0.6
    assert out[8:11] == pytest.approx([0.7, 0.7, 0.0])  # 1.4 kW-quarters: two quarters of 0.7
    assert out[13] == 0.0  # 0.2 kW alone is no run
    assert out.sum() == pytest.approx(p[:11].sum())


def test_free_solar_does_not_start_a_run() -> None:
    # 3 kW that nobody else can take: the tank is still only filled in its block
    h = _boiler(45.0)
    solar = [3.0] + [0.0] * 95
    _run(h, _week(), datetime(2026, 6, 28, 10, 0), solar)
    assert h._target == 0.0, "free solar is no reason to heat outside the block"


def test_the_disinfection_takes_the_cheapest_day_of_its_band() -> None:
    # band 7 +/- 1, so days 6, 7 and 8 after the last run; day 8 is the cheap one every time
    now = datetime(2026, 6, 28, 0, 0)
    for since, cheap_day, fires in ((6, 2, False), (7, 1, False), (8, 0, True)):
        h = _boiler(45.0)
        h.legionella = True
        h.legionella_at = now.timestamp() - since * 86400.0
        _run(h, _week(cheap_day=cheap_day), now)
        assert (h._target == 60.0) == fires, f"{since} days after the last run should fire: {fires}"
        assert h._disinfect == fires


def test_a_completed_disinfection_restarts_the_interval() -> None:
    now = datetime(2026, 6, 28, 0, 0)
    h = _boiler(45.0)
    h.legionella = True
    h.legionella_at = now.timestamp() - 8 * 86400.0
    _run(h, _week(), now)
    assert h._disinfect and h._target == 60.0
    h.on_temp(60.5)
    assert not h._disinfect and h._target == 0.0
    assert h.legionella_at > now.timestamp() - 86400.0, "the interval starts again from this run"


def test_the_house_fit_finds_a_known_house_back(monkeypatch) -> None:
    # 200 days of a known house on a setpoint between 19 and 20.5 C that changes per day, off for two hours every night, with sensor noise
    from homeassistant.components import recorder as rec
    from homeassistant.helpers import aiohttp_client
    ci, cm, him, ua, w, g, qi = 8.0, 12.5, 2.4, 0.18, 0.009, 0.006, 0.4
    rng, hours = np.random.default_rng(1), 200 * 24
    te = 6.0 + 6.0 * np.sin(np.arange(hours) / 24 / 30) + 3.0 * np.sin(np.arange(hours) * 2 * np.pi / 24)
    sun, wind = np.clip(400 * np.sin((np.arange(hours) % 24 - 6) * np.pi / 12), 0, None) * rng.uniform(0, 1, 200).round(1).repeat(24), rng.uniform(0, 8, hours)
    sp = np.repeat(rng.choice([19.0, 19.5, 20.0, 20.5], 200), 24)
    # the house hour by hour: a heat pump that follows the loss and corrects the room, off between 2 and 4 h
    t, m, room, heat = 19.5, 19.5, [], []
    for k in range(hours):
        q = 0.0 if k % 24 in (2, 3) else min(6.0, max(0.0, (ua + w * wind[k]) * (sp[k] - te[k]) - qi - g * sun[k] + 3.0 * (sp[k] - t)))
        room.append(t), heat.append(q)
        t, m = t + (him * (m - t) + (ua + w * wind[k]) * (te[k] - t) + q + g * sun[k] + qi) / ci, m + him * (t - m) / cm
    start = datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp()
    rows = lambda v: [{"start": start + 3600 * k, "mean": x} for k, x in enumerate(v)]
    # the power that heat took at the plan's COP: above 0.6 kW each extra kW gives 64 % of the heat
    power = [e if (e := q / _cop(o, r)) <= 0.6 else 0.6 + (e - 0.6) / 0.64 for q, o, r in zip(heat, te, room)]
    stats = {"power": rows([e * 1000 for e in power]), "room": rows(np.array(room) + rng.normal(0, 0.05, hours))}
    times = [datetime.fromtimestamp(start + 3600 * k, timezone.utc).replace(tzinfo=None).isoformat() for k in range(hours)]

    class _Rec:
        async def async_add_executor_job(self, fn, *args):
            return stats

    class _Get:
        async def __aenter__(self):
            return SimpleNamespace(json=lambda: asyncio.sleep(0, {"hourly": {"time": times, "temperature_2m": list(te), "shortwave_radiation": list(sun), "wind_speed_10m": list(wind)}}))
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(rec, "get_instance", lambda hass: _Rec())
    monkeypatch.setattr(aiohttp_client, "async_get_clientsession", lambda hass: SimpleNamespace(get=lambda *a, **k: _Get()))
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: SimpleNamespace(state="0", attributes={"unit_of_measurement": "W"})))
    fit = asyncio.run(fit_house(hass, "power", "room", [], 52.0, 5.0))
    assert fit is not None
    assert (fit[3], fit[5], fit[6]) == pytest.approx((ua, g, qi), rel=0.15)  # loss, sun and free heat
    assert fit[4] == pytest.approx(w, rel=0.3)  # wind only comes from the cloudy days, so it is the loosest
    assert fit[0] + fit[1] == pytest.approx(ci + cm, rel=0.15)  # air plus mass
    assert fit[0] == pytest.approx(ci, rel=0.2)  # air from the first hour of the cooldowns, which also carries the free heat and the error of ua

