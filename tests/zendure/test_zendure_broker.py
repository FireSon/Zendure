"""Closed-loop over a real MQTT broker: real driver + real device + simulated Zendure firmware as a second client."""

import asyncio
import json
import time
from types import SimpleNamespace

from amqtt.broker import Broker
import paho.mqtt.client as mqtt

from conftest import fake_hass
from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.distribution import Distribution
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.zendure.device import ConnectionType
from custom_components.zenmaster.zendure.solarflow import ZendureHyper
from custom_components.zenmaster.zendure.driver import ZendureDriver

for cls in (HaSensor, HaNumber, HaBinarySensor, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1884
PRODKEY = "gDa3tb"
DEVICE_ID = "9RUB1mW7"


class SimFirmware:
    """Simulated Hyper2000 firmware: own paho client, answers function/invoke and reports periodically."""

    def __init__(self) -> None:
        self.target = 0
        self.soc = 55
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "sim-firmware")
        self.client.on_connect = lambda c, u, f, rc, p: c.subscribe(f"iot/{PRODKEY}/{DEVICE_ID}/#")
        self.client.on_message = self._on_message

    def start(self) -> None:
        self.client.connect(BROKER_HOST, BROKER_PORT)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _on_message(self, client, userdata, msg) -> None:
        command = json.loads(msg.payload.decode())
        if not msg.topic.endswith("function/invoke"):
            return
        args = command["arguments"]
        self.target = args["outputPower"] - args["chargePower"]
        client.publish(f"/{PRODKEY}/{DEVICE_ID}/function/invoke/reply", json.dumps({"messageId": command["messageId"]}))

    def report(self) -> None:
        props = {"electricLevel": self.soc, "hyperTmp": 3131, "outputHomePower": max(0, self.target), "gridInputPower": max(0, -self.target)}
        self.client.publish(f"/{PRODKEY}/{DEVICE_ID}/properties/report", json.dumps({"properties": props}))


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def start_stack() -> tuple[Broker, SimpleNamespace, Distribution, ZendureDriver, SimFirmware]:
    broker = Broker(
        {
            "listeners": {"default": {"type": "tcp", "bind": f"{BROKER_HOST}:{BROKER_PORT}"}},
            "sys_interval": 0,
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
    )
    await broker.start()
    hass = fake_hass()
    hass.loop = asyncio.get_running_loop()  # the driver hops paho callbacks onto the loop and starts the device task there
    distribution = Distribution(hass, "entry")
    for e in distribution.entities.values():
        e.hass = None
    driver = ZendureDriver(hass, "entry", [], distribution)
    driver.mqtt_host = BROKER_HOST
    driver.mqtt_port = BROKER_PORT
    await driver.connect()
    firmware = SimFirmware()
    firmware.start()
    await asyncio.sleep(0.3)  # both clients connected and subscribed
    return broker, hass, distribution, driver, firmware


async def stop_stack(broker: Broker, driver: ZendureDriver, firmware: SimFirmware) -> None:
    firmware.stop()
    await driver.unload()
    await broker.shutdown()


def test_full_mqtt_loop() -> None:
    async def scenario() -> None:
        broker, hass, distribution, driver, firmware = await start_stack()
        device = None
        try:
            device = ZendureHyper(hass, None, None, "Hyper2000_D9", {"prodkey": PRODKEY, "device_id": DEVICE_ID, "sn": "AB3CD9"}, "Hyper2000", 1200, -1200)
            device.driver = driver
            for e in device.entities.values():
                e.hass = None
            driver.devices[DEVICE_ID] = device

            # first report: the driver routes it, hands the device its mqtt client and marks it LOCAL
            firmware.report()
            assert await wait_for(lambda: device.connection == ConnectionType.LOCAL and device.mqtt is not None)
            assert device.soc == 55 and device.temp == 40.0

            # power command travels over the broker; the firmware reply confirms it
            device.power(600)
            assert device.power_setpoint == 600
            assert await wait_for(lambda: device.state == DeviceState.CONFIRM)
            firmware.report()
            assert await wait_for(lambda: device.state == DeviceState.ACTIVE and device.power_actual == 600)
            assert firmware.target == 600

            # charging direction, once the hold after the last command has passed
            device.power_since = 0.0
            device.power(-400)
            assert await wait_for(lambda: firmware.target == -400)
            firmware.report()
            assert await wait_for(lambda: device.power_actual == -400)

            # power_off ends control: firmware to zero, device to EXTERNAL
            device.power_off()
            assert device.state == DeviceState.EXTERNAL
            assert await wait_for(lambda: firmware.target == 0)
        finally:
            if device is not None:
                device.unload()
            await stop_stack(broker, driver, firmware)

    asyncio.run(scenario())


def test_mqtt_discovery_creates_device() -> None:
    async def scenario() -> None:
        broker, hass, distribution, driver, firmware = await start_stack()
        device = None
        try:
            # an unknown device announces itself with its product and a serial in its log payload; the driver reports it, the coordinator would make the subentry
            found: list = []
            driver.on_discovered = lambda data: found.append(data) or driver.init_device("sub1", "Hyper2000_D9", data)
            firmware.client.publish(f"/{PRODKEY}/{DEVICE_ID}/properties/report", json.dumps({"product": "solarFlowPro", "log": {"sn": "AB3CD9"}}))
            assert await wait_for(lambda: "AB3CD9" in driver.devices)
            assert found[0]["family"] == "Hyper2000" and found[0]["sn"] == "AB3CD9"

            device = driver.devices["AB3CD9"]
            assert device in distribution.devices
            assert device.model == "solarFlowPro" and device.driver is driver and device.sn == "AB3CD9" and device.name == "Hyper2000_D9"
            assert device.power_profile[0].power == 1200 and device.power_profile[1].power == -1200
            for e in device.entities.values():
                e.hass = None

            # follow-up telemetry routes to the freshly created device
            firmware.report()
            assert await wait_for(lambda: device.connection == ConnectionType.LOCAL and device.soc == 55)
        finally:
            if device is not None:
                device.unload()
            await stop_stack(broker, driver, firmware)

    asyncio.run(scenario())
