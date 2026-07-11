# Indicative house-as-battery backtest over nov-apr: per day, an LP picks the indoor-temp trajectory
# within the comfort band to minimise day-ahead electricity cost, vs what the house actually spent.
# Uses only measured quantities: UA/COP = 0.121 kW_elec/degC (energy signature) and tau (night decay).
#   P_elec[t] = (UA/COP)*(T[t]-T_out[t]) + (C/COP)*(T[t+1]-T[t])/dt,  C/COP = tau*(UA/COP), dt = 1h.
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import highspy
import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_S = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL = _S["HA_URL"]
HA_TOKEN = _S["HA_TOKEN"]
WS = HA_URL.replace("http", "ws", 1) + "/api/websocket"

LAT, LON = 52.07704264717319, 5.499350541375222
START, END = "2025-11-01", "2026-04-30"
T_IN = "sensor.fs_ahu_verventa_temperature"
P_HP = "sensor.fs_evcharger_fs_ahu_heating_power_actual"

SLOPE = 0.121      # energy signature: P_elec = SLOPE*(T_in-T_out) - ICEPT  (kW_elec per degC)
ICEPT = 0.886      # signature intercept (free solar/internal gains); kept so modelled energy matches reality
PMAX = 2.5         # max electrical kW
T_MAX, T_MIN_DAY, T_MIN_NIGHT = 22.0, 19.0, 18.0
INF = highspy.kHighsInf


def _hr(x) -> datetime:
    if isinstance(x, (int, float)):
        dt = datetime.fromtimestamp(x / 1000, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


async def ha_hourly(ids: list[str]) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
            await ws.receive_json()
            await ws.send_json({"id": 2, "type": "recorder/statistics_during_period",
                                "start_time": f"{START}T00:00:00+00:00", "end_time": f"{END}T23:59:59+00:00",
                                "statistic_ids": ids, "period": "hour", "types": ["mean"]})
            while True:
                m = await ws.receive_json()
                if m.get("id") == 2:
                    break
    return {sid: {_hr(r["start"]): r["mean"] for r in rows if r.get("mean") is not None}
            for sid, rows in m["result"].items()}


async def meteo_hourly() -> dict:
    params = {"latitude": LAT, "longitude": LON, "start_date": START, "end_date": END,
              "hourly": "temperature_2m", "timezone": "UTC"}
    async with aiohttp.ClientSession() as s:
        async with s.get("https://archive-api.open-meteo.com/v1/archive", params=params) as r:
            h = (await r.json())["hourly"]
    return {_hr(t): tt for t, tt in zip(h["time"], h["temperature_2m"]) if tt is not None}


async def prices_hourly() -> dict:
    url = f"https://api.energy-charts.info/price?bzn=NL&start={START}&end={END}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            j = await r.json()
    acc: dict = {}
    for ts, p in zip(j["unix_seconds"], j["price"]):
        k = _hr(ts * 1000)
        acc.setdefault(k, []).append(p / 1000.0)  # EUR/MWh -> EUR/kWh
    return {k: float(np.mean(v)) for k, v in acc.items()}


def _t_min(h: datetime) -> float:
    return T_MIN_NIGHT if h.hour >= 22 or h.hour < 6 else T_MIN_DAY


def solve_day(price, tout, hours, t0, tau):
    # temp-driven model: T[t+1] = T[t] + A*P[t] - (1/tau)*(T[t]-T_out[t]) + Gn, P the electrical kW.
    # A and Gn follow from the measured signature (slope, intercept) and tau; no COP needed.
    A = (1.0 / SLOPE) / tau          # degC per kWh_elec
    B = 1.0 / tau                    # loss fraction per hour
    Gn = (ICEPT / SLOPE) / tau       # free gain per hour (degC)
    nT, nP = 25, 24
    n = nT + nP
    c = np.zeros(n)
    lo = np.zeros(n)
    hi = np.zeros(n)
    lo[0] = hi[0] = t0
    for k in range(1, 25):
        lo[k], hi[k] = _t_min(hours[k - 1] + timedelta(hours=1)), max(T_MAX, t0)
    for t in range(24):
        lo[nT + t], hi[nT + t] = 0.0, PMAX
        c[nT + t] = price[t]
    start, idx, val, rl = [0], [], [], []
    for t in range(24):
        # T[t+1] + (B-1)*T[t] - A*P[t] = B*T_out[t] + Gn
        idx += [t + 1, t, nT + t]
        val += [1.0, B - 1.0, -A]
        start.append(len(val))
        rl.append(B * tout[t] + Gn)
    lp = highspy.HighsLp()
    lp.num_col_, lp.num_row_ = n, 24
    lp.col_cost_, lp.col_lower_, lp.col_upper_ = c, lo, hi
    lp.row_lower_ = lp.row_upper_ = np.array(rl)
    lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
    lp.a_matrix_.start_, lp.a_matrix_.index_, lp.a_matrix_.value_ = np.array(start), np.array(idx), np.array(val, float)
    H = highspy.Highs()
    H.setOptionValue("output_flag", False)
    H.passModel(lp)
    H.run()
    if H.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return None
    x = np.array(H.getSolution().col_value)
    return x[:25], x[nT:nT + 24]


async def main() -> None:
    ha = await ha_hourly([T_IN, P_HP])
    tout = await meteo_hourly()
    price = await prices_hourly()
    tin, php = ha[T_IN], ha[P_HP]

    # group full UTC days that have prices + outdoor for all 24h and a start temp
    by_day: dict = {}
    for h in sorted(set(tout) & set(price) & set(php)):
        by_day.setdefault(h.date(), []).append(h)
    days = [d for d, hs in by_day.items() if len(hs) == 24 and hs[0] in tin]

    for tau in (25, 40, 55):
        act_cost = lp_cost = act_kwh = lp_kwh = 0.0
        act_t = lp_t = 0.0
        nslot = solved = skipped = 0
        for d in days:
            hs = by_day[d]
            pr = np.array([price[h] for h in hs])
            to = np.array([tout[h] for h in hs])
            pa = np.array([php[h] / 1000.0 for h in hs])
            res = solve_day(pr, to, hs, tin[hs[0]], tau)
            if res is None:
                skipped += 1
                continue
            T, P = res
            solved += 1
            act_cost += float(np.sum(pr * pa))
            lp_cost += float(np.sum(pr * P))
            act_kwh += float(np.sum(pa))
            lp_kwh += float(np.sum(P))
            act_t += float(np.sum([tin[h] for h in hs if h in tin]))
            lp_t += float(np.sum(T[1:]))
            nslot += 24
        sav = 100.0 * (act_cost - lp_cost) / act_cost if act_cost else 0.0
        print(f"tau={tau:>2}h: actual EUR {act_cost:7.2f} | LP EUR {lp_cost:7.2f} | savings {sav:5.1f}%"
              f" | kWh act {act_kwh:6.0f} vs lp {lp_kwh:6.0f}"
              f" | avg T act {act_t/nslot:.1f} vs lp {lp_t/nslot:.1f} | days {solved} (skip {skipped})")


asyncio.run(main())
