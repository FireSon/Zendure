from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from .solver import LpSolver, LpColumn, SolverEntry
from ..config import ConfigField
from ..consumer import ConsumerDevice
from ..const import Phase, watt_factor

_TANK_K = 3600.0 / 4.2  # litre-kelvin per kWh of water: 1 kWh lifts 200 l by 4.3 degC
_COMFORT_PENALTY = 50.0  # EUR per degC-quarter of house comfort shortfall (soft floor; lets DHW win later)
_ABS_MARGIN = 3.0  # the hard house floor sits this far under t_min; below t_min costs a comfort penalty
_CONTROLS = [{"value": "delta", "label": "Temp delta"}, {"value": "power", "label": "Opgenomen vermogen"}, {"value": "capacity", "label": "Capaciteit %"}, {"value": "temperature", "label": "Temperatuur"}]
_FIT_DAYS, _FIT_MIN_DAYS = 365, 40  # the weekly house fit: days of recorder history, cloudy heating days before it is trusted
_TARGET_COST = 0.05 / 4  # EUR per degC-quarter under t_min: the plan only dips under it when that saves more
_KNEE, _KNEE_COP = 0.6, 0.64  # above this many kW each extra kW gives this share of the heat, also when the cold forces the MHI above it
_DEFROST = ((-3, 0.052), (0, 0.064), (3, 0.055), (6, 0.045), (9, 0.048), (99, 0.046))  # share of the heat the defrosts cost, per upper limit of the outdoor band (MHI season 2025-26, 0.4 kWh per defrost)
_MORNING, _EVENING, _NIGHT = 7, 17, 23  # the house floor: t_min_day from the morning, t_min from the evening, t_min_night from the night
_MASS_HOURS = 2.0  # the air hands its heat to the building mass in about this many hours (the cooldowns of the MHI house)
_MIN_KW, _MIN_COLD, _COLD = 0.6, 0.9, 4.5  # the MHI runs from 0.6 kW, and from 0.9 kW under 4.5 C outdoor (season 2025-26)
_MIN_RUN = 2  # a run shorter than half an hour loses most of it to the start


def _min_kw(outdoor: float) -> float:
    # the lowest power the heat pump holds: it raises its minimum compressor speed in the cold
    return _MIN_COLD if outdoor < _COLD else _MIN_KW


def _cop(outdoor: float, indoor: float) -> float:
    # Carnot between the air inside (+10 K at the condenser) and outside (-5 K at the evaporator) at 39 % efficiency, less the defrosts: fitted with the knee on the MHI's air-side heat
    carnot = (indoor + 283.15) / (indoor - outdoor + 15)
    return min(6.0, max(1.5, 0.39 * carnot * (1 - next(d for top, d in _DEFROST if outdoor < top))))


