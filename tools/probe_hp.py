from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_S = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL = _S["HA_URL"]
HA_TOKEN = _S["HA_TOKEN"]
WS = HA_URL.replace("http", "ws", 1) + "/api/websocket"

CAP = "sensor.fs_ahu_mhi_capacity"
SDM = "sensor.fs_evcharger_sdm220m_total_power"
HPA = "sensor.fs_evcharger_fs_ahu_heating_power_actual"
MULTI = "sensor.multicalor_mhi_power_usage"


def _key(x):
    return datetime.fromtimestamp(x / 1000, timezone.utc) if isinstance(x, (int, float)) else x


async def main():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)   # winter: continuous running -> representative COP
    end = datetime(2026, 4, 1, tzinfo=timezone.utc)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
            await ws.receive_json()
            await ws.send_json({"id": 2, "type": "recorder/statistics_during_period",
                                "start_time": start.isoformat(), "end_time": end.isoformat(),
                                "statistic_ids": [CAP, SDM, HPA, MULTI], "period": "hour", "types": ["mean"]})
            while True:
                m = await ws.receive_json()
                if m.get("id") == 2:
                    break
    res = m["result"]
    cap = {_key(r["start"]): r["mean"] for r in res.get(CAP, []) if r.get("mean") is not None}
    sdm = {_key(r["start"]): r["mean"] for r in res.get(SDM, []) if r.get("mean") is not None}
    hpa = {_key(r["start"]): r["mean"] for r in res.get(HPA, []) if r.get("mean") is not None}
    multi = {_key(r["start"]): r["mean"] for r in res.get(MULTI, []) if r.get("mean") is not None}

    for name, d in (("mhi_capacity", cap), ("sdm220m_total_power", sdm), ("heating_power_actual", hpa), ("multicalor_mhi_power", multi)):
        v = np.array(list(d.values()))
        if len(v) == 0:
            print(f"{name}: NO DATA")
            continue
        print(f"{name}: n={len(v)} min={v.min():.2f} max={v.max():.2f} avg={v.mean():.2f}")

    # capacity step histogram (rounded to nearest 1)
    hist = Counter(round(x) for x in cap.values())
    print("\ncapacity value histogram (rounded, top 20 by count):")
    for val, cnt in sorted(hist.items()):
        if cnt >= max(hist.values()) * 0.01:
            print(f"  {val:>3} : {cnt}")

    # COP: pair capacity with electrical (sdm is negative for consumption -> use -sdm), unit running
    pairs = [(cap[k], -sdm[k] / 1000.0) for k in cap.keys() & sdm.keys() if cap[k] > 1 and sdm[k] < -50]
    if pairs:
        c = np.array([p[0] for p in pairs]); e = np.array([p[1] for p in pairs])
        cop = c / e
        print(f"\nCOP = capacity / (|sdm_total_power|/1000): n={len(pairs)}")
        print(f"  median {np.median(cop):.2f}, p10 {np.percentile(cop,10):.2f}, p90 {np.percentile(cop,90):.2f}")
        for lo, hi in ((1, 7), (7, 11), (11, 16), (16, 70)):
            band = [(cc, ee) for cc, ee in zip(c, e) if lo <= cc < hi]
            if band:
                bc = np.array([x for x, _ in band]); be = np.array([y for _, y in band])
                print(f"  cap step ~{lo}-{hi}: n={len(band)} avg_cap={bc.mean():.1f} avg_elec_kW={be.mean():.3f} COP={np.mean(bc/be):.2f}")

    mp = [(cap[k], abs(multi[k])) for k in cap.keys() & multi.keys() if cap[k] > 1 and abs(multi[k]) > 1e-6]
    if mp:
        mc = np.array([p[0] for p in mp]); me = np.array([p[1] for p in mp])
        me_kw = me / (1000.0 if me.mean() > 50 else 1.0)
        print(f"\nCOP = capacity / multicalor_mhi_power: n={len(mp)} median {np.median(mc / me_kw):.2f}")
        for lo, hi in ((1, 7), (7, 11), (11, 16), (16, 70)):
            band = [(cc, ee) for cc, ee in zip(mc, me_kw) if lo <= cc < hi]
            if band:
                bc = np.array([x for x, _ in band]); be = np.array([y for _, y in band])
                print(f"  cap ~{lo}-{hi}: n={len(band)} avg_cap={bc.mean():.1f} avg_kW={be.mean():.3f} COP={np.mean(bc / be):.2f}")


asyncio.run(main())
