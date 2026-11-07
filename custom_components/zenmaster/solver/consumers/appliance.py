from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import numpy as np

from ..solver import Action, EntryStatus, LpSolver, SolverEntry
from ...config import ConfigField
from ...const import Phase, watt_factor
from ...consumer import ConsumerDevice

_ON_THRESHOLD_W = 10.0  # consumption above this = the appliance is running
_INTERRUPT_WINDOW_S = 120.0  # only interrupt within this many seconds of a witnessed start
_DONE_S = 600.0  # idle this long ends a running cycle; measured pauses inside a cycle reach 300 s
_QUIET_S = 2700.0  # rest before a rise counts as a start; measured: pauses inside a cycle reach 25 min, real starts follow 115 min or more
_QUIET_MAX_S = 5400.0  # ceiling for the learned value, so one outlier cannot block arming altogether
_EVENING_MIN = 17 * 60  # armed at or after this, or before _MORNING_MIN: the cycle must be done by 08:00
_MORNING_MIN = 8 * 60  # armed after this: done within four hours and never past 17:00, so it stays clear of dinner
_DEFAULTS = {"dishwasher": (0.9, 7), "washingmachine": (0.3, 7), "dryer": (1.6, 7)}  # kW and quarters per kind, measured here and rounded: used while power_rating or cycle_duration_slots is not set


def _time_to_slot(time_str: str) -> int | None:
    try:
        h, m = time_str.split(":")[:2]  # "HH:MM", or "HH:MM:SS" the way HA's time entity keeps it
        return int(h) * 4 + int(m) // 15
    except (ValueError, AttributeError):
        return None