# the fields every store shares; each class below adds the ones that only make sense for it
_LINK = [
    ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True, hidden=True),
    ConfigField("name", {"text": {}}),  # the subentry title: HA names the device and its entities after it
    ConfigField("temp_entity", {"entity": {"filter": [{"domain": "sensor", "device_class": "temperature"}]}}, cast=str, section_key="link"),
    ConfigField("power_entity", {"entity": {"filter": [{"domain": "sensor", "device_class": "power"}, {"domain": "sensor", "device_class": "current"}]}}, cast=str, section_key="link"),
    ConfigField("switch_entity", {"entity": {"filter": [{"domain": "switch"}, {"domain": "binary_sensor"}]}}, cast=str, required=False, section_key="link"),
]
# settings have no cast: they are entities on the consumer's device, HA keeps their value (see ConsumerDevice)
_MANAGE = ConfigField("manage", {"boolean": {}}, section_key="behaviour")
_BAND = [
    ConfigField("t_min", {"number": {"mode": "box", "step": 0.5}}, section_key="behaviour"),
    ConfigField("t_max", {"number": {"mode": "box", "step": 0.5}}, section_key="behaviour"),
]
_AMBIENT = ConfigField("ambient", {"number": {"mode": "box", "step": 0.5}}, section_key="behaviour")
_AMBIENT_ENTITY = ConfigField("ambient_entity", {"entity": {"filter": [{"domain": "sensor", "device_class": "temperature"}]}}, cast=str, required=False, section_key="link")
_HEAT_MAX = ConfigField("heat_max", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kW"}}, required=False, section_key="plate")
_COP = ConfigField("cop", {"number": {"mode": "box", "step": 0.1}}, required=False, section_key="plate")
_MAX_POWER = ConfigField("max_power", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kW"}}, required=False, section_key="plate")
_VOLUME = ConfigField("volume", {"number": {"mode": "box", "step": 10, "unit_of_measurement": "l"}}, required=False, section_key="plate")
_TAIL = [
    ConfigField("phase", {"select": {"options": [{"value": "0", "label": "Alle fasen"}, {"value": "1", "label": "L1"}, {"value": "2", "label": "L2"}, {"value": "3", "label": "L3"}]}}, section_key="expert"),
    ConfigField("state", {"entity": {}}, readonly=True),
]


class Heating(SolverEntry, ConsumerDevice):
    # One thermal store: the temperature is its state of charge, electrical power charges it, and an RC model
    # calibrated from history ties the two together. The classes at the bottom pick the fields and the behaviour.
    SOFT_FLOOR = False  # a house may dip below its comfort floor at a price; a tank may not
    CAL_DAYS = 7  # days between two self-calibrations

    def __init__(self) -> None:
        super().__init__()
        self.id = ""
        self.phase = Phase.all
        self.temp_entity = ""
        self.switch_entity = ""  # on/off of the machine
        self.max_power = 2.5  # electrical source cap (kW)
        # off the nameplate: the heat range and the COP. The electrical range follows from it; empty keeps max_power
        self.heat_max = 0.0
        self.cop = 0.0
        self.t_min = 19.0  # floor: soft for house, hard for boiler/pool
        self.t_max = 22.0
        self.t_min_night = 18.0  # night setback of the soft house floor
        self.t_min_day = 18.5  # daytime floor of the house, until the evening
        self.ambient = 20.0  # fixed loss temperature (boiler/pool); house loses to the outdoor forecast
        self.ambient_entity = ""  # sensor in the room the tank stands in; empty falls back to ambient
        self.volume = 0.0  # litres of water; filled in it beats any fit, because the physics is exact
        # calibrated / runtime params (not in the config)
        self.slope = 0.12  # UA/COP: degC rise per kW of electrical input
        self.icept = 0.0  # free-gain offset
        self.tau = 40.0  # cooling time constant (hours)
        # house heat balance (kW/C, kW/C per m/s of wind, kW per W/m2 of sun, kW)
        self.ua, self.ua_wind, self.sun_gain, self.q_int = 0.0, 0.0, 0.0, 0.0
        self.ci, self.cm, self.him = 0.0, 0.0, 0.0  # air and building mass (kWh/C) and their coupling (kW/C); ci 0 keeps the RC model
        self.mass: LpColumn = None  # building mass temperature column (two-store house only)
        self._mass0, self._mass_at = 0.0, None  # the building mass now: it has no sensor
        self._cops = [3.0] * 96  # absolute COP per quarter, from the outdoor forecast
        self._temp0 = 20.0  # live temperature, pinned at slot 0 (pushed in via track)
        self._tmin = [self.t_min] * 96  # per-slot house floor (night setback), rebuilt each tick
        self._last_cal: datetime | None = None
        self.power: LpColumn = None  # electrical draw column (kW per slot)
        self.temp: LpColumn = None  # store temperature column (state of charge)
        self.slack: LpColumn = None  # soft-floor shortfall column (house only)
        self.over: LpColumn = None  # soft-ceiling excess column (house only)

    def entry_config(self, lp: LpSolver) -> None:
        self.phase = Phase(self.phase)  # accept the plain int value coming from the config
        soft = self.SOFT_FLOOR
        hard_min = self.t_min - _ABS_MARGIN if soft else self.t_min  # house keeps a hard floor below the soft comfort floor
        self._floor = hard_min
        if soft:
            self._outdoor = lp.temp  # house loses heat to the outdoor forecast
        lp.track(self.temp_entity, lambda v: setattr(self, "_temp0", v))  # keep the live temperature fresh
        # start from the live temperature: track only hears the next change
        if lp.hass is not None and (state := lp.hass.states.get(self.temp_entity)) is not None and state.state not in ("unknown", "unavailable"):
            self._temp0 = float(state.state)
        lp.track(self.power_entity, self.on_power, phase=self.phase)  # measured energy, for the realised saving

        # electrical draw column, tagged on its phase so the solver's phase-limit row includes it. The cap comes
        # from the nameplate when it is filled in (heat divided by COP), otherwise from max_power as before.
        if self.volume:  # a known volume gives the rise per kWh of heat exactly; the COP turns that into electrical
            self.slope = self.volume / (_TANK_K * self.tau * (self.cop or 1.0))
        self.max_kw = self.heat_max / self.cop if self.heat_max and self.cop else self.max_power
        self.power = lp.column(hi=self.max_kw, tags=("draw",), dest=lp.data(f"{self.id}_power", "kW", name=self.name), phase=self.phase)
        # temperature state. House has no hard upper bound: with no cooling path a ceiling is infeasible in summer, so it gets a soft one below.
        # Boiler/pool keep the hard band [hard_min, t_max]. Slot 0 is pinned to the live temp in prerun.
        self.temp = lp.column(lo=hard_min, dest=lp.data(f"{self.id}_temp", "C", name=self.name)) if soft else lp.column(lo=hard_min, hi=self.t_max, dest=lp.data(f"{self.id}_temp", "C", name=self.name))
        # The floor is a row, not a bound: a store that sits under it cannot always climb back inside the phase limit,
        # and a bound it cannot reach makes the whole LP infeasible. Falling short costs _COMFORT_PENALTY per degC.
        self.slack = lp.column(cost=_COMFORT_PENALTY, lo=0.0)
        lp.rowQuarter({self.temp: 1.0, self.slack: 1.0}, lo=lambda t: self._tmin[t])
        # the house has a soft ceiling too: above t_max costs the same penalty, so cheap power does not overheat it
        if soft:
            self.over = lp.column(cost=_COMFORT_PENALTY, lo=0.0)
            lp.rowQuarter({self.temp: 1.0, self.over: -1.0}, hi=lambda t: self.t_max)

        self._cops = [3.0] * lp.horizon  # absolute COP per quarter for the heat balance, sized to the solve window
        self._mins = [_MIN_KW] * lp.horizon  # the minimum power per quarter, from the outdoor forecast; only _runs uses it
        # a house is two heat balances per quarter, all in kW heat: Ci*dTi = COP*P + Him*(Tm - Ti) + g*sun + Q_int - (UA + w*wind)*(Ti - T_out), Cm*dTm = Him*(Ti - Tm)
        if soft:  # air heated by the heat pump, building mass warmed by the air; the comfort rows bound the air
            self.mass = lp.column(dest=lp.data(f"{self.id}_mass", "C", name=self.name))
            # under t_min costs a little, so the plan only dips under it when that saves more
            below = lp.column(cost=_TARGET_COST, lo=0.0)
            lp.rowQuarter({self.temp: 1.0, below: 1.0}, lo=lambda t: self.t_min)
            # power above the knee gives less heat per kW; the LP keeps knee at power - _KNEE when that is positive
            knee = lp.column(lo=0.0)
            lp.rowQuarter({knee: 1.0, self.power: -1.0}, lo=lambda t: -_KNEE, hi=lambda t: 0.0)
            # the sun on the expected solar production when there are solar meters, else on the global irradiance
            sun = lambda t: (lp.solar if lp.globals.get("solar_entities") else lp.sun).data[12 + t]
            ua = lambda t: self.ua + self.ua_wind * lp.wind.data[12 + t]
            gain = lambda t: 0.25 / self.ci * (self.sun_gain * sun(t) + self.q_int + ua(t) * self._outdoor.data[12 + t])
            air = lp.rowDelta(lambda t1, t2: self.temp[t1] - (1 - 0.25 * (ua(t2) + self.him) / self.ci) * self.temp[t2] - 0.25 * self.him / self.ci * self.mass[t2] - 0.25 * self._cops[t2] / self.ci * (self.power[t2] - (1 - _KNEE_COP) * knee[t2]), const=gain)
            mass = lp.rowDelta(lambda t1, t2: self.mass[t1] - (1 - 0.25 * self.him / self.cm) * self.mass[t2] - 0.25 * self.him / self.cm * self.temp[t2], const=0.0)
            self._rebuild_model = lambda: (air(), mass())
        else:
            # discretized RC model -> temp[t+1] = (1-B)*temp[t] + A*power[t] + B*loss_temp + icept*A, with
            # A = 0.25/(tau*slope) degC per kW-quarter and B = 0.25/tau loss fraction per quarter.
            # The lambdas read the live parameters so a recalibration only needs the row rebuild below.
            loss = lambda t: 0.25 / self.tau * self.ambient + self.icept * 0.25 / (self.tau * self.slope)
            self._rebuild_model = lp.rowDelta(lambda t1, t2: self.temp[t1] + (0.25 / self.tau - 1.0) * self.temp[t2] - 0.25 / (self.tau * self.slope) * self.power[t2], const=loss)

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        # The house is used as a heat buffer, so the saving is the same heat bought at a better moment:
        # the energy of this quarter against the average buy price over the coming day.
        if self._pending_kwh and getattr(lp, "price_buy", None) is not None:
            window = lp.price_buy.data[12 : 12 + 96]
            reference = float(sum(window) / len(window)) if len(window) else 0.0
            price = float(lp.price_buy.data[12])
            if (booked := lp.globals.get("savings_pending")) is not None:
                booked.append(self._pending_kwh * (reference - price))
            self._pending_kwh = 0.0

        self._tmin = [self.t_min] * lp.horizon
        if self.SOFT_FLOOR:
            # the house floor per hour of the day, then per quarter of the plan
            floors = [self.t_min_night] * _MORNING + [self.t_min_day] * (_EVENING - _MORNING) + [self.t_min] * (_NIGHT - _EVENING) + [self.t_min_night] * (24 - _NIGHT)
            self._tmin = [floors[(now + timedelta(minutes=15 * t)).hour] for t in range(lp.horizon)]
            # the COP of each quarter follows the outdoor forecast
            self._cops = [_cop(self._outdoor.data[12 + t], self._tmin[t]) for t in range(lp.horizon)]  # the room at the floor of that quarter
            self._mins = [_min_kw(self._outdoor.data[12 + t]) for t in range(lp.horizon)]
            self._rebuild_model()
        # Slot 0 is the temperature the sensor reports, also when that sits outside the band: clamping it into the
        # band would hide the energy still needed to get back. A store cannot jump, so the bounds move with it: the
        # floor climbs at full power, the ceiling falls at the store's own loss rate, both until the band is reached.
        if self.temp is not None:  # a store that plans outside the LP has no columns to bound
            t0 = self._temp0
            self.temp.lower[0] = self.temp.upper[0] = t0
            self.temp.lower[1:] = min(self._floor, t0)  # never a bound the store cannot reach; the row pulls it back up
            if not self.SOFT_FLOOR:
                self.temp.upper[1:] = max(self.t_max, t0)  # a store above its band cools on its own, so do not force it down
        if self.mass is not None:  # the mass follows the measured room by its own time constant, as in the fit; the plan starts there
            step = min(1.0, (now - self._mass_at).total_seconds() / 3600 * self.him / self.cm) if self._mass_at else 1.0
            self._mass0 += step * (self._temp0 - self._mass0)
            self._mass_at, self.mass.lower[0], self.mass.upper[0] = now, self._mass0, self._mass0
        if lp.hass is None:
            return
        if self._last_cal is None or (now - self._last_cal).days >= self.CAL_DAYS:  # on cold start and every CAL_DAYS days
            self._last_cal = now
            lp.hass.async_create_task(self.calibrate(lp, now))

    def entry_postrun(self, lp: LpSolver) -> None:
        if self.power is not None:
            p = self.power.dest.data
            end = 12 + lp.horizon
            p[12:end] = np.round(p[12:end] / 0.1) * 0.1  # snap the plan to 0.1 kW steps for display

    async def restore(self, hass) -> None:
        d = await self.load_params(hass)
        self.slope = d.get("slope", self.slope)
        self.icept = d.get("icept", self.icept)
        self.tau = d.get("tau", self.tau)
        for k in ("ua", "ua_wind", "sun_gain", "q_int", "ci", "cm", "him"):
            setattr(self, k, d.get(k, getattr(self, k)))

    async def calibrate(self, lp: LpSolver, now: datetime) -> None:
        # fit the RC model (slope, icept, tau) of a tank from recent history; the house has its own calibrate
        res = await fit_store(lp.hass, self.power_entity, self.temp_entity, self.ambient, phase=self.phase)
        if res is not None:
            self.icept = res[1]
            if res[2] is not None:
                self.tau = res[2]
            # with a known volume the slope follows from tau, as in entry_config; otherwise from the fit
            self.slope = self.volume / (_TANK_K * self.tau * (self.cop or 1.0)) if self.volume else res[0]
            self._rebuild_model()  # push the new parameters into the LP rows
            if (request_solve := lp.globals.get("request_solve")) is not None:
                request_solve()  # the standing plan was built with the old model: solve again right away
        await self.save_params(lp.hass, {"slope": self.slope, "icept": self.icept, "tau": self.tau})



class HeatingHeatPump(Heating):
    # space heating by a heat pump: comfort floor with a night setback, loses heat to the outdoor forecast
    SOFT_FLOOR = True
    TILES = ["temp_entity", "control_entity"]
    CONFIG = [*_LINK, ConfigField("control_entity", {"entity": {"filter": [{"domain": "number"}, {"domain": "climate"}]}}, cast=str, required=False, section_key="link"),
              ConfigField("control", {"select": {"options": _CONTROLS}}, cast=str, section_key="behaviour"), _MANAGE, ConfigField("setpoint", {"number": {"mode": "box", "step": 0.5}}, section_key="behaviour"), _BAND[1],
              ConfigField("t_min_day", {"number": {"mode": "box", "step": 0.5}}, section_key="behaviour"), ConfigField("t_min_night", {"number": {"mode": "box", "step": 0.5}}, section_key="behaviour"), _HEAT_MAX, _COP, *_TAIL]
    # the house calls its comfort target the setpoint, like a thermostat; the shared code reads it as t_min
    t_min = property(lambda self: self.setpoint, lambda self, v: setattr(self, "setpoint", v))

    def __init__(self) -> None:
        super().__init__()
        self.t_min, self.t_max, self.t_min_day, self.t_min_night = 19.5, 20.5, 18.5, 18.0
        self.ci, self.cm, self.him, self.ua, self.q_int = 8.0, 12.5, 2.4, 0.2, 0.4  # a typical house until the calibration has fitted this one
        self.control_entity = ""  # the number or climate the plan of each quarter is written to
        self.control = "temperature"

    def _runs(self, p: np.ndarray, mins: np.ndarray) -> np.ndarray:
        # blocks of power the MHI can make: a gap of one quarter is closed, since a restart costs more than running on
        on = p > 0
        on[1:-1] |= on[:-2] & on[2:]
        # each block keeps its energy, on as many quarters from its start as hold its highest minimum; under _MIN_RUN quarters it drops out
        out = np.zeros_like(p)
        for block in np.split(np.arange(len(p)), np.flatnonzero(np.diff(on)) + 1):
            if on[block[0]] and (n := min(len(block), int(p[block].sum() / mins[block].max()))) >= _MIN_RUN:
                out[block[:n]] = p[block].sum() / n
        return out

    def entry_postrun(self, lp: LpSolver) -> None:
        p = self.power.dest.data
        p[12:12 + lp.horizon] = self._runs(p[12:12 + lp.horizon], np.array(self._mins))
        # this quarter's plan in the type the entity takes, on its step and inside its range; written every quarter, a device falls back to its own curve without it
        if self.control_entity and lp.hass is not None and (state := lp.hass.states.get(self.control_entity)) is not None:
            a, p, floor = state.attributes, float(self.power.dest.data[12]), self._tmin[0]
            delta = max(0.0, float(self.temp.dest.data[13]) - floor) if p else 0.0  # the room the plan heats to by the end of this quarter; none in a quarter _runs dropped
            value = {"power": p * (1000.0 if a.get("unit_of_measurement") == "W" else 1.0), "capacity": 100.0 * p / self.max_kw, "delta": delta, "temperature": floor + delta}[self.control]
            step = a.get("step") or a.get("target_temp_step") or 0.5
            value = round(round(value / step) * step, 3)
            value = min(max(value, a.get("min", a.get("min_temp", value))), a.get("max", a.get("max_temp", value)))
            domain = self.control_entity.split(".")[0]
            service, key = ("set_temperature", "temperature") if domain == "climate" else ("set_value", "value")
            lp.hass.async_create_task(lp.hass.services.async_call(domain, service, {"entity_id": self.control_entity, key: value}, blocking=False))
        super().entry_postrun(lp)

    async def calibrate(self, lp: LpSolver, now: datetime) -> None:
        # every week the house as air and mass from the last year of heating days
        if (fit := await fit_house(lp.hass, self.power_entity, self.temp_entity, lp.globals.get("solar_entities", []), lp.globals.get("latitude", 0.0), lp.globals.get("longitude", 0.0), self.phase)) is not None:
            self.ci, self.cm, self.him, self.ua, self.ua_wind, self.sun_gain, self.q_int = fit
            self._rebuild_model()
            await self.save_params(lp.hass, {k: getattr(self, k) for k in ("ua", "ua_wind", "sun_gain", "q_int", "ci", "cm", "him")})
            if (request_solve := lp.globals.get("request_solve")) is not None:
                request_solve()


class PoolHeatPump(Heating):
    # pool water from a heat pump: hard band, loses heat to the outside air around the pool
    CONFIG = [*_LINK, _MANAGE, *_BAND, _AMBIENT, _HEAT_MAX, _COP, *_TAIL]

    def __init__(self) -> None:
        super().__init__()
        self.t_min, self.t_max = 26.0, 30.0

_CAL_MIN_DAYS = 20   # minimum heating-active days before a refit is trusted (off-season gate)


def floor_hour(x) -> datetime:
    # recorder stat 'start' (unix seconds in-process, ms via WS) or iso string -> UTC hour
    dt = datetime.fromtimestamp(x / 1000 if x > 1e12 else x, timezone.utc) if isinstance(x, (int, float)) else datetime.fromisoformat(str(x).replace("Z", "+00:00")).astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


async def fit_store(hass, elec_id: str, temp_id: str, ambient: float, days: int = 90, phase: Phase = Phase.all):
    # Self-calibrate a thermal store from recorder history. Returns (slope, icept, tau) or None when there
    # is too little heating signal in the window (off-season -> caller keeps its current params).
    #   slope = UA/COP (kW_elec/degC), icept = free-gain offset (kW_elec), tau = cooling time constant (h).
    # The loss is against the fixed temperature of the room the tank stands in.
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    if not elec_id or not temp_id:
        return None
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, now, {elec_id, temp_id}, "hour", None, {"mean"})
    state = hass.states.get(elec_id)
    factor = watt_factor(state, phase) / 1000.0  # the reading in kW, whatever unit the sensor uses
    pwr = {floor_hour(r["start"]): abs(r["mean"]) * factor for r in stats.get(elec_id, []) if r.get("mean") is not None}  # abs: a reversed meter reports negative
    tmp = {floor_hour(r["start"]): r["mean"] for r in stats.get(temp_id, []) if r.get("mean") is not None}

    ref = {k: float(ambient) for k in tmp}

    hrs = sorted(set(pwr) & set(tmp) & set(ref))
    if not hrs:
        return None

    # energy signature: daily-mean P_elec vs daily-mean (T - T_ref), gated on heating-active days
    days_acc: dict = {}
    for k in hrs:
        days_acc.setdefault(k.date(), []).append((pwr[k], tmp[k] - ref[k]))
    dp = np.array([np.mean([p for p, _ in v]) for v in days_acc.values()])
    dd = np.array([np.mean([x for _, x in v]) for v in days_acc.values()])
    active = (dp > 0.1) & (dd > 3.0)
    if int(active.sum()) < _CAL_MIN_DAYS:
        return None
    m, b = np.linalg.lstsq(np.c_[dd[active], np.ones(int(active.sum()))], dp[active], rcond=None)[0]
    r2 = 1.0 - np.sum((dp[active] - (m * dd[active] + b)) ** 2) / np.sum((dp[active] - dp[active].mean()) ** 2)
    if not (m > 0 and r2 > 0.5):
        return None
    slope, icept = float(m), float(-b)

    # tau from off-stretches (P~0) with a real gradient, at night
    runs, cur, taus = [], [], []
    for k in hrs:
        if pwr[k] < 0.05:
            cur = cur + [k] if (cur and k == cur[-1] + timedelta(hours=1)) else [k]
        else:
            if len(cur) >= 6:
                runs.append(cur)
            cur = []
    if len(cur) >= 6:
        runs.append(cur)
    for run in runs:
        xy = [(i, np.log(tmp[k] - ref[k])) for i, k in enumerate(run) if tmp[k] - ref[k] > 0.5 and ((k.hour >= 22) or (k.hour < 6))]
        if len(xy) >= 5:
            sl = float(np.polyfit([i for i, _ in xy], [y for _, y in xy], 1)[0])
            if sl < 0:
                taus.append(-1.0 / sl)
    tau = float(min(120.0, max(10.0, np.median(taus)))) if taus else None
    return slope, icept, tau


async def fit_house(hass, power_id: str, temp_id: str, solar_ids: list, lat: float, lon: float, phase: Phase = Phase.all):
    # The house from its daily heat balances over the last year: loss with wind, sun and free heat, and air plus mass from the room changes.
    # Returns (ci, cm, him, ua, ua_wind, sun_gain, q_int), or None with too few days.
    import aiohttp
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    # hourly means of the heat pump and the solar meters in kW and of the room; the standby of fans and control gives no heat
    now = datetime.now(timezone.utc)
    begin = now - timedelta(days=_FIT_DAYS)
    stats = await get_instance(hass).async_add_executor_job(statistics_during_period, hass, begin, now, {power_id, temp_id, *solar_ids}, "hour", None, {"mean"})
    kw = lambda i, ph: {floor_hour(r["start"]): abs(r["mean"]) * watt_factor(hass.states.get(i), ph) / 1000.0 for r in stats.get(i, []) if r.get("mean") is not None}
    p, t, solar = kw(power_id, phase), {floor_hour(r["start"]): r["mean"] for r in stats.get(temp_id, []) if r.get("mean") is not None}, [kw(i, Phase.all) for i in solar_ids]
    standby = float(np.median([v for v in p.values() if v < 0.1] or [0.0]))

    # outdoor temperature, global irradiance and wind of the same hours from the weather archive
    params = {"latitude": lat, "longitude": lon, "start_date": begin.date().isoformat(), "end_date": now.date().isoformat(), "hourly": "temperature_2m,shortwave_radiation,wind_speed_10m", "wind_speed_unit": "ms", "timezone": "UTC"}
    try:
        async with async_get_clientsession(hass).get("https://archive-api.open-meteo.com/v1/archive", params=params) as r:
            h = (await r.json())["hourly"]
    except (aiohttp.ClientError, KeyError):
        return None
    wx = {datetime.fromisoformat(s).replace(tzinfo=timezone.utc): (o, sun or 0.0, w or 0.0) for s, o, sun, w in zip(h["time"], h["temperature_2m"], h["shortwave_radiation"], h["wind_speed_10m"]) if o is not None}

    # per whole day the means of heat (COP x power above standby), outdoor, indoor-outdoor, wind, sun (solar kW, else irradiance) and irradiance, and the room change
    days = defaultdict(list)
    useful = lambda e: e - (1 - _KNEE_COP) * max(0.0, e - _KNEE)  # the power that gives heat at the full COP: above the knee only part of it, as in the plan
    for k in sorted(set(p) & set(t) & set(wx)):
        days[k.date()].append((_cop(wx[k][0], t[k]) * useful(max(0.0, p[k] - standby)), wx[k][0], t[k] - wx[k][0], wx[k][2], sum(s.get(k, 0.0) for s in solar) if solar else wx[k][1], wx[k][1], t[k]))
    # the room change over the whole 24 hours: from the first hour of the day to the first hour of the next
    end = lambda d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)
    full = [(d, v) for d, v in days.items() if len(v) == 24 and end(d) in t]
    heat, out, dt, wind, sun, light = (np.array([np.mean([x[i] for x in v]) for _, v in full]) for i in range(6))
    change = np.array([t[end(d)] - v[0][6] for d, v in full])
    cold = out < 12.0  # on warm days the heat pump cannot go below zero, so the balance there says nothing
    cloudy = cold & (light < 40.0) & (heat > 0.2)  # little sun, so the loss is not mixed up with it, and the heat pump really heated the house
    if cloudy.sum() < _FIT_MIN_DAYS:
        return None

    # the loss on the cloudy days: heat = (ua + w * wind) * dt - q + c * room change / 24, with c the air plus mass in kWh per degC
    (ua, w, _, _), *_ = np.linalg.lstsq(np.c_[dt, dt * wind, -np.ones(len(dt)), change / 24][cloudy], heat[cloudy], rcond=None)
    # sun, free heat and storage on all cold days: the heat the loss asks for that the heat pump did not give
    (g, q, c), *_ = np.linalg.lstsq(np.c_[sun, np.ones(len(sun)), -change / 24][cold], ((ua + w * wind) * dt - heat)[cold], rcond=None)

    # the first hour after the heat pump stops at night only the air cools, by ua / ci of the indoor-outdoor difference
    hour, on = timedelta(hours=1), lambda k: p.get(k, 0.0) > standby + 0.1
    drops = [(t[k] - t[k + hour]) / (t[k] - wx[k][0]) for k in t if k + hour in t and k in wx and wx[k][1] == 0.0 and t[k] - wx[k][0] > 5.0 and on(k - hour) and not on(k) and not on(k + hour)]
    if len(drops) < 10 or not 0 < (ci := ua / np.median(drops)) < c:
        return None
    return float(ci), c - ci, float(ci * (c - ci) / (c * _MASS_HOURS)), float(ua), float(w), float(g), float(q)
