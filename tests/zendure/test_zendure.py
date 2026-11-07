"""ZendureSolarFlow unit tests: power/entity_write routing per ConnectionType, with a fake MQTT client."""

import asyncio
import json
import time

from conftest import fake_hass
import custom_components.zenmaster.zendure.solarflow as zendure_module
from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.zendure.device import Command, ConnectionType
from custom_components.zenmaster.zendure.solarflow import ZendureHyper, ZendureSolarFlow, _limits

for cls in (HaSensor, HaNumber, HaBinarySensor):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


class FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, json.loads(payload)))


class FakeResponse:
    status = 200

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body


class FakeSession:
    def __init__(self, report: dict) -> None:
        self.report = report
        self.posts: list[tuple[str, str]] = []

    async def post(self, url: str, data=None, headers=None, timeout=None) -> FakeResponse:
        self.posts.append((url, data))
        return FakeResponse("{}")

    async def get(self, url: str, headers=None, timeout=None) -> FakeResponse:
        await asyncio.sleep(0.05)  # keep SENT observable between the post and the report
        return FakeResponse(json.dumps(self.report))


def make_device(model: str = "SF2400 AC", cls: type = ZendureSolarFlow) -> ZendureSolarFlow:
    d = cls(fake_hass(), None, None, "SF2400_D9", {"prodkey": "gDa3tb", "device_id": "simdev", "sn": "AB3CD9"}, model, 2400, -2400)
    for e in d.entities.values():
        e.hass = None
    d.mqtt = FakeMqtt()
    return d


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def test_power_local_publishes_the_limits() -> None:
    async def scenario() -> None:
        d = make_device()
        d.connection = ConnectionType.LOCAL

        # a SolarFlow takes the limits as one property write, no hemsEP
        d.power(700)
        topic, cmd = d.mqtt.published[0]
        assert topic == "iot/gDa3tb/simdev/properties/write"
        assert cmd["properties"] == {"smartMode": 1, "acMode": 2, "outputLimit": 700, "inputLimit": 0}

        d.power_since = 0.0  # the hold after the last command has passed
        d.power(-500)
        _, cmd = d.mqtt.published[1]
        assert cmd["properties"] == {"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": 500}

        d.power_since = 0.0
        d.power(0)
        _, cmd = d.mqtt.published[2]
        assert cmd["properties"] == {"smartMode": 0, "acMode": 2, "outputLimit": 0, "inputLimit": 0}
        d.unload()

    asyncio.run(scenario())


def test_power_cloud_publishes_like_local() -> None:
    async def scenario() -> None:
        d = make_device()
        d.connection = ConnectionType.CLOUD
        d.power(300)
        assert len(d.mqtt.published) == 1  # one property write
        d.unload()

    asyncio.run(scenario())


def test_power_hyper_publishes_hemsep() -> None:
    # the Hyper is steered over hemsEP: RAM on the device, no flash wear like deviceAutomation
    async def scenario() -> None:
        d = make_device(model="Hyper2000", cls=ZendureHyper)
        d.connection = ConnectionType.LOCAL

        d.power(600)
        topic, cmd = d.mqtt.published[0]
        assert topic == "iot/gDa3tb/simdev/properties/write" and cmd["properties"] == {"hemsState": 1}  # raised once
        _, cmd = d.mqtt.published[1]
        assert cmd["function"] == "hemsEP" and cmd["arguments"]["outputPower"] == 600

        d.power_since = 0.0  # the hold after the last command has passed
        d.power(-400)
        _, cmd = d.mqtt.published[2]
        assert cmd["function"] == "hemsEP" and cmd["arguments"]["chargePower"] == 400  # no second hemsState write
        d.unload()

    asyncio.run(scenario())


def test_power_off_publishes_off_command() -> None:
    async def scenario() -> None:
        d = make_device()
        d.connection = ConnectionType.LOCAL
        d.power_off()
        topic, cmd = d.mqtt.published[0]
        assert topic == "iot/gDa3tb/simdev/properties/write" and cmd["properties"] == {"smartMode": 0, "acMode": 2, "outputLimit": 0, "inputLimit": 0}  # 0 W first
        topic, cmd = d.mqtt.published[1]
        assert topic == "iot/gDa3tb/simdev/properties/write" and cmd["properties"] == {"hemsState": 0}  # then the hems flag down
        d.unload()

    asyncio.run(scenario())


