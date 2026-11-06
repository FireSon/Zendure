"""Integration: a real ZendureSolarFlow that connects over BLE and reports telemetry."""

import asyncio
import time
from types import SimpleNamespace

from conftest import fake_hass
from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.sensor import HaSensor

for cls in (HaSensor, HaNumber, HaBinarySensor):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

from custom_components.zenmaster.zendure.device import Command, ConnectionType
from custom_components.zenmaster.zendure.solarflow import ZendureSolarFlow

BLE_NAME = "ZenE1_22"
SECONDS = 30.0


def test_solarflow_reports_over_ble() -> None:
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

        assert connected, f"no BLE connection to {BLE_NAME} within {SECONDS} s"
        assert reported, f"connected to {BLE_NAME} but no telemetry within {SECONDS} s"

    asyncio.run(scenario())
