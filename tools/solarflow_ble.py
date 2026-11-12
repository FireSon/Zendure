"""Hardware check: a real ZendureSolarFlow that connects over BLE and reports telemetry; needs the device in range."""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from conftest import fake_hass  # noqa: E402  the homeassistant shim of the tests, so the integration imports without HA

from custom_components.zenmaster.binary_sensor import HaBinarySensor  # noqa: E402
from custom_components.zenmaster.number import HaNumber  # noqa: E402
from custom_components.zenmaster.sensor import HaSensor  # noqa: E402

for cls in (HaSensor, HaNumber, HaBinarySensor):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform here

from custom_components.zenmaster.zendure.device import Command, ConnectionType  # noqa: E402
from custom_components.zenmaster.zendure.solarflow import ZendureSolarFlow  # noqa: E402

BLE_NAME = "ZenE1_22"
SECONDS = 30.0


async def scenario() -> None:
    driver = SimpleNamespace(create_device=lambda: None)
    hass = fake_hass()
    device = ZendureSolarFlow(hass, None, None, "Hyper2000_D9", {"prodkey": "gDa3tb", "device_id": "9RUB1mW7", "sn": "AB3CD9"}, "Hyper 2000", 1200, -1200)
    device.driver = driver
    for e in device.entities.values():
        e.hass = None
    device.ble_name = BLE_NAME
    device.connection = ConnectionType.BLE

    # _msg_update is the last report; keep it fresh so the loop does not declare the device offline
    baseline = time.time()
    device._msg_update = baseline

    # the device loop only reaches cmd_ble on a queued command; an empty write connects and starts the reporting
    device.device_queue.put_nowait((Command.WRITE, None))

    deadline = time.time() + SECONDS
    while time.time() < deadline and device._msg_update <= baseline:
        await asyncio.sleep(2.0)

    connected = bool(device._ble_client and device._ble_client.is_connected)
    reported = device._msg_update > baseline
    device.unload()

    # the result: reporting, connected without telemetry, or no connection at all
    print(f"{BLE_NAME}: {'telemetry received' if reported else 'connected but no telemetry' if connected else 'no BLE connection'} within {SECONDS} s")


if __name__ == "__main__":
    asyncio.run(scenario())