class Appliance(SolverEntry, ConsumerDevice):
    # A schedulable on/off appliance (dishwasher, dryer, washing machine, or any custom load). `kind` is a
    # label only; the behaviour is identical: the appliance places its cycle in its cheapest window,
    # arming via a witnessed power rise and acting on start/stop/skip commands.
    # Fields without a cast are settings: entities on the device, kept by HA (see ConsumerDevice).
    CONFIG = [
        ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True, hidden=True),
        ConfigField("name", {"text": {}}),  # the subentry title: HA names the device and its entities after it
        ConfigField("kind", {"select": {"options": [{"value": "dishwasher", "label": "Vaatwasser"}, {"value": "dryer", "label": "Droger"}, {"value": "washingmachine", "label": "Wasmachine"}, {"value": "custom", "label": "Overig"}]}}, cast=str, readonly=True),
        ConfigField("armed", {"boolean": {}}, cast=bool, hidden=True),
        ConfigField("armed_at", {"number": {}}, cast=float, required=False, hidden=True),
        ConfigField("switch_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("power_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("manage", {"boolean": {}}, section_key="behaviour"),
        ConfigField("latest_start_time", {"time": {}}, section_key="behaviour"),
        ConfigField("power_rating", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kW"}}, section_key="expert"),
        ConfigField("cycle_duration_slots", {"number": {"mode": "box"}}, section_key="expert"),
        ConfigField("phase", {"select": {"options": [{"value": "0", "label": "Alle fasen"}, {"value": "1", "label": "L1"}, {"value": "2", "label": "L2"}, {"value": "3", "label": "L3"}]}}, section_key="expert"),
        ConfigField("state", {"entity": {}}, readonly=True),
    ]

    # the override (manage) is a switch on the device; these are the moment-to-moment actions
    ACTIONS = [
        Action("start", "Start", "play", ("none", "armed", "planned", "ready")),
        Action("stop", "Stop", "stop", ("running",)),
        Action("skip", "Overslaan", "skip", ("armed", "planned")),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.id = ""
        self.kind = "custom"
        self.switch_entity = ""
        self.power_rating = 0.0
        self.cycle_duration_slots = 0
        self.latest_start_time = ""
        self.armed = False
        self.armed_at = 0.0  # when the arming happened; without a fixed time it sets the deadline, so it must survive a restart
        self.phase = Phase.all
        self._profile = None          # learned per-slot kW cycle profile (None -> flat power_rating)
        self._last_cal: datetime | None = None
        self._idle_since: float | None = None  # when the power dropped below the threshold while busy
        self._quiet_since: float | None = None  # when the power last dropped below the threshold, busy or not
        self._first = True  # no reading yet: an appliance found idle counts as rested, so arming works right after a restart
        self._quiet_s = _QUIET_S  # learned per appliance by calibrate(): rest needed before a rise is a start
        self._arm_price: float | None = None  # price of the quarter this cycle was armed in
        self._cycle_kwh = 0.0  # measured energy of the running cycle
        self._cycle_cost = 0.0  # that energy priced at the quarter it was used in

    def release(self) -> None:
        self.armed = False  # hands off: nothing waits for a cheap window any more

    def entry_config(self, lp: LpSolver) -> None:
        self.data = lp.data(self.id + "_power", "kW", 2, source="lp")   # placed profile (12 history + 192)
        lp.track(self.power_entity, self.on_power, phase=self.phase)

    def on_power(self, watts: float, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        super().on_power(watts, now)  # the energy of this quarter
        if watts <= _ON_THRESHOLD_W:            # idle: rest long enough and a later rise counts as a start
            if self._quiet_since is None:       # found idle on the first reading: it has been resting all along
                self._quiet_since = now - self._quiet_s if self._first else now
            if self.busy and self._idle_since is None:
                self._idle_since = now
            if not self.busy and self.status is EntryStatus.running:
                self.status = EntryStatus.none  # it stopped drawing and we are not running a cycle on it
            self._first = False
            self._running_since = None
            return
        self._first = False
        rested = now - self._quiet_since if self._quiet_since is not None else 0.0
        self._quiet_since = self._idle_since = None
        if self.armed and not self.busy and self.data.data[12] > 0:  # it really draws now: the planned cycle has begun
            self.busy = True
            self.status = EntryStatus.running
            return
        if rested >= self._quiet_s:             # below -> above after a real rest: witnessed start
            self._running_since = now
        if not self.manage or self.busy or self._running_since is None or now - self._running_since > _INTERRUPT_WINDOW_S:
            if self.status is EntryStatus.none:
                self.status = EntryStatus.running  # it draws while we plan nothing: say so instead of "off"
            return                              # hands off / busy / a pause inside a cycle / past the 2-min window
        self.armed = True
        self.armed_at = now
        self.status = EntryStatus.armed
        if self.switch_entity:
            self.hass.async_create_task(self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self.switch_entity}, blocking=False))

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        # weekly (and cold-start) recalibration of the cycle's power profile from history
        if lp.hass is not None and (self._last_cal is None or (now - self._last_cal).days >= 7):
            self._last_cal = now
            lp.hass.async_create_task(self.calibrate(lp, now))

        # book the measured energy at the price of the quarter it was used in, and remember the price
        # of the quarter the cycle was armed in: that is what it would have cost without shifting
        price = float(lp.price_buy.data[12]) if getattr(lp, "price_buy", None) is not None else 0.0
        if self.armed and self._arm_price is None:
            self._arm_price = price
        if self._pending_kwh:
            self._cycle_kwh += self._pending_kwh
            self._cycle_cost += self._pending_kwh * price
            self._pending_kwh = 0.0

        # a cycle that stayed idle long enough is done: not busy, and no longer armed for a new plan
        if self.busy and self._idle_since is not None and time.time() - self._idle_since >= _DONE_S:
            self.busy = self.armed = False
            self._idle_since = None
            self.status = EntryStatus.none
            if self._cycle_kwh > 0 and self._arm_price is not None and (booked := lp.globals.get("savings_pending")) is not None:
                # positive when the plan moved the cycle to a cheaper moment, negative when it did not
                booked.append(self._cycle_kwh * (self._arm_price - self._cycle_cost / self._cycle_kwh))
            self._arm_price, self._cycle_kwh, self._cycle_cost = None, 0.0, 0.0

        # armed means we cut the power; with the switch on there is nothing to wait for, so the flag is void.
        # unavailable says nothing (the switch comes back late after a restart), so then we neither void nor act.
        switch = lp.hass.states.get(self.switch_entity) if self.switch_entity and lp.hass is not None else None
        switch = switch.state if switch is not None else ""
        if self.armed and switch == "on":
            self.armed = False
            if not self.busy:
                self.status = EntryStatus.none

        # plan this cycle in its cheapest window when armed; baseload has already seeded the phase loads
        self.data.data[12:] = 0.0
        if self.armed:
            # without a learned profile a flat cycle; a rating or duration that is not set takes the default of its kind
            kw, slots = _DEFAULTS.get(self.kind, (0.0, 0))
            profile = self._profile or [self.power_rating if self.power_rating > 0 else kw] * (self.cycle_duration_slots if self.cycle_duration_slots > 0 else slots)
            slot = _time_to_slot(self.latest_start_time) if self.latest_start_time else None
            if slot is not None:
                latest = (slot - (now.hour * 60 + now.minute) // 15) % 96 or 96  # wall-clock slot -> slots from now
            else:
                # no fixed time: the moment of arming sets the deadline. In the evening the cycle must be done by
                # 08:00; during the day within four hours and never past 17:00, so it cannot run into cooking.
                armed = datetime.fromtimestamp(self.armed_at, tz=now.tzinfo) if self.armed_at else now
                mins = armed.hour * 60 + armed.minute
                if mins >= _EVENING_MIN or mins < _MORNING_MIN:
                    day = armed + timedelta(days=1) if mins >= _EVENING_MIN else armed
                    deadline = day.replace(hour=8, minute=0, second=0, microsecond=0)
                else:
                    deadline = min(armed + timedelta(hours=4), armed.replace(hour=17, minute=0, second=0, microsecond=0))
                latest = max(len(profile), int((deadline - now).total_seconds() // 900))  # place_block wants slots to be finished in
            start = lp.place_block(self.phase, profile, latest)
            if start is not None:
                self.data.data[12 + start : 12 + start + len(profile)] = profile
        # the plan wants it on now: keep asking until the appliance actually draws, so a command lost while the
        # switch was unavailable is simply sent again. busy follows the measurement, in on_power.
        if self.manage and self.armed and not self.busy and self.data.data[12] > 0 and (switch == "off" or not self.switch_entity):
            self.status = EntryStatus.planned  # running only once power is actually measured, in on_power
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
        elif action in ("arm_off", "arm_on"):
            self.manage = action == "arm_on"  # the override itself: status and release follow from the setter

    async def restore(self, hass) -> None:
        params = await self.load_params(hass)
        if profile := params.get("profile"):
            self._profile = profile
            self.cycle_duration_slots = len(profile)
        self._quiet_s = float(params.get("quiet_s") or _QUIET_S)

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
        factor = watt_factor(state, self.phase) / 1000.0  # the reading in kW, whatever unit the sensor uses
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

        # longest silence that still sat inside one cycle (gaps up to 90 min join two runs); twice that
        # is the rest this appliance must show before a rise may count as a start
        longest, gap, started = 0, 0, False
        for kw in series:
            if kw > _ON_THRESHOLD_W / 1000.0:
                if started and 0 < gap <= 6:
                    longest = max(longest, gap)
                started, gap = True, 0
            elif started:
                gap += 1
                if gap > 6:
                    started = False
        self._quiet_s = min(_QUIET_MAX_S, max(_QUIET_S, longest * 15 * 60 * 2.0))
        await self.save_params(lp.hass, {"profile": self._profile, "quiet_s": self._quiet_s})
