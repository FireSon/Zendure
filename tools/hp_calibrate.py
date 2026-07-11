# Identify the house thermal battery from real nov-apr data.
# Demand: energy signature (daily P_elec vs T_in - T_out). Storage: fit the cooling time constant tau
# from cold HP-off stretches (capacity == 0), where ln(T_in - T_out) decays linearly with time.
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
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
CAP = "sensor.fs_ahu_mhi_capacity"


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


async def meteo_hourly() -> tuple[dict, dict]:
    params = {"latitude": LAT, "longitude": LON, "start_date": START, "end_date": END,
              "hourly": "temperature_2m,shortwave_radiation", "timezone": "UTC"}
    async with aiohttp.ClientSession() as s:
        async with s.get("https://archive-api.open-meteo.com/v1/archive", params=params) as r:
            h = (await r.json())["hourly"]
    tout, solar = {}, {}
    for t, tt, ss in zip(h["time"], h["temperature_2m"], h["shortwave_radiation"]):
        if tt is None or ss is None:
            continue
        k = _hr(t)
        tout[k], solar[k] = tt, ss
    return tout, solar


def _r2(y, pred):
    return 1.0 - np.sum((y - pred) ** 2) / np.sum((y - y.mean()) ** 2)


async def main() -> None:
    ha = await ha_hourly([T_IN, P_HP, CAP])
    tout, solar = await meteo_hourly()
    tin, php, cap = ha[T_IN], ha[P_HP], ha[CAP]
    hrs = sorted(set(tin) & set(tout) & set(cap) & set(php))

    # energy signature: daily-mean P_elec vs daily-mean (T_in - T_out) -> slope = UA/COP (kW_elec per degC)
    days: dict = {}
    for h in hrs:
        days.setdefault(h.date(), []).append((php[h] / 1000.0, tin[h] - tout[h]))
    dp = np.array([np.mean([p for p, _ in v]) for v in days.values()])
    dd = np.array([np.mean([x for _, x in v]) for v in days.values()])
    slope, icpt = np.linalg.lstsq(np.c_[dd, np.ones_like(dd)], dp, rcond=None)[0]
    print(f"window {START}..{END}: {len(hrs)} hours, {len(days)} days")
    print(f"energy signature: P_elec ~ {slope:.4f}*(T_in-T_out) {icpt:+.3f} kW, daily R2 = {_r2(dp, slope*dd+icpt):.3f}")
    print(f"  -> UA/COP ~ {slope*1000:.0f} W per degC")

    # cold HP-off stretches (capacity == 0) -> fit tau from the cooling decay
    runs, cur = [], []
    for h in hrs:
        if cap[h] < 1.0:
            cur = cur + [h] if (cur and h == cur[-1] + timedelta(hours=1)) else [h]
        else:
            if len(cur) >= 6:
                runs.append(cur)
            cur = []
    if len(cur) >= 6:
        runs.append(cur)

    print(f"\nHP-off stretches >=6h: {len(runs)}")
    taus = []
    for run in sorted(runs, key=len, reverse=True):
        tout_avg = float(np.mean([tout[h] for h in run]))
        xs = [i for i, h in enumerate(run) if tin[h] - tout[h] > 0.5]
        ys = [np.log(tin[h] - tout[h]) for h in run if tin[h] - tout[h] > 0.5]
        if tout_avg >= 10 or len(xs) < 5:
            continue
        sl = float(np.polyfit(xs, ys, 1)[0])
        drop = tin[run[0]] - tin[run[-1]]
        if sl < 0:
            tau = -1.0 / sl
            taus.append(tau)
            if len(taus) <= 8:
                print(f"  {run[0]:%Y-%m-%d %H}h  {len(run)}h  T_out~{tout_avg:4.1f}  T_in {tin[run[0]]:.1f}->{tin[run[-1]]:.1f} (drop {drop:+.1f})  tau~{tau:.0f}h")
    if taus:
        print(f"\ncold-decay tau: n={len(taus)}, median {np.median(taus):.0f} h, range {min(taus):.0f}..{max(taus):.0f} h")
    else:
        print("\nno usable cold-decay stretches (off-periods too mild or too short)")


asyncio.run(main())
