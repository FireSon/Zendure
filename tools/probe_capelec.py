# Does compressor capacity drive electrical consumption? 2D bins: outdoor temp x capacity -> median
# electrical (and n). If electrical rises with capacity WITHIN an outdoor band, capacity controls draw
# (LP can modulate heat); if flat, capacity is decoupled from draw (problem for the LP control).
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

CAP = "sensor.fs_ahu_mhi_capacity"
ELE = "sensor.fs_evcharger_sdm220m_total_power"
OUT = "sensor.fs_ahu_mhi_outside_temperature"


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
                                "statistic_ids": [CAP, ELE, OUT], "period": "hour", "types": ["mean"]})
            while True:
                m = await ws.receive_json()
                if m.get("id") == 2:
                    break
    res = m["result"]
    d = {sid: {_hr(r["start"]): r["mean"] for r in res.get(sid, []) if r.get("mean") is not None} for sid in (CAP, ELE, OUT)}
    hrs = sorted(set(d[CAP]) & set(d[ELE]) & set(d[OUT]))

    cap_bins = ((1, 7), (7, 12), (12, 20), (20, 35), (35, 70))
    out_bins = ((-7, -2), (-2, 2), (2, 5), (5, 8), (8, 13))
    print("median electrical W  (rows = outdoor band, cols = capacity %), n in ()\n")
    print(f"{'outdoor':>10} " + " ".join(f"{f'cap{lo}-{hi}':>12}" for lo, hi in cap_bins))
    for olo, ohi in out_bins:
        cells = []
        for clo, chi in cap_bins:
            ks = [k for k in hrs if olo <= d[OUT][k] < ohi and clo <= d[CAP][k] < chi]
            cells.append(f"{np.median([abs(d[ELE][k]) for k in ks]):.0f}({len(ks)})" if ks else "-")
        print(f"{f'{olo}..{ohi}':>10} " + " ".join(f"{c:>12}" for c in cells))


asyncio.run(main())
