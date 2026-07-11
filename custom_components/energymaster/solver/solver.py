from __future__ import annotations

from enum import IntEnum
import logging
from collections.abc import Callable
from typing import Any
from datetime import datetime

import numpy as np
import highspy

from ..const import Phase

_LOGGER = logging.getLogger(__name__)

Bound = float | Callable[[int], float]  # a bound or coefficient: fixed number or f(t)

_STORE = "energymaster_calibration"  # one shared .storage file holding every entry's persisted params


class SolverData:
    def __init__(self, id, uom, name="", source="derived", data_size=192):
        self.id = id  # stable key for the translated label (e.g. "house_power")
        self.uom = uom
        self.name = name  # device display name to tell instances apart ("" for singletons like solar/price)
        self.source = source
        self.data = np.zeros(data_size)


class EntryStatus(IntEnum):
    none = 1
    armed = 2
    planned = 3
    running = 4
    ready = 5
    aborted = 6


class SolverEntry:
    def __init__(self):
        self.status: EntryStatus = EntryStatus.none
        self.phase: Phase = Phase.all
        self.power_entity: str = ""
        self.hass = None
        self.busy = False
        self._seen_below: bool = False  # power has been observed below the on-threshold
        self._running_since: float | None = None  # when a witnessed below->above start happened

    def entry_config(self, lp: LpSolver):
        pass

    async def entry_prerun(self, lp: LpSolver, now: datetime):
        pass

    def entry_postrun(self, lp: LpSolver):
        pass

    async def restore(self, hass) -> None:
        pass  # load persisted params (e.g. calibration, profiles) back into the entry; called once before init

    # Persisted params (e.g. calibration) for this entry, in the one shared HA Store, keyed by type + id.
    async def save_params(self, hass, params: dict) -> None:
        from homeassistant.helpers.storage import Store

        store = Store(hass, 1, _STORE)
        data = await store.async_load() or {}
        data[f"{type(self).__name__}_{getattr(self, 'id', '')}"] = params
        await store.async_save(data)

    async def load_params(self, hass) -> dict:
        from homeassistant.helpers.storage import Store

        data = await Store(hass, 1, _STORE).async_load() or {}
        return data.get(f"{type(self).__name__}_{getattr(self, 'id', '')}", {})


class LinExpr:
    # a linear combination of LP columns: {column index -> coefficient}, built with +/-/* on column refs
    def __init__(self, terms: dict[int, float] | None = None) -> None:
        self.terms: dict[int, float] = dict(terms) if terms else {}

    def __add__(self, other: "LinExpr") -> "LinExpr":
        r = LinExpr(self.terms)
        for i, c in other.terms.items():
            r.terms[i] = r.terms.get(i, 0.0) + c
        return r

    def __sub__(self, other: "LinExpr") -> "LinExpr":
        r = LinExpr(self.terms)
        for i, c in other.terms.items():
            r.terms[i] = r.terms.get(i, 0.0) - c
        return r

    def __mul__(self, scalar: float) -> "LinExpr":
        return LinExpr({i: c * scalar for i, c in self.terms.items()})

    __rmul__ = __mul__

    def __neg__(self) -> "LinExpr":
        return LinExpr({i: -c for i, c in self.terms.items()})


class LpColumn:
    def __init__(self, index: int, cost: float, lo: float, hi: float, tags=(), dest=None):
        self.index = index
        self.cost = cost
        self.lo = lo
        self.hi = hi
        self.costs: np.array
        self.upper: np.array
        self.lower: np.array
        self.tags = set(tags)
        self.dest = dest  # SolverData that solve() writes the result back into

    def __getitem__(self, t) -> LinExpr:
        return LinExpr({self.index + t: 1.0})

    def init(self, lp: LpSolver):
        h = lp.horizon
        self.costs = lp.cost[self.index : self.index + h]
        self.costs[0:h] = self.cost
        self.lower = lp.lower[self.index : self.index + h]
        self.lower[0:h] = self.lo
        self.upper = lp.upper[self.index : self.index + h]
        self.upper[0:h] = self.hi


