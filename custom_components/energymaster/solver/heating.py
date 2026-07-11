from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

import numpy as np

from .solver import LpSolver, LpColumn, SolverEntry
from ..const import Phase

_COMFORT_PENALTY = 50.0  # EUR per degC-quarter of house comfort shortfall (soft floor; lets DHW win later)


class HeatType(str, Enum):
    house = "house"    # space heating: soft comfort floor, loses heat to the outdoor forecast
    boiler = "boiler"  # domestic hot water: hard band, loses heat to a fixed room temperature
    pool = "pool"      # pool: hard band, loses heat to a fixed ambient temperature


class Heating(SolverEntry):
    # One heating device = one thermal store (temperature is the state of charge) charged by electrical power, with a calibrated RC model. A DHW boiler on the same HP comes later as an optional second store.
    def __init__(self) -> None:
        super().__init__()
        self.name = ""
        self.id = ""
        self.kind = HeatType.house
        self.source = "hp"  # "hp" (COP learned via slope) or "electric" (COP=1)
        self.phase = Phase.all
        self.temp_entity = ""
        self.max_power = 2.5  # electrical source cap (kW)
        self.t_min = 19.0  # floor: soft for house, hard for boiler/pool
        self.t_max = 22.0
        self.t_target = 20.0
        self.t_abs_min = 16.0  # hard house floor; below t_min is allowed only at a comfort penalty
        self.t_min_night = 18.0  # night setback of the soft house floor
        self.ambient = 20.0  # fixed loss temperature (boiler/pool); house loses to the outdoor forecast
        self.ramp_kw = 1.0  # max power change per quarter (0 = no ramp limit)
        # calibrated / runtime params (not in the config)
        self.slope = 0.12  # UA/COP: degC rise per kW of electrical input
        self.icept = 0.0  # free-gain offset
        self.tau = 40.0  # cooling time constant (quarters)
        self._temp0 = 20.0  # live temperature, pinned at slot 0 (pushed in via track)
        self._tmin = [self.t_min] * 96  # per-slot house floor (night setback), rebuilt each tick
        self._last_cal: datetime | None = None
        self.power: LpColumn = None  # electrical draw column (kW per slot)
        self.temp: LpColumn = None  # store temperature column (state of charge)
        self.slack: LpColumn = None  # soft-floor shortfall column (house only)

    def entry_config(self, lp: LpSolver) -> None:
        self.kind = HeatType(self.kind)  # accept the plain string value coming from the config
        soft = self.kind is HeatType.house
        hard_min = self.t_abs_min if soft else self.t_min  # house keeps a hard floor below the soft comfort floor
        if soft:
            self._outdoor = lp.temp  # house loses heat to the outdoor forecast
        lp.track(self.temp_entity, lambda v: setattr(self, "_temp0", v))  # keep the live temperature fresh

        # electrical draw column (0..max_power), tagged on its phase so the solver's phase-limit row includes it
        self.power = lp.column(hi=self.max_power, tags=("draw", self.phase.name), dest=lp.data(f"{self.id}_power", "kW", name=self.name))
        # temperature state. House has no upper bound: with no cooling path a ceiling is just infeasible in summer.
        # Boiler/pool keep the hard band [hard_min, t_max]. Slot 0 is pinned to the live temp in prerun.
        self.temp = lp.column(lo=hard_min, dest=lp.data(f"{self.id}_temp", "C", name=self.name)) if soft else lp.column(lo=hard_min, hi=self.t_max, dest=lp.data(f"{self.id}_temp", "C", name=self.name))
        if soft:
            # soft comfort floor: temp + slack >= t_min, where falling below t_min costs _COMFORT_PENALTY per degC of slack
            self.slack = lp.column(cost=_COMFORT_PENALTY, lo=0.0)
            lp.rowQuarter({self.temp: 1.0, self.slack: 1.0}, lo=lambda t: self._tmin[t])

        # discretized RC model -> temp[t+1] = (1-B)*temp[t] + A*power[t] + B*loss_temp + icept*A
        A = 0.25 / (self.tau * self.slope)  # degC gained per kW of electrical input over one quarter
        B = 0.25 / self.tau  # fraction of the gap to the loss temperature shed per quarter
        G = self.icept * A  # free-gain contribution per quarter
        loss = (lambda t: B * self._outdoor.data[12 + t] + G) if soft else (B * self.ambient + G)
        lp.rowDelta(lambda t1, t2: self.temp[t1] + (B - 1.0) * self.temp[t2] - A * self.power[t2], const=loss)
        if self.ramp_kw:  # bound how fast the power may change between consecutive quarters
            lp.rowDelta(lambda t1, t2: self.power[t1] - self.power[t2], lo=-self.ramp_kw, hi=self.ramp_kw)

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        if self.kind is HeatType.house:  # rebuild the soft floor with a lower setpoint at night
            self._tmin = [self.t_min_night if (h := (now + timedelta(minutes=15 * t)).hour) >= 22 or h < 6 else self.t_min for t in range(lp.horizon)]
        self.temp.lower[0] = self.temp.upper[0] = min(max(self._temp0, self.temp.lower[1]), self.temp.upper[1])  # pin slot 0, clamped into the band so an out-of-band live temp cannot make the LP infeasible
        if lp.hass is None:
            return
        if self._last_cal is None or (now - self._last_cal).days >= 7:  # self-calibrate on cold start and weekly
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

    async def calibrate(self, lp: LpSolver, now: datetime) -> None:
        # fit the RC model (slope, icept, tau) from recent history; house loses to the outdoor forecast (ambient=None)
        lat = lp.globals.get("latitude", 0.0)
        lon = lp.globals.get("longitude", 0.0)
        ambient = None if self.kind is HeatType.house else self.ambient
        res = await fit_store(lp.hass, self.power_entity, self.temp_entity, lat, lon, ambient=ambient)
        if res is not None:
            self.slope, self.icept = res[0], res[1]
            if res[2] is not None:
                self.tau = res[2]
        await self.save_params(lp.hass, {"slope": self.slope, "icept": self.icept, "tau": self.tau})


