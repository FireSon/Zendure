"""Droge replay: spiegelt de sim-distributie tegen 24u echte P1/Zendure-data uit productie (stuurt niets aan)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("conftest", ROOT / "tests" / "conftest.py")
conftest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conftest)

from tests.distribution.test_device_task import pump_device  # noqa: E402
from tests.distribution.test_simulation import SimHarness  # noqa: E402

P1_ENTITY = "sensor.power_actual"
ZENDURE_ENTITY = "sensor.zendure_manager_power"


def fetch_history(url: str, token: str, entities: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[float, float]]]:
    query = f"{url}/api/history/period/{urllib.parse.quote(start.isoformat())}?end_time={urllib.parse.quote(end.isoformat())}&filter_entity_id={','.join(entities)}&minimal_response&no_attributes"
    request = urllib.request.Request(query, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = json.loads(response.read())
    series: dict[str, list[tuple[float, float]]] = {}
    for entity_states in raw:
        if not entity_states:
            continue
        entity_id = entity_states[0]["entity_id"]
        points = []
        for state in entity_states:
            try:
                points.append((datetime.fromisoformat(state["last_changed"]).timestamp(), float(state["state"])))
            except (ValueError, KeyError):
                continue
        series[entity_id] = points
    return series


def resample(points: list[tuple[float, float]], start_ts: float, seconds: int) -> list[float]:
    # per seconde forward-fill vanaf de laatste bekende waarde
    values = [0.0] * seconds
    index = 0
    current = points[0][1] if points else 0.0
    for second in range(seconds):
        ts = start_ts + second
        while index < len(points) and points[index][0] <= ts:
            current = points[index][1]
            index += 1
        values[second] = current
    return values


async def main() -> None:
    secrets = json.loads((ROOT / "secrets.json").read_text())
    url, token = secrets["HA_URL"], secrets["HA_TOKEN"]
    config = json.loads((ROOT / "config" / ".storage" / "energymaster").read_text())
    soc_entities = {dev_id: dev_cfg["entities"]["soc"] for dev_id, dev_cfg in config["data"]["zendure"]["devices"].items() if dev_cfg.get("capacity_kwh")}

    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=24)
    seconds = int((end - start).total_seconds())
    print(f"historie ophalen {start.isoformat()} .. {end.isoformat()}")
    series = fetch_history(url, token, [P1_ENTITY, ZENDURE_ENTITY, *soc_entities.values()], start, end)
    for entity_id, points in series.items():
        print(f"  {entity_id}: {len(points)} punten")
    if P1_ENTITY not in series or ZENDURE_ENTITY not in series:
        print("P1 of Zendure-historie ontbreekt, stop")
        return

    p1_real = resample(series[P1_ENTITY], start.timestamp(), seconds)
    zendure_real = resample(series[ZENDURE_ENTITY], start.timestamp(), seconds)
    house = [p1_real[s] + zendure_real[s] for s in range(seconds)]  # house load: P1 stripped of the current battery control
    print(f"house: min={min(house):.0f} max={max(house):.0f} mean={sum(house) / len(house):.0f} W")

    harness = SimHarness("replay")
    for device in harness.devs:
        entity = soc_entities.get(device.device_id)
        if entity and entity in series and series[entity]:
            device._soc_pct = series[entity][0][1]
            device.update_properties({"properties": {"electricLevel": int(device._soc_pct)}}, 0.0)
    print("start-SoC: " + " ".join(f"{d.name.split('_')[0]}={d._soc_pct:.0f}%" for d in harness.devs if d.capacity_kwh))

    sim_time = 1000000.0
    real_import_wh = real_export_wh = sim_import_wh = sim_export_wh = 0.0
    switches = 0
    prev_active: set = set()
    hour_rows = []
    hour_house = hour_real = hour_sim = hour_fleet = 0.0
    for second in range(seconds):
        sim_time += 1.0
        harness._window_tick(sim_time)
        for device in harness.devs:
            device.tick(sim_time, 1.0)
        fleet = sum(device.output_power() for device in harness.devs)
        p1_sim = int(house[second] - fleet)
        await harness.dist._process_p1(p1_sim, now=sim_time)
        for device in harness.devs:
            await pump_device(device, sim_time)  # the real device task logic runs on simulated time

        real_import_wh += max(0.0, p1_real[second]) / 3600.0
        real_export_wh += max(0.0, -p1_real[second]) / 3600.0
        sim_import_wh += max(0.0, p1_sim) / 3600.0
        sim_export_wh += max(0.0, -p1_sim) / 3600.0
        current = {device.name for device in harness.devs if device.setpoint != 0}
        switches += len(current - prev_active) + len(prev_active - current)
        prev_active = current
        hour_house += house[second]
        hour_real += p1_real[second]
        hour_sim += p1_sim
        hour_fleet += fleet
        if (second + 1) % 3600 == 0:
            hour_rows.append((len(hour_rows), hour_house / 3600, hour_real / 3600, hour_sim / 3600, hour_fleet / 3600, [f"{d.soc.asNumber:.0f}" for d in harness.devs if d.capacity_kwh]))
            hour_house = hour_real = hour_sim = hour_fleet = 0.0

    print(f"\n{'uur':>4} {'huislast':>9} {'P1 echt':>9} {'P1 sim':>9} {'fleet sim':>9}  soc per device")
    for hour, avg_house, avg_real, avg_sim, avg_fleet, socs in hour_rows:
        print(f"{hour:4d} {avg_house:8.0f}W {avg_real:8.0f}W {avg_sim:8.0f}W {avg_fleet:8.0f}W  {' '.join(socs)}")
    print(f"\nnet-import: echt {real_import_wh:.0f} Wh, sim {sim_import_wh:.0f} Wh")
    print(f"net-export: echt {real_export_wh:.0f} Wh, sim {sim_export_wh:.0f} Wh")
    print(f"schakelingen sim: {switches}")


if __name__ == "__main__":
    asyncio.run(main())
