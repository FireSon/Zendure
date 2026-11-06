from __future__ import annotations
import logging
from datetime import datetime, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period

from .solver import LpSolver, SolverEntry
from ..const import Phase, watt_factor

_LOGGER = logging.getLogger(__name__)


# Provides the per-phase measured background house load (via load_history) and seeds lp.l1/l2/l3 from it each tick.
class BaseLoad(SolverEntry):
    def entry_config(self, lp: LpSolver):
        # measured background load per phase (kW), refreshed daily by load_history; 12 history + 192 future slots
        self.loads = [
            None,
            lp.data("load_l1", "kW", 2, source="ha"),
            lp.data("load_l2", "kW", 2, source="ha"),
            lp.data("load_l3", "kW", 2, source="ha"),
        ]
        self.load = lp.data("load", "kW", 2, source="ha")  # aggregate house load (sum l1/l2/l3) for UI/plan
        self._loaded = None  # local date load_history last ran (None = not yet)

    async def entry_prerun(self, lp: LpSolver, now: datetime):
        # Per tick: refresh the daily background load, then seed the per-phase load buffers from it; consumers schedule on top in their own prerun.
        today = dt_util.as_local(now).date()
        if self._loaded is None:
            await self.load_history(lp, now)
        if self._loaded != today:  # cold start, then again at local midnight
            lp.hass.async_create_task(self.load_history(lp, now))

        end = 12 + lp.horizon
        lp.l1[0:lp.horizon] = self.loads[1].data[12:end]
        lp.l2[0:lp.horizon] = self.loads[2].data[12:end]
        lp.l3[0:lp.horizon] = self.loads[3].data[12:end]

    def entry_postrun(self, lp: LpSolver):
        # aggregate house load for the UI, after every consumer's prerun added its plan to l1/l2/l3
        self.load.data[12 : 12 + lp.horizon] = lp.l1 + lp.l2 + lp.l3

    async def load_history(self, lp, now):
        self._loaded = dt_util.as_local(now).date()

        # 28-day weekday-weighted per-phase house load: house = P1_net + roof_PV + battery AC net - modelled consumers; run once a day.
        p1 = lp.globals.get("p1")
        if not p1:
            return
        slots = (p1["l1"], p1["l2"], p1["l3"])  # each: [(entity_id, signed watt factor)]
        bufs = (self.loads[1].data, self.loads[2].data, self.loads[3].data)
        solar_ac = lp.globals.get("solar_ac", [])  # AC roof PV entity ids (Zendure DC solar excluded)
        batteries = lp.globals.get("batteries", [])  # PowerDevice list; home_output/home_input are AC W

        def eid(dev, attr):
            return getattr(dev.entities.get(attr), "entity_id", None)

        p1_ids = {eid for s in slots for eid, _ in s}
        batt_ids = {i for d in batteries for i in (eid(d, "home_output"), eid(d, "home_input")) if i}
        cons_ids = {e.power_entity for e in lp._entries if e.power_entity}
        all_ids = p1_ids | set(solar_ac) | batt_ids | cons_ids
        if not p1_ids:
            return
        # the hourly statistics of every entity, bucketed per weekday and hour
        buckets = await self.history_buckets(lp, p1_ids, all_ids)

        def avg(sid, wd, h):
            # weekday-weighted mean kW for (weekday, hour): the matching weekday counts double
            s = buckets.get(sid)
            if not s:
                return 0.0
            sa = sum(s[(d, h)][0] for d in range(7))
            ca = sum(s[(d, h)][1] for d in range(7))
            sw, cw = s[(wd, h)]
            return (sa + sw) / (ca + cw) if (ca + cw) else 0.0

        base = dt_util.as_local(now)
        for ph in range(3):
            slot = slots[ph]
            cons = [(e.power_entity, 1.0) for e in lp._entries if e.power_entity and e.phase - 1 == ph]
            cons += [(e.power_entity, 1.0 / 3) for e in lp._entries if e.power_entity and e.phase == Phase.all]
            # battery net on this phase: a phase-bound device counts fully, an all/unset device splits over 3
            batt = []
            for d in batteries:
                out, inp = eid(d, "home_output"), eid(d, "home_input")
                if not out or not inp:
                    continue
                dph = getattr(d, "power_phase", None)
                weight = 1.0 if (dph is not None and int(dph) - 1 == ph) else (1.0 / 3 if (dph is None or int(dph) == 0) else 0.0)
                if weight:
                    batt.append((out, inp, weight))
            buf = bufs[ph]
            buf[12:] = 0.0
            for k in range((len(buf) - 12) // 4):  # hours from now
                t = base + timedelta(hours=k)
                wd, h = t.weekday(), t.hour
                house = sum(sign * avg(eid, wd, h) for eid, sign in slot)
                house += sum(avg(s, wd, h) for s in solar_ac) / 3.0  # roof PV split evenly (no per-panel phase)
                house += sum((avg(out, wd, h) - avg(inp, wd, h)) * w for out, inp, w in batt)  # battery AC net
                bg = max(0.0, house - sum(abs(avg(c, wd, h)) * w for c, w in cons))  # abs: a reversed consumer meter must still subtract
                for q in range(4):  # one hourly value fills 4 quarters
                    buf[12 + k * 4 + q] = bg

    async def history_buckets(self, lp, p1_ids, all_ids):
        # 28 days of hourly means per entity in kW, summed and counted per (weekday, hour-of-day)
        start, end = dt_util.utcnow() - timedelta(days=28), dt_util.utcnow()
        inst = get_instance(lp.hass)
        stats = await inst.async_add_executor_job(statistics_during_period, lp.hass, start, end, all_ids, "hour", None, {"mean"})
        # each entity to kW: the P1 slots carry their own signed factor, the others get it from their unit and phase
        phase_of = {e.power_entity: e.phase for e in lp._entries if e.power_entity}
        factor = {sid: 0.001 if sid in p1_ids else watt_factor(lp.hass.states.get(sid), phase_of.get(sid, Phase.all)) / 1000 for sid in all_ids}
        # bucket each entity by (weekday, hour-of-day), skipping hours without a mean
        buckets = {}
        for sid, rows in stats.items():
            s = {(wd, h): [0.0, 0] for wd in range(7) for h in range(24)}
            for r in rows:
                if (mean := r.get("mean")) is None:
                    continue
                t = dt_util.as_local(dt_util.utc_from_timestamp(r["start"]))
                b = s[(t.weekday(), t.hour)]
                b[0] += mean * factor.get(sid, 0.001)
                b[1] += 1
            buckets[sid] = s
        return buckets
