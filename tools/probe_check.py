from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp

_ROOT = Path(__file__).resolve().parents[1]
_S = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL = _S["HA_URL"]
HA_TOKEN = _S["HA_TOKEN"]

IDS = [
    "sensor.fs_ahu_mhi_compressor_hz",
    "sensor.fs_ahu_mhi_capacity",
    "sensor.fs_evcharger_sdm220m_total_power",
    "sensor.fs_ahu_mhi_supply_temperature",
    "sensor.fs_ahu_mhi_return_temperature",
    "sensor.fs_ahu_mhi_outside_temperature",
    "climate.fs_ahu_multicalor",
]


async def main():
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    async with aiohttp.ClientSession() as s:
        for eid in IDS:
            async with s.get(f"{HA_URL}/api/states/{eid}", headers=headers) as r:
                if r.status != 200:
                    print(f"{eid}: HTTP {r.status}")
                    continue
                st = await r.json()
                a = st.get("attributes", {})
                print(f"{eid:48} = {str(st.get('state')):>8} {a.get('unit_of_measurement','')!s:>4}  dc={a.get('device_class','')}")


asyncio.run(main())
