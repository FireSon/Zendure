# List all heat-pump-related HA entities (state + unit + class) to find anything that yields a COP:
# flow/return temperature, flow rate, thermal power/energy, or a reported COP.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp

_ROOT = Path(__file__).resolve().parents[1]
_S = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL = _S["HA_URL"]
HA_TOKEN = _S["HA_TOKEN"]

KEYS = ("mhi", "ahu", "multicalor", "verventa", "evcharger", "sdm220",
        "flow", "return", "aanvoer", "retour", "water", "thermal", "cop",
        "capacity", "heat", "debiet", "ww_", "dhw", "tap")


async def main():
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{HA_URL}/api/states", headers=headers) as r:
            states = await r.json()
    rows = []
    for st in states:
        eid = st["entity_id"]
        attr = st.get("attributes", {})
        name = str(attr.get("friendly_name", "")).lower()
        hay = eid.lower() + " " + name
        if not any(k in hay for k in KEYS):
            continue
        rows.append((eid, str(st.get("state"))[:14], str(attr.get("unit_of_measurement", "")), str(attr.get("device_class", "")), attr.get("friendly_name", "")))
    rows.sort()
    print(f"{len(rows)} matching entities:\n")
    for eid, state, unit, dc, fn in rows:
        print(f"{eid:55} {state:>14} {unit:>6} {dc:14} {fn}")


asyncio.run(main())
