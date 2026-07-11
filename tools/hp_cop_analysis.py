# Run once compressor_hz has logged a while. From HA stats it: (1) fits compressor power vs Hz (clean,
# from your unit), (2) estimates a cycle-based COP from Tc (Thi-R) / Te (~outdoor), split heating/cooling
# via the supply-return air sign, and (3) an air-side cross-check at a reference airflow (scales with it).
#   usage: python tools/hp_cop_analysis.py [days] [period]   e.g. python tools/hp_cop_analysis.py 12 5minute
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_S = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL = _S["HA_URL"]
HA_TOKEN = _S["HA_TOKEN"]
WS = HA_URL.replace("http", "ws", 1) + "/api/websocket"

HZ = "sensor.fs_ahu_mhi_compressor_hz"
ELE = "sensor.fs_evcharger_sdm220m_total_power"
R1, R2, R3 = "sensor.fs_ahu_mhi_thi_r1_temperature", "sensor.fs_ahu_mhi_thi_r2_temperature", "sensor.fs_ahu_mhi_thi_r3_temperature"
OUT = "sensor.fs_ahu_mhi_outside_temperature"
SUP, RET = "sensor.fs_ahu_mhi_supply_temperature", "sensor.fs_ahu_mhi_return_temperature"
IDS = [HZ, ELE, R1, R2, R3, OUT, SUP, RET]

ETA = 0.45          # fraction of Carnot the real cycle reaches (tune once data exists)
TE_OFFSET = 5.0     # heating: evaporating temp ~ outdoor - this
TC_OFFSET = 8.0     # cooling: condensing temp ~ outdoor + this
AIRFLOW_REF = 1200  # m3/h at the operating point for the air-side cross-check (COP scales linearly with this)


def _hr(x):
    return datetime.fromtimestamp(x / 1000, timezone.utc) if isinstance(x, (int, float)) else x


async def main():
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 12
    period = sys.argv[2] if len(sys.argv) > 2 else "5minute"
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
            await ws.receive_json()
            await ws.send_json({"id": 2, "type": "recorder/statistics_during_period",
                                "start_time": start.isoformat(), "end_time": end.isoformat(),
                                "statistic_ids": IDS, "period": period, "types": ["mean"]})
            while True:
                m = await ws.receive_json()
                if m.get("id") == 2:
                    break
    res = m["result"]
    d = {sid: {_hr(r["start"]): r["mean"] for r in res.get(sid, []) if r.get("mean") is not None} for sid in IDS}
    for sid in (HZ, ELE):
        if not d[sid]:
            print(f"{sid}: NO DATA yet -> run again once it has logged ({period}, {days}d window).")
            return
    hrs = sorted(set(d[HZ]) & set(d[ELE]) & set(d[R1]) & set(d[OUT]) & set(d[SUP]) & set(d[RET]))
    run = [k for k in hrs if d[HZ][k] > 5 and abs(d[ELE][k]) > 50]
    print(f"window {period} {days:.0f}d: {len(hrs)} samples, {len(run)} with compressor running")
    if len(run) < 30:
        print("insufficient running samples for a fit -> let it log longer.")
        return

    hz = np.array([d[HZ][k] for k in run])
    pw = np.array([abs(d[ELE][k]) for k in run])
    c2, c1, c0 = np.polyfit(hz, pw, 2)
    pred = c2 * hz ** 2 + c1 * hz + c0
    r2 = 1.0 - np.sum((pw - pred) ** 2) / np.sum((pw - pw.mean()) ** 2)
    print(f"\ncompressor power vs Hz:  P[W] = {c2:.4f}*Hz^2 + {c1:.2f}*Hz + {c0:.0f}   (R2={r2:.3f})")
    print(f"  Hz range {hz.min():.0f}-{hz.max():.0f}, P range {pw.min():.0f}-{pw.max():.0f} W")

    # cycle-based COP per sample (split heating/cooling by supply-return sign), bucketed by outdoor
    rows = []
    for k in run:
        coil = max(d[R1][k], d[R2].get(k, -99), d[R3].get(k, -99)) if d[SUP][k] > d[RET][k] else min(d[R1][k], d[R2].get(k, 99), d[R3].get(k, 99))
        out, p = d[OUT][k], abs(d[ELE][k])
        heating = d[SUP][k] > d[RET][k]
        if heating:
            tc, te = coil + 273.15, (out - TE_OFFSET) + 273.15
            cop = ETA * tc / max(1.0, tc - te)
        else:
            te, tc = coil + 273.15, (out + TC_OFFSET) + 273.15
            cop = ETA * te / max(1.0, tc - te)
        air = AIRFLOW_REF * 0.335 * abs(d[SUP][k] - d[RET][k]) / p   # air-side COP at AIRFLOW_REF (scales)
        rows.append((heating, out, cop, air))

    for label, heat in (("HEATING", True), ("COOLING", False)):
        sub = [r for r in rows if r[0] == heat]
        if not sub:
            continue
        print(f"\n{label}: n={len(sub)}  cycle-COP median={np.median([r[2] for r in sub]):.2f}  air-side(@{AIRFLOW_REF}m3/h) median={np.median([r[3] for r in sub]):.2f}")
        for lo, hi in ((-10, 0), (0, 5), (5, 10), (10, 20), (20, 40)):
            b = [r for r in sub if lo <= r[1] < hi]
            if b:
                print(f"  outdoor {lo:>3}..{hi:<2}: n={len(b)} cycle-COP={np.median([r[2] for r in b]):.2f} air-side={np.median([r[3] for r in b]):.2f}")


asyncio.run(main())
