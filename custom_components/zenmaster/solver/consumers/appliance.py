from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import numpy as np
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period

from ..solver import Action, EntryStatus, LpSolver, SolverEntry
from ...config import ConfigField
from ...const import Phase, time_to_slot, watt_factor
from ...consumer import HEAD, MANAGE, TAIL, ConsumerDevice
from ...switch import HaSwitch

_ON_THRESHOLD_W = 10.0  # consumption above this = the appliance is running
_INTERRUPT_WINDOW_S = 120.0  # only interrupt within this many seconds of a witnessed start
_DONE_S = 600.0  # idle this long ends a running cycle; measured pauses inside a cycle reach 300 s
_QUIET_S = 2700.0  # rest before a rise counts as a start; measured: pauses inside a cycle reach 25 min, real starts follow 115 min or more
_QUIET_MAX_S = 5400.0  # ceiling for the learned value, so one outlier cannot block arming altogether
_EVENING_MIN = 17 * 60  # armed at or after this, or before _MORNING_MIN: the cycle must be done by 08:00
_MORNING_MIN = 8 * 60  # armed after this: done within four hours and never past 17:00, so it stays clear of dinner
_DIRECT_S = 900.0  # a short click on the room button: what is armed starts, and a new start runs at once, for this long
_DEFAULTS = {"dishwasher": (0.9, 7), "washingmachine": (0.3, 7), "dryer": (1.6, 7)}  # kW and quarters per kind, measured here and rounded: used while power_rating or cycle_duration_slots is not set
_DURATION_H = {"d": 24.0, "h": 1.0, "min": 1 / 60, "s": 1 / 3600}  # a duration sensor's unit in hours