_CAL_MIN_DAYS = 20   # minimum heating-active days before a refit is trusted (off-season gate)


def floor_hour(x) -> datetime:
    # recorder stat 'start' (unix seconds in-process, ms via WS) or iso string -> UTC hour
    dt = datetime.fromtimestamp(x / 1000 if x > 1e12 else x, timezone.utc) if isinstance(x, (int, float)) else datetime.fromisoformat(str(x).replace("Z", "+00:00")).astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


async def fit_store(hass, elec_id: str, temp_id: str, lat: float, lon: float, ambient: float | None = None, days: int = 90):
    # Self-calibrate a thermal store from recorder history. Returns (slope, icept, tau) or None when there
    # is too little heating signal in the window (off-season -> caller keeps its current params).
    #   slope = UA/COP (kW_elec/degC), icept = free-gain offset (kW_elec), tau = cooling time constant (h).
    # ambient None -> loss vs the outdoor archive (space heating); a number -> loss vs a fixed room temp (tank).
    import aiohttp
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    if not elec_id or not temp_id:
        return None
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, now, {elec_id, temp_id}, "hour", None, {"mean"})
    pwr = {floor_hour(r["start"]): r["mean"] / 1000.0 for r in stats.get(elec_id, []) if r.get("mean") is not None}
    tmp = {floor_hour(r["start"]): r["mean"] for r in stats.get(temp_id, []) if r.get("mean") is not None}

    if ambient is None:
        params = {"latitude": lat, "longitude": lon, "start_date": start.date().isoformat(),
                  "end_date": now.date().isoformat(), "hourly": "temperature_2m", "timezone": "UTC"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://archive-api.open-meteo.com/v1/archive", params=params) as r:
                    h = (await r.json())["hourly"]
        except (aiohttp.ClientError, KeyError):
            return None
        ref = {floor_hour(t): tt for t, tt in zip(h["time"], h["temperature_2m"]) if tt is not None}
    else:
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

    # tau from off-stretches (P~0) with a real gradient, at night; cold-gated only for outdoor loss
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
        if ambient is None and np.mean([ref[k] for k in run]) >= 8.0:
            continue
        xy = [(i, np.log(tmp[k] - ref[k])) for i, k in enumerate(run) if tmp[k] - ref[k] > 0.5 and ((k.hour >= 22) or (k.hour < 6))]
        if len(xy) >= 5:
            sl = float(np.polyfit([i for i, _ in xy], [y for _, y in xy], 1)[0])
            if sl < 0:
                taus.append(-1.0 / sl)
    tau = float(min(120.0, max(10.0, np.median(taus)))) if taus else None
    return slope, icept, tau
