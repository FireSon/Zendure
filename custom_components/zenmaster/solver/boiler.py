from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta

import numpy as np

from .heating import Heating, _AMBIENT, _AMBIENT_ENTITY, _BAND, _LINK, _MANAGE, _MAX_POWER, _TANK_K, _TAIL, _VOLUME
from .solver import Action, EntryStatus, LpSolver
from ..config import ConfigField
from ..const import Phase, watt_factor

# the tank as ten layers, the coil heating the bottom one; the sensor height as a fraction of the tank
_LAYERS = 10
_HEIGHT = {"onder": 0.15, "midden": 0.5, "boven": 0.85}
_TOP = 0.85  # the layer the minimum holds for
_BOTTOM = 0.15  # the height of the optional bottom sensor
_COLD = 15.0  # mains water
_STEP = 0.5  # one step of a tank sensor
_TAU = 110.0  # hours, until the nights measure one
_WEIGHT = 0.25  # running mean, about seven days
_NIGHTS = 10  # days of nights
_LONG_RUN = 0.5  # hours of running that mix the tank
# the standard COP curve (spectabel PW040): COP against the outside air at water 35 and 55
_CURVE_T = (-7.0, 0.0, 7.0, 15.0, 21.0)
_CURVE_W35 = (3.05, 3.59, 4.13, 4.60, 4.99)
_CURVE_W55 = (2.22, 2.62, 3.01, 3.35, 3.63)