class Appliance(SolverEntry, ConsumerDevice):
    # A schedulable on/off appliance (dishwasher, dryer, washing machine, or any custom load). `kind` is a
    # label only; the behaviour is identical: the appliance places its cycle in its cheapest window,
    # arming via a witnessed power rise and acting on start/stop/skip commands.
    # Fields without a cast are settings: entities on the device, kept by HA (see ConsumerDevice).
    CONFIG = [
        *HEAD,
        ConfigField("kind", {"select": {"options": [{"value": "dishwasher", "label": "Vaatwasser"}, {"value": "dryer", "label": "Droger"}, {"value": "washingmachine", "label": "Wasmachine"}, {"value": "custom", "label": "Overig"}]}}, cast=str, readonly=True),
        ConfigField("armed", {"boolean": {}}, cast=bool, hidden=True),
        ConfigField("armed_at", {"number": {}}, cast=float, required=False, hidden=True),
        ConfigField("switch_type", {"select": {"options": [{"value": "plug", "label": "Stekker"}, {"value": "pause", "label": "Pauze/hervat"}, {"value": "automation", "label": "Automation"}]}}, cast=str, section_key="link"),
        ConfigField("switch_entity", {"entity": {}}, cast=str, section_key="link", visible_when={"switch_type": ["plug", "pause"]}),
        ConfigField("pause_entity", {"entity": {"domain": "button"}}, cast=str, required=False, section_key="link", visible_when={"switch_type": "pause"}),
        ConfigField("status_entity", {"entity": {}}, cast=str, required=False, section_key="link", visible_when={"switch_type": "pause"}),
        ConfigField("power_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("duration_entity", {"entity": {"domain": "sensor", "device_class": "duration"}}, cast=str, required=False, section_key="link"),
        MANAGE,
        ConfigField("latest_start_time", {"time": {}}, section_key="behaviour"),
        ConfigField("power_rating", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kW"}}, section_key="expert"),
        ConfigField("cycle_duration_slots", {"number": {"mode": "box"}}, section_key="expert"),
        *TAIL,
    ]

    # the override (manage) is a switch on the device; these are the moment-to-moment actions
    ACTIONS = [
        Action("arm", "Klaarzetten", "check", ("none", "ready", "aborted")),
        Action("start", "Start", "play", ("none", "armed", "planned", "ready")),
        Action("stop", "Stop", "stop", ("running",)),
        Action("skip", "Overslaan", "skip", ("armed", "planned")),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.id = ""
        self.kind = "custom"
        self.switch_entity = ""
        self.switch_type = "plug"  # how a cycle is held and let run: the plug, the appliance's pause and resume buttons, or an automation
        self.pause_entity = ""
        self.status_entity = ""  # the appliance's own operation state: a program held in pause arms it
        self.duration_entity = ""  # the appliance's own estimate of the selected program's length, read at planning
        self.power_rating = 0.0
        self.cycle_duration_slots = 0
        self.latest_start_time = ""
        self.armed = False
        self.armed_at = 0.0  # when the arming happened; without a fixed time it sets the deadline, so it must survive a restart
        self.phase = Phase.all
        self._profile = None  # learned per-slot kW cycle profile (None -> flat power_rating)
        self._last_cal: datetime | None = None
        self._idle_since: float | None = None  # when the power dropped below the threshold while busy
        self._quiet_since: float | None = None  # when the power last dropped below the threshold, busy or not
        self._first = True  # no reading yet: an appliance found idle counts as rested, so arming works right after a restart
        self._quiet_s = _QUIET_S  # learned per appliance by calibrate(): rest needed before a rise is a start
        self._arm_price: float | None = None  # price of the quarter this cycle was armed in
        self._cycle_kwh = 0.0  # measured energy of the running cycle
        self._cycle_cost = 0.0  # that energy priced at the quarter it was used in
        self._direct_until = 0.0  # until when a start runs at once instead of waiting for its window

    def entity_create(self) -> None:
        # the direct-start switch beside the settings; it ends on its own after _DIRECT_S
        super().entity_create()
        HaSwitch(self, "direct", on_changed=self._set_direct)
        self.entity_update("direct", False)

    async def _set_direct(self, _entity, on) -> None:
        # on: what is armed starts now and a new start is not held back; off: back to the plan, what runs keeps running
        self._direct_until = time.time() + _DIRECT_S if on else 0.0
        if on and self.armed and not self.busy:
            await self.on_action(self, "start")

    def release(self) -> None:
        # hands off: nothing waits for a cheap window any more, and a switch the arming cut goes back on
        if self.armed and self._switched and self.hass is not None:
            self.hass.async_create_task(self._switch(self.hass, True))
        self.armed = False

    @property
    def _switched(self) -> bool:
        # ZenMaster switches the appliance itself; with an automation it only publishes the status
        return self.switch_type != "automation" and bool(self.switch_entity) and (self.switch_type != "pause" or bool(self.pause_entity))

    def _switch(self, hass, on: bool):
        # off holds the cycle, on lets it run: the plug, or the appliance's own pause and resume buttons
        if self.switch_type == "pause":
            return hass.services.async_call("button", "press", {"entity_id": self.switch_entity if on else self.pause_entity}, blocking=False)
        return hass.services.async_call("homeassistant", "turn_on" if on else "turn_off", {"entity_id": self.switch_entity}, blocking=False)

    def cycle(self) -> list[float]:
        # the cycle to plan: the learned profile, else flat on the rating and duration, a missing one the default of its kind
        kw, slots = _DEFAULTS.get(self.kind, (0.0, 0))
        profile = self._profile or [self.power_rating if self.power_rating > 0 else kw] * (self.cycle_duration_slots if self.cycle_duration_slots > 0 else slots)

        # the appliance's own estimate of the program sets the length: the profile stretched or shortened to it, its levels kept
        state = self.hass.states.get(self.duration_entity) if self.duration_entity and self.hass is not None else None
        try:
            n = round(float(state.state) * _DURATION_H[state.attributes.get("unit_of_measurement")] * 4) if state is not None else 0
        except (ValueError, KeyError):
            n = 0
        if profile and n > 0:
            profile = np.interp(np.linspace(0, len(profile) - 1, n), np.arange(len(profile)), profile).tolist()
        return profile

    def entry_config(self, lp: LpSolver) -> None:
        self.data = lp.data(self.id + "_power", "kW", 2, source="lp")   # placed profile (12 history + 192)
        lp.track(self.power_entity, self.on_power, phase=self.phase)

    def on_power(self, watts: float, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        super().on_power(watts, now)  # the energy of this quarter
        if watts <= _ON_THRESHOLD_W:  # idle: rest long enough and a later rise counts as a start
            if self._quiet_since is None:  # found idle on the first reading: it has been resting all along
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
        if rested >= self._quiet_s:  # below -> above after a real rest: witnessed start
            self._running_since = now
        if not self.manage or self.busy or self._running_since is None or now - self._running_since > _INTERRUPT_WINDOW_S or now < self._direct_until or not any(self.cycle()):
            if self.status is EntryStatus.none:
                self.status = EntryStatus.running  # it draws while we plan nothing: say so instead of "off"
            return  # hands off / busy / a pause inside a cycle / past the 2-min window / a cycle of unknown size
        self.armed = True
        self.armed_at = now
        self.status = EntryStatus.armed
        if self._switched:
            self.hass.async_create_task(self._switch(self.hass, False))
        # armed: kept at once, so a restart does not forget it, and planned at once, so the user sees the window now
        if self.on_action is not None:
            self.hass.async_create_task(self.on_action(self, "armed"))

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        # the direct start ends after its quarter of an hour; what started in it runs on
        if self._direct_until and time.time() >= self._direct_until:
            self._direct_until = 0.0
            self.entity_update("direct", False)

        # weekly (and cold-start) recalibration of the cycle's power profile from history
        if lp.hass is not None and (self._last_cal is None or (now - self._last_cal).days >= 7):
            self._last_cal = now
            lp.hass.async_create_task(self.calibrate(lp, now))

        # book the measured energy at the price of the quarter it was used in, and remember the price
        # of the quarter the cycle was armed in: that is what it would have cost without shifting
        price = float(lp.price_buy.data[lp.settle]) if lp.price_buy is not None else 0.0
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
        state = lp.hass.states.get(self.switch_entity) if self._switched and lp.hass is not None else None
        switch = state.state if state is not None else ""
        # turned on after the arming: the user let it run. Still on from before is our switch-off on its way
        if self.armed and switch == "on" and state.last_changed.timestamp() > self.armed_at:
            self.armed = False
            if not self.busy:
                self.status = EntryStatus.none

        # pause/resume: a program the appliance holds in pause is armed, as a witnessed start would be
        if self.switch_type == "pause" and self.manage and not self.armed and not self.busy and (s := lp.hass.states.get(self.status_entity) if self.status_entity and lp.hass is not None else None) is not None and s.state == "pause":
            self.armed, self.armed_at, self.status = True, time.time(), EntryStatus.armed

        # plan this cycle in its cheapest window when armed; baseload has already seeded the phase loads
        self.data.data[12:] = 0.0
        if self.armed:
            profile = self.cycle()
            slot = time_to_slot(self.latest_start_time) if self.latest_start_time else None
            if slot is not None:
                # started by then, so finished a cycle later: place_block wants the slots it must be finished in
                latest = ((slot - (now.hour * 60 + now.minute) // 15) % 96 or 96) + len(profile)  # wall-clock slot -> slots from now
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
        if self.manage and self.armed and not self.busy and self.data.data[12] > 0 and (switch == "off" or self.switch_type == "pause" or not self._switched):
            self.status = EntryStatus.planned  # running only once power is actually measured, in on_power
            if self._switched:
                await self._switch(lp.hass, True)

    async def command(self, hass, action: str) -> None:
        if action == "start":
            self.armed = False
            self.busy = True
            self.status = EntryStatus.running
            if self._switched:
                await self._switch(hass, True)
        elif action == "stop":
            self.armed = False
            self.busy = False
            self.status = EntryStatus.aborted
            if self._switched:
                await self._switch(hass, False)
        elif action == "arm":
            # armed by an automation or by hand, for an appliance that waits without drawing (a remote start): nothing is switched
            self.armed = True
            self.armed_at = time.time()
            self.status = EntryStatus.armed
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
        if not self.power_entity:
            return
        end = datetime.now(timezone.utc)
        stats = await get_instance(lp.hass).async_add_executor_job(
            statistics_during_period, lp.hass, end - timedelta(days=10), end, {self.power_entity}, "5minute", None, {"mean"})
        rows = [r["mean"] for r in stats.get(self.power_entity, []) if r.get("mean") is not None]
        state = lp.hass.states.get(self.power_entity)
        factor = watt_factor(state, self.phase) / 1000.0  # the reading in kW, whatever unit the sensor uses
        series = [float(np.mean(rows[i:i + 3])) * factor for i in range(0, len(rows), 3)]  # 5-min means -> 15-min kW
        runs, cur = [], []
        for kw in series:  # split into runs above the on-threshold
            if kw > _ON_THRESHOLD_W / 1000.0:
                cur.append(kw)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        runs = [r for r in runs if len(r) >= 2]  # ignore blips
        if len(runs) < 3:
            return
        n = int(np.median([len(r) for r in runs]))
        self._profile = [float(np.mean([r[j] for r in runs if len(r) > j])) for j in range(n)]
        self.cycle_duration_slots = n  # learned duration replaces the configured fallback

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
