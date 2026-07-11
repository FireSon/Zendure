# Live harness: couples the real dishwasher/dryer/washingmachine consumers to the running HA via
# its WebSocket (power state -> on_power) + REST (switch via services). Dry-run by default (logs the
# switch call instead of executing). URL/token come from secrets.json. NOT a unit test (real side-effects).
#
#   python tools/live_appliances.py                  # dry-run, listen until Ctrl-C
#   python tools/live_appliances.py --switch         # actually flips the switches
#   python tools/live_appliances.py --seconds 15     # stop after 15s (for a bounded check)
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

import aiohttp

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from custom_components.energymaster.solver.solver import SolverData, SolverEntry
from custom_components.energymaster.solver.consumers.dishwasher import Dishwasher
from custom_components.energymaster.solver.consumers.dryer import Dryer
from custom_components.energymaster.solver.consumers.washingmachine import WashingMachine

_SECRETS = json.loads((_ROOT / "secrets.json").read_text(encoding="utf-8"))
HA_URL: str = _SECRETS["HA_URL"]
HA_TOKEN: str = _SECRETS["HA_TOKEN"]
WS_URL: str = HA_URL.replace("http", "ws", 1) + "/api/websocket"

# (consumer class, power_entity, switch_entity) per appliance, by its smart-plug id suffix.
WIRING: list[tuple[type[SolverEntry], str, str]] = [
    (WashingMachine, "sensor.power_usage_28bc3", "switch.relay_state_28bc3"),
    (Dishwasher, "sensor.power_usage_28df8", "switch.relay_state_28df8"),
]


class HassShim:
    # Stands in for hass: services.async_call hits the HA REST API (or logs in dry-run).
    def __init__(self, session: aiohttp.ClientSession, do_switch: bool) -> None:
        self._session = session
        self._do_switch = do_switch
        self.services = self  # so self.hass.services.async_call(...) resolves

    def async_create_task(self, coro) -> asyncio.Task:
        return asyncio.create_task(coro)

    async def async_call(self, domain: str, service: str, data: dict, blocking: bool = False) -> None:
        entity = data.get("entity_id")
        if not self._do_switch:
            print(f"[dry-run] would call {domain}.{service} on {entity}")
            return
        url = f"{HA_URL}/api/services/{domain}/{service}"
        async with self._session.post(url, json=data, headers={"Authorization": f"Bearer {HA_TOKEN}"}) as r:
            print(f"[switch] {domain}.{service} {entity} -> HTTP {r.status}")


class LpShim:
    # Stands in for the solver: track() registers a value handler per entity (the coordinator's role);
    # data() returns a real buffer so entry_config works. No LP here.
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[float], None]]] = {}

    def data(self, id: str, uom: str, day: int, source: str = "derived") -> SolverData:
        return SolverData(id, uom, source, day * 96 + 12)

    def track(self, entity_id: str, callback: Callable[[float], None]) -> None:
        if entity_id:
            self._subs.setdefault(entity_id, []).append(callback)

    def dispatch(self, entity_id: str, state: str) -> None:
        try:
            watts = float(state)
        except (ValueError, TypeError):
            return
        for cb in self._subs.get(entity_id, []):
            cb(watts)


async def main(do_switch: bool, seconds: float) -> None:
    async with aiohttp.ClientSession() as session:
        hass = HassShim(session, do_switch)
        lp = LpShim()
        for cls, power_entity, switch_entity in WIRING:
            c = cls()
            c.hass = hass
            c.switch_entity = switch_entity
            c.power_entity = power_entity
            c.entry_config(lp)  # wires lp.track(power_entity, c.on_power)
        print("mode:", "SWITCH" if do_switch else "dry-run", "| tracking:", list(lp._subs))

        # prime once with the current state so we react to live power immediately
        headers = {"Authorization": f"Bearer {HA_TOKEN}"}
        for entity_id in list(lp._subs):
            async with session.get(f"{HA_URL}/api/states/{entity_id}", headers=headers) as r:
                if r.status == 200:
                    st = await r.json()
                    print(f"  {entity_id} = {st['state']}")
                    lp.dispatch(entity_id, st["state"])

        async with session.ws_connect(WS_URL) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
            await ws.receive_json()  # auth_ok
            await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
            await ws.receive_json()  # subscribe result
            print("connected; listening for state_changed ...")

            async def _listen() -> None:
                async for msg in ws:
                    ev = msg.json()
                    if ev.get("type") != "event":
                        continue
                    d = ev["event"]["data"]
                    ns = d.get("new_state")
                    if ns is not None and d["entity_id"] in lp._subs:
                        print(f"event: {d['entity_id']} = {ns['state']}")
                        lp.dispatch(d["entity_id"], ns["state"])

            if seconds > 0:
                try:
                    await asyncio.wait_for(_listen(), timeout=seconds)
                except asyncio.TimeoutError:
                    print(f"stopped after {seconds}s")
            else:
                await _listen()


if __name__ == "__main__":
    do_switch = "--switch" in sys.argv
    seconds = 0.0
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    asyncio.run(main(do_switch, seconds))