class BoilerHeatPump(Heating):
    # Domestic hot water, outside the LP. Once a day, planned at midnight, the tank is filled in the block where a kWh of
    # heat costs least, only when the top of the model would not hold t_min until the next fill, and never at another
    # moment. A model of ten layers follows the tank, and where its top ends moves the daily energy. Only the disinfection
    # goes up to t_max. The target goes to the unit as its setpoint, rounded up to its step; we watch the tank.
    CAL_DAYS = 1
    ACTIONS = [Action("start", "Start", "play", ("none",))]  # a fill now, for one that was broken off
    TILES = ["temp_entity", "top_entity", "bottom_entity", "setpoint_entity"]
    CONFIG = [*_LINK, ConfigField("setpoint_entity", {"entity": {"filter": [{"domain": "number", "device_class": "temperature"}]}}, cast=str, required=False, section_key="link"),
              ConfigField("bottom_entity", {"entity": {"filter": [{"domain": "sensor", "device_class": "temperature"}]}}, cast=str, required=False, section_key="link"),
              ConfigField("top_entity", {"entity": {"filter": [{"domain": "sensor", "device_class": "temperature"}]}}, cast=str, required=False, section_key="link"),
              _MANAGE, *_BAND, _AMBIENT, _AMBIENT_ENTITY,
              ConfigField("legionella", {"boolean": {}}, section_key="behaviour"),
              ConfigField("legionella_days", {"number": {"mode": "box", "step": 1}}, section_key="behaviour"),
              ConfigField("legionella_band", {"number": {"mode": "box", "step": 1}}, section_key="behaviour"),
              ConfigField("legionella_at", {"number": {}}, cast=float, required=False, hidden=True),
              ConfigField("fill_at", {"number": {}}, cast=float, required=False, hidden=True),
              ConfigField("nights_from", {"number": {}}, cast=float, required=False, hidden=True),
              ConfigField("layers", {"text": {}}, cast=list, required=False, hidden=True),
              ConfigField("sensor_height", {"select": {"options": [{"value": "onder", "label": "Onder"}, {"value": "midden", "label": "Midden"}, {"value": "boven", "label": "Boven"}]}}, section_key="plate"),
              _MAX_POWER, _VOLUME, *_TAIL]

    def __init__(self) -> None:
        super().__init__()
        # the settings: the top never under t_min, t_max the limit of the unit and the disinfection
        self.t_min, self.t_max = 38.0, 60.0
        self.setpoint_entity = ""
        self.bottom_entity = self.top_entity = ""  # optional
        self.sensor_height = "midden"
        self.legionella = False
        self.legionella_days = 7  # NEN 1006: weekly
        self.legionella_band = 1  # days either side
        self.legionella_at = 0.0  # hidden, survives a restart
        self.fill_at = 0.0  # hidden: the block of today, 0 = none
        self.nights_from = 0.0  # hidden: its first reading
        self.layers: list = []  # hidden: the model, kept over a restart
        # what is learned: the loss, the COP level on the standard curve, and the kWh of heat a fill for a day covers
        self.tau = _TAU
        self.tau_bottom = self.tau_top = 0.0  # 0 = not measured, the tau of the sensor holds there
        self.level = 1.0
        self.draw = 2.0
        self._levels: list = []
        # the run under way: the target, the disinfection, the fill of the day, and when the compressor came on
        self._target = 0.0
        self._disinfect = False
        self._fill = 0.0
        self._fill_on: date | None = None
        self._fill_since = 0.0
        self._filled = False
        self._on_since = self._off_at = 0.0
        # the model: a temperature per layer from the bottom up, the room it loses to, the bottom sensor, its last step, the run it watches, and what the house drew since the block
        self._layers: np.ndarray | None = None
        self._room = self.ambient
        self._bottom: float | None = None
        self._top: float | None = None
        self._stepped = 0.0
        self._run: list | None = None
        self._tapped = 0.0

    async def command(self, hass, action: str) -> None:
        # start: the fill of the day again in this quarter; the solve that follows takes it up at the level the model gives
        if action == "start":
            self.fill_at = time.time() // 900 * 900

    def release(self) -> None:
        # hands the tank back to its own thermostat: no run in progress, setpoint parked
        self._target, self._fill = 0.0, 0.0
        self.setpoint(None)

    def entry_config(self, lp: LpSolver) -> None:
        # the phase from the config, the power column, and the sensors the tank and the model follow
        self.phase = Phase(self.phase)
        self.data = lp.data(f"{self.id}_power", "kW", 2, name=self.name, source="lp")
        lp.track(self.temp_entity, self.on_temp)
        lp.track(self.power_entity, self.on_power, phase=self.phase)
        lp.track(self.bottom_entity, self.on_bottom)
        lp.track(self.top_entity, self.on_top)
        # the readings as they are now: a sensor only reports a change, and the model starts from them
        for entity, handler in ((self.temp_entity, self.on_temp), (self.bottom_entity, self.on_bottom), (self.top_entity, self.on_top)):
            if self.hass is not None and (state := self.hass.states.get(entity)) is not None:
                try:
                    handler(float(state.state))
                except ValueError:
                    pass
        self._room = self.ambient
        lp.track(self.ambient_entity, lambda v: setattr(self, "_room", v))

    def on_bottom(self, degrees: float) -> None:
        self._bottom = degrees

    def on_top(self, degrees: float) -> None:
        self._top = degrees

    def setpoint(self, degrees: float | None) -> None:
        # write the setpoint: a temperature to run for, None to park it at the entity minimum and stop
        if not self.setpoint_entity or self.hass is None:
            return
        state = self.hass.states.get(self.setpoint_entity)
        low = float(state.attributes.get("min") or 0.0) if state is not None else 0.0
        step = float(state.attributes.get("step") or 0.0) if state is not None else 0.0
        want = low if degrees is None else max(low, math.ceil(degrees / step) * step if step else degrees)
        # nothing to write when the unit already holds it; a value someone else put there goes back
        try:
            if state is not None and float(state.state) == want:
                return
        except ValueError:
            pass
        domain = self.setpoint_entity.split(".")[0]
        self.hass.async_create_task(self.hass.services.async_call(domain, "set_value", {"entity_id": self.setpoint_entity, "value": want}, blocking=False))

    def on_power(self, watts: float, now: float | None = None) -> None:
        # the moments the compressor comes on and goes off, so a run is measured from its real start to its real end
        if watts > 100.0 >= self._meter[0]:
            self._on_since = now if now is not None else time.time()
        if watts <= 100.0 < self._meter[0]:
            self._off_at = now if now is not None else time.time()
        super().on_power(watts, now)

    def on_temp(self, degrees: float) -> None:
        self._temp0 = degrees
        # the first reading: the layers kept from before the restart, moved to the sensor, or else the tank taken as mixed; its own nights start here
        if self._layers is None and self.layers:
            kept = np.array(self.layers)
            self._layers = kept + degrees - at(kept, _HEIGHT[self.sensor_height])
        if self._layers is None:
            self._layers = np.full(_LAYERS, degrees)
        if not self.nights_from:
            self.nights_from = time.time()
        # the fill of the day is in
        if self._fill and degrees >= self._fill:
            self._fill = 0.0
        # we watch the tank, so the unit never decides the end; a completed disinfection restarts the interval
        if self._target and degrees >= self._target:
            if self._disinfect:
                self.legionella_at, self._disinfect = time.time(), False
            self._target = 0.0
            self.data.data[13:] = 0.0  # done: the rest of the block leaves the plan, so a restart or the next quarter does not take it up again
            self.status = EntryStatus.none
            self.setpoint(None)

    def cop_at(self, degrees: float, outside):
        # what the unit makes of one kWh: the standard curve at the level measured on this unit
        return np.maximum(1.0, self.level * curve(outside, degrees))

    def taus(self) -> np.ndarray:
        # the loss time per layer: measured at each height with a sensor, along a line in between, the tau of the sensor where nothing is measured
        heights, values = [_HEIGHT[self.sensor_height]], [self.tau]
        if self.tau_bottom and _BOTTOM < heights[0]:
            heights, values = [_BOTTOM, *heights], [self.tau_bottom, *values]
        if self.tau_top and _TOP > heights[-1]:
            heights, values = [*heights, _TOP], [*values, self.tau_top]
        return np.interp((np.arange(_LAYERS) + 0.5) / _LAYERS, heights, values)

    def update_model(self, now: datetime, kwh: float, outside: float) -> None:
        # the energy of the step goes in at the bottom and rises through the colder layers; every layer loses to the room at its own pace
        hours = (now.timestamp() - self._stepped) / 3600.0 if self._stepped else 0.0
        self._stepped = now.timestamp()
        self._layers[0] += kwh * self.cop_at(self._layers.mean(), outside) * _TANK_K * _LAYERS / self.volume
        self._layers = mix(self._room + (mix(self._layers) - self._room) * np.exp(-hours / self.taus()))

        # the sensor fell further than the model with the unit off: a tap, a litre at a time out at the top and mains water in at the bottom
        before = self._layers.sum()
        height = _HEIGHT[self.sensor_height]
        if self._meter[0] < 100.0 and at(self._layers, height) > self._temp0 + _STEP:
            while at(self._layers, height) > self._temp0 and self._layers[-1] > _COLD + _STEP:
                self._layers += _LAYERS / self.volume * (np.r_[_COLD, self._layers[:-1]] - self._layers)

        # with a sensor at the bottom or the top and the unit off, the layers between it and the middle follow it: the cold water that mixes in, the loss down there, the warm water up there
        centres = (np.arange(_LAYERS) + 0.5) / _LAYERS
        if self._bottom is not None and self._meter[0] < 100.0 and height > _BOTTOM:
            share = np.clip((height - centres) / (height - _BOTTOM), 0.0, 1.0)
            self._layers = mix(self._layers + share * (self._bottom - at(self._layers, _BOTTOM)))
        if self._top is not None and self._meter[0] < 100.0 and height < _TOP:
            share = np.clip((centres - height) / (_TOP - height), 0.0, 1.0)
            self._layers = mix(self._layers + share * (self._top - at(self._layers, _TOP)))

        # what the sensors took out beyond the loss of the model is what the house drew
        self._tapped += (before - self._layers.sum()) * self.volume / _LAYERS / _TANK_K

    def update_level(self, now: datetime, kwh: float, outside: float) -> None:
        # a run collects all its energy on the curve, from the model mean before the heat of its first step
        if self._meter[0] > 100.0 and self._run is None:
            self._run = [self._on_since, self._layers.mean() - kwh * self.cop_at(self._layers.mean(), outside) * _TANK_K / self.volume, 0.0]
        if (run := self._run) is None:
            return
        run[2] += kwh * curve(outside, self._layers.mean())
        if self._meter[0] > 100.0:
            return

        # the run is over, from the power going up to going down; after half an hour the tank is mixed, so its rise plus what leaked away, against that energy, is the level
        self._run = None
        if (hours := (self._off_at - run[0]) / 3600.0) < _LONG_RUN or not run[2]:
            return
        heat = self.volume / _TANK_K * (self._temp0 - run[1] + ((self._temp0 + run[1]) / 2.0 - self._room) * hours / self.tau)
        self._levels = self._levels[-9:] + [heat / run[2]]
        self.level = float(np.median(self._levels))
        self._layers = np.full(_LAYERS, self._temp0)

    def fill_to(self, hours: float) -> tuple[float, float]:
        # the model heated at the bottom, a tenth of a kWh at a time and mixing as the unit does, until its top holds t_min
        # for that many hours: the sensor then, and the heat it took; no heat when the top holds already
        T, heat = self._layers.copy(), 0.0
        while self.top_after(T, hours) < self.t_min and at(T, _HEIGHT[self.sensor_height]) < self.t_max:
            T[0] += 0.1 * _TANK_K * _LAYERS / self.volume
            T = mix(T)
            heat += 0.1
        return at(T, _HEIGHT[self.sensor_height]), heat

    def fill_quarters(self, outside: float) -> int:
        # the quarters a fill for a day takes: the heat the model needs, over what one quarter of running adds
        return max(1, math.ceil(self.fill_to(24.0)[1] / (self.max_power * 0.25 * self.cop_at(self._layers.mean(), outside))))

    def block_at(self, lp: LpSolver, first: int, span: int, outside: float) -> int:
        # the start of the cheapest block in the span that a fill for a day takes, per kWh of heat at the outside air of each quarter
        quarters = self.fill_quarters(outside)
        air = lp.temp.data[12 + first:12 + first + span] if lp.temp is not None else np.zeros(span)
        cost = lp.price_buy.data[12 + first:12 + first + span] / self.cop_at(self._layers.mean(), air)
        return first + min(range(max(1, span - quarters + 1)), key=lambda s: cost[s:s + quarters].sum())

    def top_after(self, T: np.ndarray, hours: float) -> float:
        # the top of these layers after that many hours of loss, each layer at its own pace, with the draw of a day at the end taken from the top
        T = mix(self._room + (T - self._room) * np.exp(-hours / self.taus()))
        heat = self.draw
        while heat > 0.0 and T[-1] > _COLD + _STEP:
            heat -= (T[-1] - _COLD) / _TANK_K
            T = T + _LAYERS / self.volume * (np.r_[_COLD, T[:-1]] - T)
        return at(T, _TOP)

    def fill_level(self, lp: LpSolver, rest: int, outside: float) -> float:
        # what the house drew since the last block moves the daily energy; no fill when the top of the model holds t_min until the block of tomorrow
        if self._filled:
            self.draw = (1.0 - _WEIGHT) * self.draw + _WEIGHT * self._tapped
        self._filled, self._tapped = True, 0.0
        level, heat = self.fill_to(0.25 * self.block_at(lp, rest, 96, outside))
        return min(self.t_max, level) if heat else 0.0

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        # books the measured energy and schedules the daily recalibration; the energy of this step goes to the model
        kwh = self._pending_kwh
        await super().entry_prerun(lp, now)
        outside = lp.temp.data[12] if lp.temp is not None else 0.0

        # the model follows the tank, from its first reading on
        if self._layers is not None and self.volume:
            self.update_model(now, kwh, outside)
            self.update_level(now, kwh, outside)
            self.layers = self._layers.round(2).tolist()

        # no prices or no reading: decide nothing rather than run now
        price = lp.price_buy.data[12:]  # seven days deep
        if not price[:96].any() or self._layers is None:
            return

        # the cheapest day of the disinfection band
        rest = 96 - (now.hour * 4 + now.minute // 15)  # quarters left today
        low = self.legionella_days - self.legionella_band
        days = [min(len(price) - 96, max(0, int((self.legionella_at + d * 86400.0 - now.timestamp()) // 900))) for d in range(low, low + 2 * self.legionella_band + 1)]
        cheapest = min(days, key=lambda s: float(np.mean(price[s:s + 96])))

        # once a day the fill: planned in the first hour of the day for the cheapest block of that day, started in its first quarter or not at all
        if self.volume and now.hour == 0 and self._fill_on != now.date():
            self._fill_on, self.fill_at = now.date(), now.timestamp() // 900 * 900 + 900.0 * self.block_at(lp, 0, rest, outside)
        if self.fill_at and now.timestamp() >= self.fill_at:
            if now.timestamp() < self.fill_at + 900.0:
                self._fill, self._fill_since = self.fill_level(lp, rest, outside), now.timestamp()
            self.fill_at = 0.0
        # a fill the unit has not started within a quarter is not wanted by its own thermostat: dropped, never run later
        if self._fill and self._run is None and now.timestamp() >= self._fill_since + 900.0:
            self._fill = 0.0
        # after a restart the plan says it ran and runs on: the fill goes on, at the level the model gives for the rest of it
        if not self._fill and self.data.data[11] > 0 and self.data.data[12] > 0:
            self._fill, self._fill_since = self.fill_level(lp, rest, outside), now.timestamp()

        # the fill, raised to t_max only by the disinfection; a target already met ends at once
        target = self._fill
        if self.legionella and (self._disinfect or (cheapest == 0 and price[0] <= np.min(price[:rest]))):
            self._disinfect, target = True, self.t_max
        self._target = target
        self.on_temp(self._temp0)

        # the target itself goes to the unit, so the unit stops there too; what it actually draws back onto its phase
        self.setpoint(self._target if self._target else None)
        self.status = EntryStatus.running if self._target else EntryStatus.none
        self.data.data[12] = self._meter[0] / 1000.0

        # the planned block ahead in the series, so the panel shows it; whether it fills is decided when it comes
        self.data.data[13:] = 0.0
        if (ahead := int((self.fill_at - now.timestamp() // 900 * 900) // 900)) > 0:
            self.data.data[12 + ahead:12 + ahead + self.fill_quarters(outside)] = self.max_power
        # a running fill: its remaining quarters after this one
        elif self._target:
            self.data.data[13:12 + self.fill_quarters(outside)] = self.max_power
        # what it draws now and the block ahead on its phase, so the plan keeps room for it
        lp.phase_add(self.phase, self.data.data[12:])

    async def restore(self, hass) -> None:
        d = await self.load_params(hass)
        self.tau = d.get("tau", self.tau)
        self.tau_bottom, self.tau_top = d.get("tau_bottom", 0.0), d.get("tau_top", 0.0)
        self.draw = d.get("draw", self.draw)
        self._levels = d.get("levels", [])
        # the level is the median of the runs measured so far
        if self._levels:
            self.level = float(np.median(self._levels))

    async def calibrate(self, lp: LpSolver, now: datetime) -> None:
        # the loss per sensor from the nights, the assumption where the drop is too small to measure; the daily energy and the runs are kept with it
        tau, bottom, top = await fit_nights(lp.hass, self.power_entity, [self.temp_entity, self.bottom_entity, self.top_entity], self.ambient_entity, self.ambient, now, since=self.nights_from, phase=self.phase)
        self.tau = tau if tau is not None else _TAU
        self.tau_bottom, self.tau_top = bottom or 0.0, top or 0.0
        await self.save_params(lp.hass, {"tau": self.tau, "tau_bottom": self.tau_bottom, "tau_top": self.tau_top, "draw": self.draw, "levels": self._levels})


class BoilerElectric(Heating):
    # domestic hot water from a resistive element: every kWh in is a kWh of heat
    CONFIG = [*_LINK, _MANAGE, *_BAND, _AMBIENT, _MAX_POWER, _VOLUME, *_TAIL]

    def __init__(self) -> None:
        super().__init__()
        self.t_min, self.t_max = 40.0, 60.0
        self.tau = 110.0  # a tank coasts far longer than a house: about 28 hours
        self.cop = 1.0


def mix(T: np.ndarray) -> np.ndarray:
    # warmer water below colder rises: unstable neighbours merge until the column is stable, the heat kept
    blocks = []
    for v in T:
        blocks.append([v, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, n = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += n
    return np.concatenate([np.full(n, s / n) for s, n in blocks])


def at(T: np.ndarray, height: float) -> float:
    # the temperature at a height in the column, between the layer centres
    return float(np.interp(height * _LAYERS - 0.5, np.arange(_LAYERS), T))


def curve(outside, water):
    # the standard COP curve: along the outside air at water 35 and 55, then along the water
    w35, w55 = np.interp(outside, _CURVE_T, _CURVE_W35), np.interp(outside, _CURVE_T, _CURVE_W55)
    return w35 + (w55 - w35) * (water - 35.0) / 20.0


async def fit_nights(hass, elec_id: str, temp_ids: list, amb_id: str, ambient: float, now: datetime, since: float = 0.0, phase: Phase = Phase.all) -> list:
    # tau per sensor from the nights of the last ten days since the boiler came: the drop from 1 to 6 with the unit off, against the room, the median over the nights
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import state_changes_during_period

    if not hass or not elec_id:
        return [None] * len(temp_ids)
    start = now - timedelta(days=_NIGHTS + 1)
    grid = np.arange(start.timestamp(), now.timestamp(), 60.0)

    async def held(entity, factor=1.0):
        # the raw states held at every minute, and the smallest step the sensor makes; the nan behind the values is what a minute before the first state reads
        ts, vals = [], []
        for s in (await get_instance(hass).async_add_executor_job(state_changes_during_period, hass, start, now, entity)).get(entity, []):
            try:
                vals.append(abs(float(s.state)) * factor)
                ts.append(s.last_changed.timestamp())
            except ValueError:
                pass
        steps = np.abs(np.diff(vals))
        steps = steps[steps > 0]
        return np.array(vals + [np.nan])[np.searchsorted(ts, grid, side="right") - 1], float(steps.min()) if len(steps) else _STEP

    pwr = (await held(elec_id, watt_factor(hass.states.get(elec_id), phase) / 1000.0))[0]
    room = (await held(amb_id))[0] if amb_id else np.full(len(grid), ambient)
    room = np.where(np.isnan(room), ambient, room)

    # every night from 1 to 6 with the unit off: the drop at each sensor against the room
    taus = []
    for temp_id in temp_ids:
        if not temp_id:
            taus.append(None)
            continue
        tmp, step = await held(temp_id)
        drops, ks = [], []
        for day in range(_NIGHTS):
            if (t1 := (now - timedelta(days=day)).replace(hour=1, minute=0, second=0, microsecond=0).timestamp()) < since:
                continue
            i1 = int((t1 - grid[0]) // 60)
            i6 = i1 + 300
            if i1 < 0 or i6 >= len(grid) or np.isnan(tmp[i1]) or np.isnan(tmp[i6]) or np.nan_to_num(pwr[i1:i6]).max() > 0.1:
                continue
            if (diff := (tmp[i1] + tmp[i6]) / 2.0 - room[i1:i6].mean()) > 0.0:
                drops.append(tmp[i1] - tmp[i6])
                ks.append((tmp[i1] - tmp[i6]) / 5.0 / diff)
        # under four steps of this sensor the drop is more rounding than loss: then the assumption
        if not drops or np.median(drops) < 4 * step:
            taus.append(None)
            continue
        taus.append(float(1.0 / np.median(ks)))
    return taus
