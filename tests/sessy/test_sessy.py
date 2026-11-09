"""Sessy driver and battery against a fake dongle: a small aiohttp server with the local API's endpoints and basic auth."""

import asyncio
import base64
import time
import types

from aiohttp import web

import custom_components.zenmaster.sessy.battery as battery_module
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.sessy.driver import SessyDriver
from custom_components.zenmaster.distribution import DistributionMode
from conftest import fake_hass
from test_subentries import _coord

for cls in (HaSensor, HaNumber, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

USER, PASSWORD = "ABCD1234", "secret"


class FakeDongle:
    """A Sessy in NOM at 55 %, charging 1000 W; it answers only with the right credentials."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.strategy = "POWER_STRATEGY_NOM"
        self.strategy_errors = 0  # this many strategy reads answer 500, like a dongle that cannot reach its AC board
        self.sessy = {"state_of_charge": 0.55, "power": -1000, "power_setpoint": 0, "system_state": "SYSTEM_STATE_RUNNING_SAFE", "strategy_overridden": True}
        self.app = web.Application(middlewares=[self.auth])
        self.app.router.add_get("/api/v1/power/status", self.status)
        self.app.router.add_get("/api/v1/power/active_strategy", self.get_strategy)
        self.app.router.add_post("/api/v1/power/active_strategy", self.post)
        self.app.router.add_post("/api/v1/power/setpoint", self.post)

    async def status(self, _request) -> web.Response:
        return web.json_response({"status": "ok", "sessy": self.sessy})

    async def get_strategy(self, _request) -> web.Response:
        if self.strategy_errors:
            self.strategy_errors -= 1
            return web.Response(status=500)
        return web.json_response({"status": "ok", "strategy": self.strategy})

    @web.middleware
    async def auth(self, request, handler):
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return web.Response(status=401)
        return await handler(request)

    async def post(self, request) -> web.Response:
        body = await request.json()
        self.posts.append((request.path.rsplit("/", 1)[-1], body))
        self.strategy = body.get("strategy", self.strategy)
        return web.json_response({"status": "ok"})


async def start(monkeypatch, password: str = PASSWORD, strategy: str | None = None, dongle: FakeDongle | None = None):
    # the fake dongle on a free port, one battery aimed at it; the pauses shrunk so a test runs in tenths of a second
    monkeypatch.setattr(battery_module.SessyBattery, "POLL_S", 0.01)
    dongle = dongle or FakeDongle()
    runner = web.AppRunner(dongle.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    loop = asyncio.get_running_loop()
    hass = fake_hass()
    hass.async_create_task = loop.create_task
    driver = SessyDriver(hass, "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    d = driver.init_device("sub1", "Sessy_01", {"sn": host, "product": "5kWh", "ip": host, "username": USER, "password": password, "strategy": strategy})
    for e in d.entities.values():
        if e is not None:
            e.hass = None
    return d, dongle, runner


async def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def test_poll_fills_the_distribution_fields(monkeypatch) -> None:
    async def scenario() -> None:
        d, dongle, runner = await start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE and d.strategy)
        assert (d.soc, d.kwh, d.power_actual, d.strategy) == (55.0, 5.2, -1000, "POWER_STRATEGY_NOM")
        assert (d.home_input, d.home_output, d.battery_input, d.soc_limit) == (1000, 0, 1000, 0)
        assert d.power_profile[0].power == 1700 and d.power_profile[1].power == -2200
        assert d.entities["strategy_overridden"].state is True, "shown only"
        dongle.sessy["system_state"] = "SYSTEM_STATE_BATTERY_FULL"
        assert await wait_for(lambda: d.soc_limit == 1)
        assert dongle.posts == [], "reading never writes"
        d.unload()
        await asyncio.sleep(0.05)
        await runner.cleanup()

    asyncio.run(scenario())


def test_power_takes_the_api_strategy_and_off_hands_it_back(monkeypatch) -> None:
    async def scenario() -> None:
        d, dongle, runner = await start(monkeypatch)
        assert await wait_for(lambda: d.strategy == "POWER_STRATEGY_NOM")

        # a discharge: the API strategy, then the setpoint, positive towards the house
        d.power(700, DistributionMode.MANUAL)
        assert await wait_for(lambda: d.state == DeviceState.CONFIRM)
        assert dongle.posts == [("active_strategy", {"strategy": "POWER_STRATEGY_API"}), ("setpoint", {"setpoint": 700})]

        # off: setpoint 0 and the strategy it had
        dongle.posts.clear()
        d.power_off()
        assert await wait_for(lambda: len(dongle.posts) == 2)
        assert dongle.posts == [("setpoint", {"setpoint": 0}), ("active_strategy", {"strategy": "POWER_STRATEGY_NOM"})]

        # under control again, then Home Assistant stops: the hand back still goes out
        d.power_since = 0.0
        d.power(-1500, DistributionMode.MANUAL)
        assert await wait_for(lambda: ("setpoint", {"setpoint": -1500}) in dongle.posts)
        dongle.posts.clear()
        d.unload()
        assert await wait_for(lambda: len(dongle.posts) == 2)
        assert dongle.posts == [("setpoint", {"setpoint": 0}), ("active_strategy", {"strategy": "POWER_STRATEGY_NOM"})]
        await runner.cleanup()

    asyncio.run(scenario())


def test_a_missed_strategy_is_read_before_taking_control_and_persisted(monkeypatch) -> None:
    async def scenario() -> None:
        # the first poll's strategy read fails, so the battery is taken without a known strategy
        dongle = FakeDongle()
        dongle.strategy_errors = 1
        d, dongle, runner = await start(monkeypatch, dongle=dongle)
        saved: list = []
        d.on_config_changed = lambda device: saved.append(device.subentry_data()["strategy"])
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        assert d.strategy is None

        # power reads it first, persists it, and the hand back has it
        d.power(700, DistributionMode.MANUAL)
        assert await wait_for(lambda: ("setpoint", {"setpoint": 700}) in dongle.posts)
        assert saved == ["POWER_STRATEGY_NOM"]
        dongle.posts.clear()
        d.power_off()
        assert await wait_for(lambda: len(dongle.posts) == 2)
        assert dongle.posts[1] == ("active_strategy", {"strategy": "POWER_STRATEGY_NOM"})
        d.unload()
        await asyncio.sleep(0.05)
        await runner.cleanup()

    asyncio.run(scenario())


def test_after_a_crash_the_saved_strategy_is_handed_back(monkeypatch) -> None:
    async def scenario() -> None:
        # HA died while in control: the dongle still sits in API, the subentry holds ROI
        dongle = FakeDongle()
        dongle.strategy = "POWER_STRATEGY_API"
        d, dongle, runner = await start(monkeypatch, strategy="POWER_STRATEGY_ROI", dongle=dongle)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        assert d.strategy == "POWER_STRATEGY_ROI", "API is never learned"
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: ("setpoint", {"setpoint": 500}) in dongle.posts)
        dongle.posts.clear()
        d.power_off()
        assert await wait_for(lambda: len(dongle.posts) == 2)
        assert dongle.posts[1] == ("active_strategy", {"strategy": "POWER_STRATEGY_ROI"})
        d.unload()
        await asyncio.sleep(0.05)
        await runner.cleanup()

    asyncio.run(scenario())


def test_a_battery_that_cannot_follow_leaves_the_fleet(monkeypatch) -> None:
    async def scenario() -> None:
        d, dongle, runner = await start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)

        # a fault: out of the fleet, and a request sends nothing
        dongle.sessy["system_state"] = "SYSTEM_STATE_ERROR"
        assert await wait_for(lambda: d.state == DeviceState.EXTERNAL)
        d.power(700, DistributionMode.MANUAL)
        await asyncio.sleep(0.05)
        assert dongle.posts == [] and d.state == DeviceState.EXTERNAL

        # running again and at its setpoint: back in the fleet
        dongle.sessy |= {"system_state": "SYSTEM_STATE_RUNNING_SAFE", "power": 0}
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        d.unload()
        await asyncio.sleep(0.05)
        await runner.cleanup()

    asyncio.run(scenario())


def test_a_battery_we_never_took_is_left_alone(monkeypatch) -> None:
    async def scenario() -> None:
        d, dongle, runner = await start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        d.power_off()
        d.unload()
        await asyncio.sleep(0.1)
        assert dongle.posts == []
        await runner.cleanup()

    asyncio.run(scenario())


def test_wrong_credentials_leave_the_battery_offline(monkeypatch) -> None:
    async def scenario() -> None:
        d, dongle, runner = await start(monkeypatch, password="wrong")
        assert await wait_for(lambda: d.state == DeviceState.OFFLINE)
        assert d.power_actual == 0
        d.unload()
        await asyncio.sleep(0.05)
        await runner.cleanup()

    asyncio.run(scenario())


def test_probe_tells_good_from_wrong_credentials(monkeypatch) -> None:
    async def scenario() -> None:
        d, dongle, runner = await start(monkeypatch)
        assert await SessyDriver.probe(d.ip, USER, PASSWORD) is None
        assert await SessyDriver.probe(d.ip, USER, "wrong") == "invalid_auth"
        d.unload()
        await asyncio.sleep(0.05)
        await runner.cleanup()
        assert await SessyDriver.probe(d.ip, USER, PASSWORD) == "cannot_connect", "nothing listens there any more"

    asyncio.run(scenario())


def test_unknown_model_is_skipped() -> None:
    driver = SessyDriver(fake_hass(), "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    assert driver.init_device("sub1", "x", {"sn": "h", "product": "Plus", "ip": "h"}) is None
    assert SessyDriver.device_family({"product": "10kWh"}) == "Sessy10"


def test_coordinator_builds_a_sessy() -> None:
    async def scenario() -> None:
        coord, entry = _coord()
        coord.discovered({"sn": "sessy-ab12.local", "product": "10kWh", "ip": "sessy-ab12.local", "username": USER, "password": PASSWORD, "family": "Sessy10"}, kind="sessy")
        (sub,) = [s for s in entry.subentries.values() if s.subentry_type != "energy_manager"]  # the coordinator files its own manager subentry
        assert sub.subentry_type == "sessy"
        assert coord.sync_devices() is True
        d = coord.drivers["sessy"].devices["sessy-ab12.local"]
        assert d in coord.distribution.devices and d.kwh == 10.0
        coord.hass.async_create_task = asyncio.get_running_loop().create_task
        entry.subentries = {sid: s for sid, s in entry.subentries.items() if sid != sub.subentry_id}
        assert coord.sync_devices() is True and not coord.drivers["sessy"].devices
        await asyncio.sleep(0.05)  # the session closes on its way out

    asyncio.run(scenario())
