import asyncio
from datetime import datetime

import numpy as np
import pytest

from custom_components.energymaster.const import Phase
from custom_components.energymaster.solver.solver import LpSolver, SolverEntry
from custom_components.energymaster.solver.heating import Heating


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


def _boiler() -> Heating:
    h = Heating()
    h.name = "tank"
    h.id = "tank"
    h.kind = "boiler"  # hard band, loses heat to a fixed ambient
    h.phase = Phase.l1
    h.temp_entity = ""
    h.max_power = 2.5
    h.t_min = 50.0
    h.t_max = 55.0
    h.ambient = 45.0
    h.ramp_kw = 1.0
    h.slope = 0.12
    h.tau = 40.0
    h.icept = 0.0
    h._temp0 = 52.0
    return h


def test_boiler_holds_band_and_heats_cheap() -> None:
    price = [0.1] * 48 + [1.0] * 48  # first half of the day cheap, second half expensive
    grid = _Grid(price)
    h = _boiler()
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)
    lp.hass = None  # offline: skips calibration in entry_prerun
    lp.init([grid, h])
    x = asyncio.run(lp.run(datetime(2026, 6, 28, 0, 0)))

    assert x is not None and lp._last_lp_status == 0
    temp = h.temp.dest.data[12:108]
    assert temp[0] == pytest.approx(52.0)  # slot 0 pinned to the live temperature
    assert np.all(temp >= 50.0 - 1e-6) and np.all(temp <= 55.0 + 1e-6)  # hard DHW band

    power = x[h.power.index:h.power.index + 96]
    assert np.allclose(grid.out.data[12:108], power)  # grid supplies exactly the heating draw
    assert np.max(np.abs(np.diff(power))) <= h.ramp_kw + 1e-6  # ramp limit respected
    assert power[:48].sum() > power[48:].sum()  # energy shifted into the cheap half
    assert power.sum() > 0.0  # it did heat to hold the floor


def _house() -> Heating:
    h = Heating()
    h.name = "house"
    h.id = "house"
    h.kind = "house"  # soft comfort floor, loses heat to the outdoor forecast
    h.phase = Phase.l1
    h.temp_entity = ""
    h.max_power = 3.0
    h.t_min = 20.0
    h.t_max = 22.0
    h.t_abs_min = 16.0
    h.t_min_night = 18.0
    h.t_target = 21.0
    h.ramp_kw = 1.0
    h.slope = 0.12
    h.tau = 40.0
    h.icept = 0.0
    h._temp0 = 20.5
    return h


def _house_lp(price: list[float], outdoor: float, now: datetime):
    grid = _Grid(price)
    h = _house()
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)
    lp.hass = None
    lp.temp = lp.data("temp", "C", 7, source="meteo")  # outdoor forecast the house loses heat to
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
    assert np.all(temp >= 16.0 - 1e-6)  # hard absolute floor never breached

    slack = x[h.slack.index:h.slack.index + 96]
    assert np.all(slack < 1e-3)  # comfort floor held, no shortfall needed

    power = x[h.power.index:h.power.index + 96]
    assert power[:48].sum() > power[48:].sum()  # pre-heats the building mass while energy is cheap


def test_house_night_setback() -> None:
    price = [0.3] * 96
    lp, grid, h, x = _house_lp(price, 5.0, datetime(2026, 6, 28, 0, 0))  # now = midnight UTC

    assert x is not None and lp._last_lp_status == 0
    assert h._tmin[0] == pytest.approx(18.0)  # 00:00 -> night floor
    assert h._tmin[24] == pytest.approx(20.0)  # 06:00 -> day floor
    assert h._tmin[40] == pytest.approx(20.0)  # 10:00 -> day floor
    assert h._tmin[88] == pytest.approx(18.0)  # 22:00 -> night floor
