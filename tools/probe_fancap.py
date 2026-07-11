# Empirical fan/capacity behaviour from winter logs: per capacity step, the blower rpm/%, the air dT,
# the electrical and outdoor temp. Shows the controller's fan-vs-capacity law + the data for COP once the
# airflow is calibrated.
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
RPM = "sensor.fs_ahu_multicalor_blower_rpm"
BSP = "sensor.fs_ahu_multicalor_blower_speed"
SUP = "sensor.fs_ahu_mhi_supply_temperature"
RET = "sensor.fs_ahu_mhi_return_temperature"
ELE = "sensor.fs_evcharger_sdm220m_total_power"
OUT = "sensor.fs_ahu_mhi_outside_temperature"
IDS = [CAP, RPM, BSP, SUP, RET, ELE, OUT]


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
                                "statistic_ids": IDS, "period": "hour", "types": ["mean"]})
            while True:
                m = await ws.receive_json()
                if m.get("id") == 2:
                    break
    res = m["result"]
    d = {sid: {_hr(r["start"]): r["mean"] for r in res.get(sid, []) if r.get("mean") is not None} for sid in IDS}
    hrs = sorted(set.intersection(*[set(d[s]) for s in IDS]))

    print(f"n={len(hrs)} winter hours\n")
    print(f"{'cap%':>6} {'n':>4} {'rpm':>6} {'blow%':>6} {'supply':>7} {'return':>7} {'dT':>5} {'elec_W':>7} {'out':>5}")
    for lo, hi in ((1, 4), (4, 7), (7, 11), (11, 16), (16, 25), (25, 40), (40, 70)):
        ks = [k for k in hrs if lo <= d[CAP][k] < hi]
        if not ks:
            continue
        def med(sid, f=lambda x: x):
            return np.median([f(d[sid][k]) for k in ks])
        print(f"{med(CAP):6.1f} {len(ks):4d} {med(RPM):6.0f} {med(BSP):6.1f} {med(SUP):7.1f} {med(RET):7.1f} {med(SUP)-med(RET):5.1f} {med(ELE, abs):7.0f} {med(OUT):5.1f}")


asyncio.run(main())
