from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.energymaster.solver.solver import LpSolver, SolverData
from custom_components.energymaster.solver.consumers.ev import ElectricVehicle, ChargeMode
from custom_components.energymaster.const import Phase

ROOT = Path(__file__).resolve().parents[1]
SECRETS = json.loads((ROOT / "secrets.json").read_text())
HA = SECRETS["HA_URL"]
HEADERS = {"Authorization": f"Bearer {SECRETS['HA_TOKEN']}"}

SOC_ENTITY = "sensor.ev6_ev_battery_level"
LIMIT_ENTITY = "number.ev6_ac_charging_limit"
PLUG_ENTITY = "binary_sensor.ev6_ev_battery_plug"

CAPACITY_KWH = 74.0      # EV6 Long Range usable; pas aan voor jouw model
MAX_POWER_KW = 11.0      # 3-fase onboard charger
PHASE = Phase.all
DEPARTURE = "07:00"
COMPENSATION = 0.35      # eur/kWh
TARGET_SOC = 0.80        # plan-mode doel vóór vertrek


async def ha_state(session: aiohttp.ClientSession, entity: str) -> str:
    async with session.get(f"{HA}/api/states/{entity}", headers=HEADERS) as r:
        return (await r.json()).get("state", "")


async def fetch_prices(session: aiohttp.ClientSession) -> tuple[list[float], str]:
    # EpexPredictor NL 7d indicative curve; fall back to a synthetic day/night curve
    url = "https://epexpredictor.batzill.com/prices_short?country=NL&surcharge=0&taxPercent=0&evaluation=false&unit=EUR_PER_KWH&hourly=false&hours=24&timezone=UTC"
    try:
        async with session.get(url) as r:
            data = await r.json()
        candidates = list(data.values()) if isinstance(data, dict) else [data]
        for lst in candidates:
            if isinstance(lst, list) and lst and isinstance(lst[0], dict) and "t" in lst[0]:
                vals = [float(p["t"]) for p in lst]
                if len(vals) >= 96:
                    return vals[:96], "EpexPredictor"
                if len(vals) >= 24:
                    return [vals[i // 4] for i in range(96)], "EpexPredictor (uur->kwartier)"
    except Exception as err:
        print(f"  (prijs-fetch faalde: {err})")
    hour = datetime.now().hour
    return [0.10 if ((hour * 4 + i) // 4) % 24 < 7 else 0.30 for i in range(96)], "synthetisch"


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        soc = await ha_state(session, SOC_ENTITY)
        limit = await ha_state(session, LIMIT_ENTITY)
        plug = await ha_state(session, PLUG_ENTITY)
        price96, source = await fetch_prices(session)

    current_soc = float(soc) / 100.0
    ev_max_soc = float(limit) / 100.0
    connected = plug == "on"
    print(f"SoC {soc}% -> {current_soc:.2f}   limiet {limit}% -> {ev_max_soc:.2f}   plug {plug!r} -> connected={connected}")
    print(f"prijs: {source} ({min(price96):.2f}-{max(price96):.2f} eur/kWh)")

    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None)
    lp.hass = None
    lp.globals["phase_limit_kw"] = 17 / 3
    lp.price_buy = SolverData("price_buy", "eur/kWh", data_size=204)
    lp.price_buy.data[12:108] = price96
    lp.price_sell = SolverData("price_sell", "eur/kWh", data_size=204)
    lp.price_sell.data[12:108] = [p * 0.9 for p in price96]   # ruwe terugleverprijs voor de preview
    lp.solar = SolverData("solar", "kW", data_size=204)

    ev = ElectricVehicle()
    ev.id = "ev6"
    ev.capacity_kwh = CAPACITY_KWH
    ev.max_power_kw = MAX_POWER_KW
    ev.efficiency = 0.9
    ev.phase = PHASE
    ev.departure_time = DEPARTURE
    ev.charge_compensation_eur_kwh = COMPENSATION
    ev.current_soc = current_soc
    ev.ev_max_soc = ev_max_soc
    ev.target_soc = TARGET_SOC
    ev.connected = connected or "--connect" in sys.argv   # force plugged-in to preview the plan
    ev.entry_config(lp)
    if not connected and ev.connected:
        print("(auto niet aangesloten -> plan gesimuleerd alsof hij hangt)")

    now = datetime.now()
    # synthetische zonprognose: bult rond het middaguur (~6 kW piek), zodat solar-mode iets te kiezen heeft
    for i in range(96):
        hour = ((now.hour * 4 + now.minute // 15 + i) // 4) % 24
        lp.solar.data[12 + i] = max(0.0, 6.0 - abs(hour - 13)) if 7 <= hour <= 19 else 0.0
    kwh = max(0.0, (ev_max_soc - current_soc) * CAPACITY_KWH)
    print(f"te laden: {kwh:.1f} kWh   vertrek {DEPARTURE}   fase {PHASE.name}   max {MAX_POWER_KW} kW   comp {COMPENSATION} eur/kWh\n")

    for mode in (ChargeMode.off, ChargeMode.solar, ChargeMode.plan, ChargeMode.fast):
        lp.l1, lp.l2, lp.l3 = np.zeros(96), np.zeros(96), np.zeros(96)
        ev.mode = mode
        await ev.entry_prerun(lp, now)
        plan = ev.ev_load.data[12:108]
        on = [i for i in range(96) if plan[i] > 0.01]
        times = [(now + timedelta(minutes=15 * i)).strftime("%H:%M") for i in on]
        window = f"{times[0]}-{(now + timedelta(minutes=15 * (on[-1] + 1))).strftime('%H:%M')}" if on else "-"
        print(f"{mode.name:6} {len(on):2} slots  {plan.sum() * 0.25:5.1f} kWh  piek {plan.max():4.1f} kW  venster {window}")


if __name__ == "__main__":
    asyncio.run(main())
