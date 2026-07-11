# Indicative air-to-air heating COP from winter logs: Q = airflow * 0.335 * dT_air, COP = Q / P_elec.
# airflow ~ rpm (fan affinity law); absolute level assumes A_MAX m3/h at the observed max rpm -> COP scales
# linearly with A_MAX. Supply/return air temps + blower rpm + sdm220m electrical.
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_S = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL = _S["HA_URL"]
HA_TOKEN = _S["HA_TOKEN"]
WS = HA_URL.replace("http", "ws", 1) + "/api/websocket"

SUP = "sensor.fs_ahu_mhi_supply_temperature"
RET = "sensor.fs_ahu_mhi_return_temperature"
BSP = "sensor.fs_ahu_multicalor_blower_speed"
ELE = "sensor.fs_evcharger_sdm220m_total_power"
OUT = "sensor.fs_ahu_mhi_outside_temperature"
A_NOM = 2250.0   # MC-EE-30 nominal airflow m3/h at 100% blower; operating airflow = speed% * A_NOM
AIR = 0.335      # Wh per m3 per degC


def _hr(x):
    return datetime.fromtimestamp(x / 1000, timezone.utc) if isinstance(x, (int, float)) else x


async def main():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 1, tzinfo=timezone.utc)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
            await ws.receive_json()
            await ws.send_json({"id": 2, "type": "recorder/statistics_during_period",
                                "start_time": start.isoformat(), "end_time": end.isoformat(),
                                "statistic_ids": [SUP, RET, BSP, ELE, OUT], "period": "hour", "types": ["mean"]})
            while True:
                m = await ws.receive_json()
                if m.get("id") == 2:
                    break
    res = m["result"]
    d = {sid: {_hr(r["start"]): r["mean"] for r in res.get(sid, []) if r.get("mean") is not None} for sid in (SUP, RET, BSP, ELE, OUT)}
    for sid in (SUP, RET, BSP, ELE, OUT):
        v = list(d[sid].values())
        print(f"{sid.split('.')[-1]}: {'NO DATA' if not v else f'n={len(v)} min={min(v):.1f} max={max(v):.1f} avg={sum(v)/len(v):.1f}'}")

    hrs = sorted(set(d[SUP]) & set(d[RET]) & set(d[BSP]) & set(d[ELE]))
    if not hrs:
        print("\nno overlapping hours")
        return
    rows = []
    for k in hrs:
        dT = d[SUP][k] - d[RET][k]       # heating: supply warmer than return
        spd = d[BSP][k]                  # blower speed %
        p = abs(d[ELE][k])               # electrical W (sdm is inverted/negative)
        if dT > 1.0 and spd > 5 and p > 50:
            airflow = (spd / 100.0) * A_NOM
            q = airflow * AIR * dT       # thermal W
            rows.append((q / p, d[OUT].get(k, float("nan"))))   # COP, outdoor
    if not rows:
        print("\nno heating hours (supply>return) in window")
        return
    cop = np.array([r[0] for r in rows])
    print(f"\nheating COP (airflow = blower% * {A_NOM:.0f} m3/h):")
    print(f"  n={len(cop)} median={np.median(cop):.2f} p10={np.percentile(cop,10):.2f} p90={np.percentile(cop,90):.2f}")
    for lo, hi in ((-10, 0), (0, 5), (5, 10), (10, 20)):
        band = [c for c, o in rows if lo <= o < hi]
        if band:
            print(f"  outdoor {lo:>3}..{hi:<2}: n={len(band)} COP={np.median(band):.2f}")


asyncio.run(main())
