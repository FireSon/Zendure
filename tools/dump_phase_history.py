# Read-only: pulls 28d hourly mean for the per-phase consumed/produced power sensors via the HA
# WebSocket statistics API, nets them (consumed - produced), replicates BaseLoad's weekday-weighted
# average, and prints the next 24 quarters of expected per-phase background load.
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

_ROOT = Path(__file__).resolve().parents[1]
_SECRETS = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL: str = _SECRETS["HA_URL"]
HA_TOKEN: str = _SECRETS["HA_TOKEN"]
WS_URL: str = HA_URL.replace("http", "ws", 1) + "/api/websocket"

CONSUMED = ["sensor.power_consumed_phase_1", "sensor.power_consumed_phase_2", "sensor.power_consumed_phase_3"]
PRODUCED = ["sensor.power_produced_phase_1", "sensor.power_produced_phase_2", "sensor.power_produced_phase_3"]


def _dt(start) -> datetime:
    if isinstance(start, (int, float)):
        return datetime.fromtimestamp(start / 1000)  # WS stats: unix ms -> local
    return datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone()


def bucket(rows: list[dict]) -> dict:
    s = {(wd, h): [0.0, 0] for wd in range(7) for h in range(24)}
    for r in rows:
        if r.get("mean") is None:
            continue
        t = _dt(r["start"])
        b = s[(t.weekday(), t.hour)]
        b[0] += r["mean"]
        b[1] += 1
    return s


def avg(s: dict, wd: int, h: int) -> float:
    sa = sum(s[(d, h)][0] for d in range(7))
    ca = sum(s[(d, h)][1] for d in range(7))
    sw, cw = s[(wd, h)]
    return (sa + sw) / (ca + cw) if (ca + cw) else 0.0


async def main() -> None:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=28)
    ids = CONSUMED + PRODUCED
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
            await ws.receive_json()
            await ws.send_json({
                "id": 2, "type": "recorder/statistics_during_period",
                "start_time": start.isoformat(), "end_time": end.isoformat(),
                "statistic_ids": ids, "period": "hour", "types": ["mean"],
            })
            while True:
                msg = await ws.receive_json()
                if msg.get("id") == 2:
                    break

    data = msg["result"]
    buckets = {sid: bucket(data.get(sid, [])) for sid in ids}

    now = datetime.now()
    print(f"24 kwartieren vanaf {now:%a %H:%M} - verwachte achtergrondlast per fase (kW), net = consumed - produced:\n")
    print(f"{'tijd':>6} | {'L1':>8} | {'L2':>8} | {'L3':>8}")
    print("-" * 40)
    for k in range(6):
        t = now + timedelta(hours=k)
        nets = []
        for c, p in zip(CONSUMED, PRODUCED):
            net = (avg(buckets[c], t.weekday(), t.hour) - avg(buckets[p], t.weekday(), t.hour)) / 1000.0
            nets.append(net)
        for q in range(4):
            qt = t.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=15 * q)
            print(f"{qt:%H:%M} | {nets[0]:8.3f} | {nets[1]:8.3f} | {nets[2]:8.3f}")


asyncio.run(main())