class LpSolver:
    def __init__(self, track: Callable[[str, callable]], state: Callable[[str, callable]], horizon: int = 192):
        self.horizon = horizon  # solve window in 15-min slots (192 = 48h); 12 history slots precede it
        self._data: list[SolverData] = []
        self._entries: list[SolverEntry] = []
        self.cost = []
        self._rows = []
        self._columns: list[LpColumn] = []
        self.track = track
        self.state = state
        self.globals: dict[str, Any] = {}  # shared blackboard; entries publish/read by name in entry_config
        self.price_buy: SolverData = None  # set by DynamicPrice, read by baseload
        self.price_sell: SolverData = None  # set by DynamicPrice, read by forecast
        self.temp: SolverData = None  # outdoor forecast, set by Forecast, read by heating
        self.solar: SolverData = None  # solar production forecast, set by Forecast
        self.l1: np.array
        self.l2: np.array
        self.l3: np.array
        self._last_slot: int | None = None  # quarter index of the last run, drives shift()

    def init(self, entries):
        # one-time: build columns + rows. The structure stays fixed for the whole run; bounds are lambdas.
        self._entries = entries
        self._columns = []
        self._rows = []
        self._data = []
        self.globals = {}
        for entry in self._entries:
            entry.entry_config(self)

        self.rowQuarter({"supply": 1.0, "draw": -1.0}, lo=lambda t: self.l1[t] + self.l2[t] + self.l3[t], hi=lambda t: self.l1[t] + self.l2[t] + self.l3[t])
        self.limit = self.globals["phase_limit_kw"]
        self.rowQuarter({"l1": 1.0, "all": 1.0 / 3}, hi=lambda t: self.limit - self.l1[t])
        self.rowQuarter({"l2": 1.0, "all": 1.0 / 3}, hi=lambda t: self.limit - self.l2[t])
        self.rowQuarter({"l3": 1.0, "all": 1.0 / 3}, hi=lambda t: self.limit - self.l3[t])

    async def run(self, now):
        # shift the history window one slot per elapsed quarter (re-runs within the same quarter don't shift)
        slot = int(now.timestamp()) // 900
        if self._last_slot is not None:
            for _ in range(min(max(0, slot - self._last_slot), 12)):
                self.shift()
        self._last_slot = slot

        # Create the arrays
        h = self.horizon
        self.l1 = np.zeros(h)
        self.l2 = np.zeros(h)
        self.l3 = np.zeros(h)
        self.loads = [[self.l1, self.l2, self.l3], [self.l1], [self.l2], [self.l3]]

        self.cost = np.zeros(len(self._columns) * h)
        self.lower = np.zeros(len(self._columns) * h)
        self.upper = np.full(len(self._columns) * h, highspy.kHighsInf)

        for c in self._columns:
            c.init(self)

        # per quarter: only re-evaluate and solve; columns/rows were already built once in init()
        for entry in self._entries:
            try:
                await entry.entry_prerun(self, now)
            except Exception:
                _LOGGER.exception("entry_prerun failed for %s", type(entry).__name__)

        if self.price_buy is not None and not self.price_buy.data[12 : 12 + h].any():
            _LOGGER.warning("no buy prices available, skipping solve")
            return None

        inf = highspy.kHighsInf
        start, index, value, row_lower, row_upper = [0], [], [], [], []
        for row, lo, hi in self._rows:
            for col, coeff in row.terms.items():
                index.append(col)
                value.append(coeff)
            start.append(len(value))
            lo_v = lo() if callable(lo) else lo
            hi_v = hi() if callable(hi) else hi
            row_lower.append(-inf if lo_v is None else lo_v)
            row_upper.append(inf if hi_v is None else hi_v)

        lp = highspy.HighsLp()
        lp.num_col_ = len(self.cost)
        lp.num_row_ = len(self._rows)
        lp.col_cost_ = self.cost
        lp.col_lower_ = self.lower
        lp.col_upper_ = self.upper
        lp.row_lower_ = np.asarray(row_lower, float)
        lp.row_upper_ = np.asarray(row_upper, float)
        lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
        lp.a_matrix_.start_ = np.asarray(start)
        lp.a_matrix_.index_ = np.asarray(index)
        lp.a_matrix_.value_ = np.asarray(value, float)

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.setOptionValue("time_limit", 30.0)
        h.passModel(lp)
        h.run()
        self._last_lp_status = 0 if h.getModelStatus() == highspy.HighsModelStatus.kOptimal else 1
        if self._last_lp_status != 0:
            _LOGGER.warning("LP solve not optimal: %s", h.getModelStatus())
            return None
        x = np.asarray(h.getSolution().col_value, float)
        for col in self._columns:
            if col.dest is not None:
                col.dest.data[12 : 12 + self.horizon] = x[col.index : col.index + self.horizon]

        for entry in self._entries:
            entry.entry_postrun(self)
        return x

    def phase_room(self, phase: Phase, idx: int, total: float) -> float:
        factor = 3 if phase == Phase.all else 1
        room = min(self.limit - load[idx] for load in self.loads[phase]) * factor
        return max(0.0, min(total, room * 0.25))  # kW headroom -> kWh per quarter, never negative

    def phase_add(self, phase: Phase, ld: np.array, i1: int = 0, i2: int | None = None):
        factor = 3 if phase == Phase.all else 1
        i2 = self.horizon if i2 is None else i2
        for load in self.loads[phase]:
            for i in range(i1, i2):
                load[i] += ld[i] / factor

    def data(self, id, uom, day: int = 1, name="", source="lp_result") -> SolverData:
        # 12 history slots + at least the solve horizon (forecast asks for more via day)
        data = SolverData(id, uom, name, source, max(day * 96 + 12, self.horizon + 12))
        data.index = len(self._data)
        self._data.append(data)
        return data

    def plan(self) -> list[dict]:
        # UI get_plan: elke SolverData als 96-slot serie, slot 0 = nu (index 12)
        return [{"id": sd.id, "uom": sd.uom, "source": sd.source, "values": list(sd.data)} for sd in self._data]

    def column(self, cost: float = 0.0, lo: float = 0.0, hi: float = highspy.kHighsInf, tags=(), dest=None):
        col = LpColumn(len(self._columns) * self.horizon, cost, lo, hi, tags, dest)
        self._columns.append(col)
        return col

    def tagged(self, tag):
        return [c for c in self._columns if tag in c.tags]

    def shift(self):
        # every SolverData shifts one quarter (1 slot) back, whole array, last slot cleared
        for sd in self._data:
            sd.data[:-1] = sd.data[1:]
            sd.data[-1] = 0

    @staticmethod
    def _bound(b, t):
        # bound as a 0-arg thunk (deferred until solve) if it is f(t), otherwise the number/None itself
        return (lambda: b(t)) if callable(b) else b

    def rowQuarter(self, factors: dict, lo: Bound | None = None, hi: Bound | None = None) -> None:
        # one row per quarter: sum factor*column. A str key expands to every column with that tag; an LpColumn key is used directly.
        cols = []
        for key, f in factors.items():
            cols += [(c, f) for c in self.tagged(key)] if isinstance(key, str) else [(key, f)]
        if not cols:
            return
        for t in range(self.horizon):
            expr = LinExpr()
            for c, f in cols:
                expr += f * c[t]
            self._rows.append((expr, self._bound(lo, t), self._bound(hi, t)))

    def rowDelta(self, expr: Callable[[int, int], LinExpr], const: Bound = 0.0, lo: Bound | None = None, hi: Bound | None = None) -> Callable[[], None]:
        # couple two quarters over the 24h horizon: equality expr(t+1,t)==const, or inequality lo/hi
        # returns build() that re-evaluates expr in place; call it in prerun when coefficients change
        start = len(self._rows)
        n = self.horizon - 1
        def build() -> None:
            for t in range(n):
                row = expr(t + 1, t)
                if lo is None and hi is None:
                    b = self._bound(const, t)
                    self._rows[start + t] = (row, b, b)
                else:
                    self._rows[start + t] = (row, self._bound(lo, t), self._bound(hi, t))
        self._rows.extend([None] * n)
        build()
        return build

    def place_block(self, phase: Phase, powerprofile: list[float], latest: int = 96) -> int | None:
        # place a fixed appliance cycle in its cheapest buy-price window, keeping every phase load under the limit, then commit it
        price = self.price_buy.data[12 : 12 + self.horizon]
        limit = self.globals["phase_limit_kw"]
        loads = [self.l1, self.l2, self.l3] if phase == Phase.all else [(None, self.l1, self.l2, self.l3)[phase]]
        length = len(powerprofile)
        best_cost, best_start = float("inf"), None
        for start in range(latest - length + 1):
            if any(load[start + step] + powerprofile[step] > limit for load in loads for step in range(length)):
                continue
            window_cost = sum(powerprofile[step] * price[start + step] for step in range(length))
            if window_cost < best_cost:
                best_cost, best_start = window_cost, start
        if best_start is not None:
            for load in loads:
                for step in range(length):
                    load[best_start + step] += powerprofile[step] / (3 if phase == Phase.all else 1)
        return best_start