def test_power_returns_power_actual() -> None:
    async def scenario() -> None:
        d = make_device()
        d.connection = ConnectionType.LOCAL
        assert d.power(700) == 0  # nothing measured yet
        d.power_actual = 650
        assert d.power(700) == 650
        d.unload()

    asyncio.run(scenario())


def test_mqtt_envelope() -> None:
    async def scenario() -> None:
        d = make_device()
        d.connection = ConnectionType.LOCAL
        d.power(100)
        d.power_since = 0.0  # the hold after the last command has passed
        d.power(200)
        (_, first), (_, second) = d.mqtt.published  # two property writes
        assert first["messageId"] == 100001 and second["messageId"] == 100002
        assert first["deviceId"] == "simdev" and "timestamp" in first
        d.unload()

    asyncio.run(scenario())


def test_power_offline_does_nothing() -> None:
    async def scenario() -> None:
        d = make_device()
        d.unload()  # stop the loop so the queue stays inspectable
        d.connection = ConnectionType.OFFLINE
        assert d.power(500) == 0
        d.power_off()
        assert d.mqtt.published == [] and d.device_queue.empty()

    asyncio.run(scenario())


def test_power_zensdk_and_ble_queue_http_payloads() -> None:
    async def scenario() -> None:
        d = make_device()
        d.unload()  # stop the loop so the queue stays inspectable
        for connection in (ConnectionType.ZENSDK, ConnectionType.BLE):
            d.connection = connection
            for watts in (700, -500, 0):
                d.power_since = 0.0  # the hold after the last command has passed
                d.power(watts)
            assert d.device_queue.get_nowait() == (Command.POWER, _limits(700))
            assert d.device_queue.get_nowait() == (Command.POWER, _limits(-500))
            assert d.device_queue.get_nowait() == (Command.POWER, _limits(0))
        assert d.mqtt.published == []

    asyncio.run(scenario())


def test_entity_write_local_publishes_properties() -> None:
    async def scenario() -> None:
        d = make_device()
        d.connection = ConnectionType.LOCAL
        d.entity_write(d.entities["minSoc"], 150)
        topic, cmd = d.mqtt.published[0]
        assert topic == "iot/gDa3tb/simdev/properties/write"
        assert cmd["properties"] == {"minSoc": 150}
        d.unload()

    asyncio.run(scenario())


def test_entity_write_zensdk_queues_properties() -> None:
    async def scenario() -> None:
        d = make_device()
        d.unload()  # stop the loop so the queue stays inspectable
        d.connection = ConnectionType.ZENSDK
        d.entity_write(d.entities["minSoc"], 150)
        assert d.device_queue.get_nowait() == (Command.WRITE, {"properties": {"minSoc": 150}})

    asyncio.run(scenario())


def test_the_individual_setpoint_set_in_ha_stays_out_of_the_device() -> None:
    # EM's own setpoint: set by hand in HA it lands on the attribute, and nothing reaches the device over MQTT or zenSDK
    async def scenario() -> None:
        d = make_device()
        d.unload()  # stop the loop so the queue stays inspectable
        for connection in (ConnectionType.LOCAL, ConnectionType.ZENSDK):
            d.connection = connection
            await d.entities["individual"].async_set_native_value(-1500)
        assert d.individual == -1500
        assert d.mqtt.published == [] and d.device_queue.empty()

    asyncio.run(scenario())


def test_zensdk_full_path(monkeypatch) -> None:
    async def scenario() -> None:
        d = make_device()
        d.connection = ConnectionType.ZENSDK
        d.ip = "1.2.3.4"
        d._msg_update = time.time()
        session = FakeSession({"properties": {"outputHomePower": 700, "gridInputPower": 0, "electricLevel": 55}})
        monkeypatch.setattr(zendure_module, "async_get_clientsession", lambda hass: session)

        d.power(700)
        assert await wait_for(lambda: d.state == DeviceState.SENT)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        assert d.power_actual == 700 and d.power_setpoint == 700
        assert session.posts[0] == ("http://1.2.3.4/properties/write", json.dumps({**_limits(700), "sn": d.sn, "id": d._msg_id}))
        assert d.soc == 55
        d.unload()

    asyncio.run(scenario())
