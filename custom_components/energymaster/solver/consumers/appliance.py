from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import numpy as np

from ..solver import EntryStatus, LpSolver, SolverEntry
from ...const import Phase

_ON_THRESHOLD_W = 10.0  # verbruik hierboven = apparaat draait
_INTERRUPT_WINDOW_S = 120.0  # only interrupt within this many seconds of a witnessed start


def _time_to_slot(time_str: str) -> int | None:
    try:
        h, m = time_str.split(":")
        return int(h) * 4 + int(m) // 15
    except (ValueError, AttributeError):
        return None


class Appliance(SolverEntry):
    # A schedulable on/off appliance (dishwasher, dryer, washing machine, or any custom load). `kind` is a
    # label only; the behaviour is identical: the appliance places its cycle in its cheapest window,
    # arming via a witnessed power rise and acting on start/stop/skip commands.
    def __init__(self) -> None:
        super().__init__()
        self.id = ""
        self.kind = "custom"
        self.switch_entity = ""
        self.power_rating = 0.0
        self.cycle_duration_slots = 0
        self.latest_start_time = ""
        self.armed = False
        self.phase = Phase.all
        self._profile = None          # learned per-slot kW cycle profile (None -> flat power_rating)
        self._last_cal: datetime | None = None

    def entry_config(self, lp: LpSolver) -> None:
        self.data = lp.data(self.id + "_power", "kW", 2, source="lp")   # placed profile (12 history + 192)
        lp.track(self.power_entity, self.on_power)

    def on_power(self, watts: float, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if watts <= _ON_THRESHOLD_W:            # idle: a later rise counts as a witnessed start
            self._seen_below = True
            self._running_since = None
            return
        if self._seen_below:                    # below -> above: witnessed start
            self._running_since = now
            self._seen_below = False
        if self.busy or self._running_since is None or now - self._running_since > _INTERRUPT_WINDOW_S:
            return                              # busy / start not witnessed (cold start) / past the 2-min window
        self.armed = True
        self.status = EntryStatus.armed
        if self.switch_entity:
            self.hass.async_create_task(self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self.switch_entity}, blocking=False))

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        # weekly (and cold-start) recalibration of the cycle's power profile from history
        if lp.hass is not None and (self._last_cal is None or (now - self._last_cal).days >= 7):
            self._last_cal = now
            lp.hass.async_create_task(self.calibrate(lp, now))

        # plan this cycle in its cheapest window when armed; baseload has already seeded the phase loads
        self.data.data[12:] = 0.0
        if self.armed:
            profile = self._profile or [self.power_rating] * self.cycle_duration_slots
            if self.phase == Phase.all:
                profile = [p / 3 for p in profile]
            slot = _time_to_slot(self.latest_start_time) if self.latest_start_time else None
            latest = 96 if slot is None else ((slot - (now.hour * 60 + now.minute) // 15) % 96 or 96)  # wall-clock slot -> slots from now
            start = lp.place_block(self.phase, profile, latest)
            if start is not None:
                self.data.data[12 + start : 12 + start + len(profile)] = profile
        # arm -> busy: once the plan puts the cycle at 'now', actually start the appliance
        if self.armed and not self.busy and self.data.data[12] > 0:
            self.busy = True
            self.status = EntryStatus.running
            if self.switch_entity:
                await lp.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self.switch_entity}, blocking=False)

    async def command(self, hass, action: str) -> None:
        if action == "start":
            self.armed = False
            self.busy = True
            self.status = EntryStatus.running
            if self.switch_entity:
                await hass.services.async_call("homeassistant", "turn_on", {"entity_id": self.switch_entity}, blocking=False)
        elif action == "stop":
            self.armed = False
            self.busy = False
            self.status = EntryStatus.aborted
            if self.switch_entity:
                await hass.services.async_call("homeassistant", "turn_off", {"entity_id": self.switch_entity}, blocking=False)
        elif action == "skip":
            self.armed = False
            self.busy = False
            self.status = EntryStatus.none

    async def restore(self, hass) -> None:
        if profile := (await self.load_params(hass)).get("profile"):
            self._profile = profile
            self.cycle_duration_slots = len(profile)

    async def calibrate(self, lp: LpSolver, now: datetime) -> None:
        # learn the typical per-quarter kW cycle profile: detect runs above the on-threshold, average their shapes
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period
        if not self.power_entity:
            return
        end = datetime.now(timezone.utc)
        stats = await get_instance(lp.hass).async_add_executor_job(
            statistics_during_period, lp.hass, end - timedelta(days=10), end, {self.power_entity}, "5minute", None, {"mean"})
        rows = [r["mean"] for r in stats.get(self.power_entity, []) if r.get("mean") is not None]
        state = lp.hass.states.get(self.power_entity)
        factor = 1.0 if state is not None and state.attributes.get("unit_of_measurement") == "kW" else 0.001  # W assumed unless the sensor says kW
        series = [float(np.mean(rows[i:i + 3])) * factor for i in range(0, len(rows), 3)]   # 5-min means -> 15-min kW
        runs, cur = [], []
        for kw in series:                       # split into runs above the on-threshold
            if kw > _ON_THRESHOLD_W / 1000.0:
                cur.append(kw)
            elif cur:
                runs.append(cur); cur = []
        if cur:
            runs.append(cur)
        runs = [r for r in runs if len(r) >= 2]   # ignore blips
        if len(runs) < 3:
            return
        n = int(np.median([len(r) for r in runs]))
        self._profile = [float(np.mean([r[j] for r in runs if len(r) > j])) for j in range(n)]
        self.cycle_duration_slots = n            # learned duration replaces the configured fallback
        await self.save_params(lp.hass, {"profile": self._profile})
