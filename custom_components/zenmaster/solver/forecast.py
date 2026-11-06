from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .solver import LpSolver, SolverEntry
from ..config import ConfigField
from ..const import watt_factor

_LOGGER = logging.getLogger(__name__)
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT = ClientTimeout(total=15)
_CAL_DAYS = 10  # calibration window: days of history used to learn the POA factor


@dataclass
class SolarPanel:
    name: str
    power_entity: str = ""  # live power + the meter the POA factor is learned on
    derate_entity: str = ""  # number entity to throttle the inverter; empty = cannot curtail

    CONFIG = [
        ConfigField("name", {"text": {}}, cast=str, is_key=True),
        ConfigField("power_entity", {"entity": {}}, cast=str, required=False),
        ConfigField("derate_entity", {"entity": {}}, cast=str, required=False),
    ]


class Forecast(SolverEntry):
    # Solar forecast per production meter via a learned POA factor: power ~ a[quarter]*dni + C*dhi,
    # calibrated on the last 10 days. a is per quarter-of-day (direct term), C is global (diffuse term).
    # A meter without history yields nothing.
    def __init__(self, config=None, hass=None):
        super().__init__()
        config = config or {}
        self.hass = hass  # needed for recorder history; None offline -> no calibration
        self.lat = hass.config.latitude if hass else config.get("latitude", 0.0)
        self.lon = hass.config.longitude if hass else config.get("longitude", 0.0)
        self._house_tz = ZoneInfo(hass.config.time_zone if hass else config.get("timezone", "UTC"))
        self.solar_panels = [SolarPanel(name=p.get("name", ""), power_entity=p.get("power_entity", ""), derate_entity=p.get("derate_entity", "")) for p in config.get("solar_panels", [])]
        self._poa = {}  # power_entity -> (a: np.ndarray[96] direct coeff per quarter, C: float diffuse coeff)
        self._last_cal = None  # date of the last calibration

    def entry_config(self, lp: LpSolver):
        # publish location on the shared blackboard so other entries (e.g. Heating.calibrate) can fetch weather
        lp.globals["latitude"] = self.lat
        lp.globals["longitude"] = self.lon

        # Solver columns; hi=0 so a failed meteo fetch means "no solar", not an unbounded free supply
        self.solar_lp = lp.column(hi=0.0, tags=("supply",))
        # the panels of a battery feed that battery in the LP, not the house: their forecast per meter, kW per quarter
        self.meters: dict[str, np.ndarray] = {}
        lp.globals["solar_meters"] = self.meters
        lp.globals["solar_entities"] = [p.power_entity for p in self.solar_panels if p.power_entity]  # the house's sun gain calibrates on these

        # Forecast buffers the solver reads (7-day horizon, 15-min slots).
        self.solar = lp.solar = lp.data("solar", "kW", 7, source="meteo")
        self.temp = lp.temp = lp.data("temp", "C", 7, source="meteo")
        self.wind = lp.wind = lp.data("wind", "m/s", 7, source="meteo")
        self.sun = lp.sun = lp.data("sun", "W/m2", 7, source="meteo")  # global irradiance, the solar gain of the house

    async def entry_prerun(self, lp: LpSolver, now: datetime):
        # Recalibrate the POA once a day; blocking on cold start (first solve needs it), background after.
        today = now.astimezone(timezone.utc).date()
        if today != self._last_cal:
            if self._poa and self.hass is not None:
                self.hass.async_create_task(self.calibrate(now))
            else:
                await self.calibrate(now)

        # the meters of a battery's own panels, every meter not on the house's AC side, start at zero so a failed fetch means no solar there either;
        # without the AC list every meter counts as house solar, as before
        ac = lp.globals.get("solar_ac")
        self.meters.clear()
        for meter in self._poa:
            if ac is not None and meter not in ac:
                self.meters[meter] = np.zeros(lp.horizon)

        # Fetch the Open-Meteo 15-min forecast (7 days) and fill the buffers from 'now' forward.
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "minutely_15": "direct_normal_irradiance,diffuse_radiation,shortwave_radiation,temperature_2m,wind_speed_10m",
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "forecast_minutely_15": 672,
        }
        try:
            session = async_get_clientsession(self.hass)  # HA's shared session, never closed here
            async with session.get(_OPEN_METEO_URL, params=params, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                m15 = (await resp.json())["minutely_15"]
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as err:
            _LOGGER.warning("prerun meteo fetch: %s", err)
            return

        # offset = index of 'now' in the payload, so buffer slot s maps to payload index offset+s.
        start = datetime.fromisoformat(m15["time"][0]).replace(tzinfo=timezone.utc)
        offset = int((now.astimezone(timezone.utc) - start).total_seconds() // 900)  # 900s = 15 min
        for t in range(self.solar.data.size - 12):
            s = 12 + t
            i = offset + t
            if i >= len(m15["time"]):
                break
            self.temp.data[s] = m15["temperature_2m"][i]
            self.wind.data[s] = m15["wind_speed_10m"][i]
            self.sun.data[s] = m15["shortwave_radiation"][i]
            # Solar = sum over meters of the learned POA factor applied to this slot's irradiance.
            dni = m15["direct_normal_irradiance"][i]
            dhi = m15["diffuse_radiation"][i]
            tu = (now + timedelta(minutes=15 * t)).astimezone(timezone.utc)
            q = tu.hour * 4 + tu.minute // 15  # quarter-of-day (UTC), index into the learned a[]
            per = {meter: max(0.0, a[q] * dni + C * dhi) for meter, (a, C) in self._poa.items()}
            self.solar.data[s] = sum(per.values())

            # Set the LpColumn cost; a battery's own panels go to that battery, only the rest is free supply on the house
            if t < lp.horizon:
                for meter, series in self.meters.items():
                    series[t] = per[meter]
                self.solar_lp.upper[t] = self.solar.data[s] - sum(per[meter] for meter in self.meters)

    async def calibrate(self, now):
        # Called daily: learn the POA per production meter from the last 10 days. No history -> meter dropped.
        self._last_cal = now.astimezone(timezone.utc).date()  # marked up-front: a failed fetch retries tomorrow, not every tick
        irr = await self._hist_irr(now)
        if not irr:
            return
        poa = {}
        for meter in {p.power_entity for p in self.solar_panels if p.power_entity}:
            fit = self._fit_poa(await self._hist_power(meter, now), irr)
            if fit is not None:
                poa[meter] = fit
        self._poa = poa
        _LOGGER.info("solar POA calibrated for %d meter(s)", len(poa))

    async def _hist_irr(self, now):
        # dni/dhi per quarter for the last 10 days (the Open-Meteo forecast API also serves the recent past).
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "minutely_15": "direct_normal_irradiance,diffuse_radiation",
            "timezone": "UTC",
            "start_date": (now - timedelta(days=_CAL_DAYS)).date().isoformat(),
            "end_date": now.date().isoformat(),
        }
        try:
            session = async_get_clientsession(self.hass)  # HA's shared session, never closed here
            async with session.get(_OPEN_METEO_URL, params=params, timeout=ClientTimeout(total=40)) as resp:
                resp.raise_for_status()
                b = (await resp.json())["minutely_15"]
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as err:
            _LOGGER.warning("calibrate irr fetch: %s", err)
            return {}
        out = {}
        for t, dni, dhi in zip(b["time"], b["direct_normal_irradiance"], b["diffuse_radiation"]):
            if dni is None or dhi is None:
                continue
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            out[(dt.date(), dt.hour * 4 + dt.minute // 15)] = (dni, dhi)
        return out

    async def _hist_power(self, meter, now):
        # Measured power (kW) per quarter from the recorder (5-min mean) over the last 10 days.
        if self.hass is None:
            return {}
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        stats = await get_instance(self.hass).async_add_executor_job(statistics_during_period, self.hass, now - timedelta(days=_CAL_DAYS), now, {meter}, "5minute", None, {"mean"})
        # Average the (up to 3) 5-min means per quarter, then convert to kW.
        acc = {}
        for row in stats.get(meter, []):
            mean = row.get("mean")
            if mean is None:
                continue
            st = row.get("start")
            dt = datetime.fromtimestamp(st, timezone.utc) if isinstance(st, (int, float)) else st
            a = acc.setdefault((dt.date(), dt.hour * 4 + dt.minute // 15), [0.0, 0])
            a[0] += mean
            a[1] += 1
        factor = watt_factor(self.hass.states.get(meter)) / 1000.0  # the meter's own unit to kW
        return {k: v[0] / v[1] * factor for k, v in acc.items() if v[1]}

    def _fit_poa(self, power, irr):
        # Joint least-squares fit: power ~ a[quarter]*dni + C*dhi (C shared/global). None if too little overlap.
        rows = []
        y = []
        for (d, q), (dni, dhi) in irr.items():
            p = power.get((d, q))
            if p is None or dni + dhi <= 0:
                continue
            rows.append((q, dni, dhi))
            y.append(p)
        if len(rows) < 50:
            return None
        # Design matrix: one dni column per quarter (direct, time-varying) + one shared dhi column (diffuse).
        used = sorted({q for q, _, _ in rows})
        idx = {q: i for i, q in enumerate(used)}
        A = np.zeros((len(rows), len(used) + 1))
        for r, (q, dni, dhi) in enumerate(rows):
            A[r, idx[q]] = dni
            A[r, -1] = dhi
        coef = np.linalg.lstsq(A, np.array(y), rcond=None)[0]
        a = np.zeros(96)
        for q in used:
            a[q] = coef[idx[q]]
        return a, float(coef[-1])
