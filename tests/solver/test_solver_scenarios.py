import asyncio
from datetime import datetime

import numpy as np
import pytest

from custom_components.zenmaster.solver.solver import LpSolver, SolverData, SolverEntry
from custom_components.zenmaster.const import Phase


def _lp() -> LpSolver:
    return LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)


def _placement_lp(phase_limit: float = 17 / 3) -> LpSolver:
    # place_block operates on the phase loads + phase_limit_kw that run() normally sets up
    lp = _lp()
    lp.l1 = np.zeros(96)
    lp.l2 = np.zeros(96)
    lp.l3 = np.zeros(96)
    lp.globals["phase_limit_kw"] = phase_limit
    lp.price_buy = SolverData("price_buy", "euro", data_size=108)
    return lp


def test_place_block_cheapest_window() -> None:
    lp = _placement_lp()
    lp.price_buy.data[12:108] = 1.0
    lp.price_buy.data[12 + 40] = lp.price_buy.data[12 + 41] = 0.1  # cheapest 2-slot window starts at 40
    start = lp.place_block(Phase.l1, [1.0, 1.0])
    assert start == 40
    assert lp.l1[40] == pytest.approx(1.0) and lp.l1[41] == pytest.approx(1.0)


def test_place_block_respects_window_end() -> None:
    lp = _placement_lp()
    lp.price_buy.data[12:108] = 1.0
    lp.price_buy.data[12 + 40] = lp.price_buy.data[12 + 41] = 0.1  # cheap, but outside the allowed window
    start = lp.place_block(Phase.l1, [1.0, 1.0], 3)  # must fit before slot 3
    assert start is not None and start <= 1


def test_place_block_skips_over_phase_limit() -> None:
    lp = _placement_lp()
    lp.price_buy.data[12:108] = 1.0
    lp.price_buy.data[12 + 5] = lp.price_buy.data[12 + 6] = 0.1  # cheapest window, but slot 5 is full
    lp.l1[5] = 17 / 3  # no headroom at slot 5
    start = lp.place_block(Phase.l1, [1.0, 1.0])
    assert start == 6  # next-cheapest window that still fits
    assert lp.l1[6] == pytest.approx(1.0) and lp.l1[7] == pytest.approx(1.0)


def test_place_block_no_window_returns_none() -> None:
    lp = _placement_lp()
    assert lp.place_block(Phase.l1, [1.0, 1.0], 1) is None


def test_solve_grid_supplies_committed_load() -> None:
    # the LP balance forces grid supply to cover the deterministically placed phase load, cheapest slots first
    lp = _lp()

    class _GridLoad(SolverEntry):
        def entry_config(self, lp: LpSolver) -> None:
            lp.globals["phase_limit_kw"] = 17 / 3
            self.grid = lp.data("grid_buy", "kW")
            self.buy = lp.column(tags=("supply",), dest=self.grid)

        async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
            self.buy.costs[:] = self.price
            self.plan = [0.0] * 96
            self.plan[40] = self.plan[41] = 4.0  # 2.0 kWh committed on L1 over two slots
            for t in range(96):
                lp.l1[t] += self.plan[t]

    e = _GridLoad()
    e.price = [1.0] * 96
    e.price[40] = e.price[41] = 0.1
    lp.init([e])
    x = asyncio.run(lp.run(datetime(2026, 6, 28, 12, 0)))
    assert x is not None and lp._last_lp_status == 0
    assert np.allclose(e.grid.data[12:108], e.plan)  # grid buys exactly the placed load
    assert sum(e.plan) * 0.25 == pytest.approx(2.0)


def test_solve_prefers_free_solar() -> None:
    # two supply columns (priced grid + free solar) competing to meet the load: solar maxes first
    lp = _lp()

    class _Stacked(SolverEntry):
        def entry_config(self, lp: LpSolver) -> None:
            lp.globals["phase_limit_kw"] = 17 / 3
            self.grid = lp.data("grid_buy", "kW")
            self.solar = lp.data("solar", "kW")
            self.buy = lp.column(cost=0.3, tags=("supply",), dest=self.grid)
            self.pv = lp.column(cost=0.0, tags=("supply",), dest=self.solar)

        async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
            lp.l1[10:14] = 4.0  # 4 kW load on L1 for 4 quarters
            self.pv.upper[:] = 2.5  # free solar capped at 2.5 kW

    e = _Stacked()
    lp.init([e])
    x = asyncio.run(lp.run(datetime(2026, 6, 28, 12, 0)))
    assert x is not None and lp._last_lp_status == 0
    assert np.allclose(e.grid.data[12 + 10:12 + 14], 1.5)  # grid covers the 1.5 kW deficit
    assert np.allclose(e.solar.data[12 + 10:12 + 14], 2.5)  # solar maxed first, it is free
    assert e.grid.data[12] == pytest.approx(0.0)  # no load at slot 0, no grid draw
