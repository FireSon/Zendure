"""Marstek driver and Venus device over Modbus TCP, against a fake Modbus client that holds the battery's registers."""

import asyncio
import time
import types

import custom_components.zenmaster.marstek.venus as venus_module
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.marstek.driver import MarstekDriver
from custom_components.zenmaster.marstek.venus import CHARGE, DISCHARGE, FORCE, RS485, RS485_OFF, RS485_ON, WORK_MODE, decode
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.distribution import DistributionMode
from conftest import fake_hass
from test_subentries import _coord

for cls in (HaSensor, HaNumber, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

IP = "192.168.1.17"


def words(value: int, count: int) -> list[int]:
    # a signed value as the device holds it: two's complement, a 32-bit one high word first
    value &= (1 << 16 * count) - 1
    return [value >> 16, value & 0xFFFF] if count == 2 else [value]


class Reply:
    def __init__(self, registers: list[int] | None = None, error: bool = False) -> None:
        self.registers = registers or []
        self.error = error

    def isError(self) -> bool:
        return self.error


class FakeModbus:
    """A Venus v3 in its own work mode 1 (self-consumption), at 98 % and 100 W towards the house."""

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.down = False  # every request fails, like a device that dropped off the network
        self.writes: list[tuple[int, int]] = []
        self.regs: dict[int, int] = {37005: 98, 30006: 100, 30001: words(-100, 1)[0], 35000: 253, 32105: 5120, 44002: 2500, 44003: 2500, WORK_MODE: 1}

    def set(self, address: int, value: int, count: int = 1) -> None:
        for i, word in enumerate(words(value, count)):
            self.regs[address + i] = word

    async def connect(self) -> bool:
        self.connected = not self.down
        return self.connected

    def close(self) -> None:
        self.closed = True
        self.connected = False

    async def read_holding_registers(self, address: int, count: int = 1) -> Reply:
        if self.down:
            raise OSError("no route to host")
        return Reply([self.regs.get(address + i, 0) for i in range(count)])

    async def write_register(self, address: int, value: int) -> Reply:
        if self.down:
            raise OSError("no route to host")
        self.writes.append((address, value))
        self.regs[address] = value
        return Reply()


def start(monkeypatch, product: str = "v3", allow_control: bool = True):
    # the driver with one device on a fake client; the pauses shrunk so a test runs in tenths of a second
    monkeypatch.setattr(venus_module.MarstekVenus, "POLL_S", 0.01)
    loop = asyncio.get_running_loop()
    hass = fake_hass()
    hass.async_create_task = loop.create_task
    driver = MarstekDriver(hass, "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    d = driver.init_device("sub1", "VenusE_17", {"sn": IP, "product": product, "ip": IP, "port": 502, "allow_control": allow_control})
    d.client = FakeModbus()
    d.wait = 0.0
    for e in d.entities.values():
        if e is not None:
            e.hass = None
    return driver, d, d.client


async def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def test_decode_follows_the_register_types() -> None:
    assert decode([0xFF9C], "int16") == -100
    assert decode([0xFF9C], "uint16") == 65436
    assert decode(words(-2491, 2), "int32") == -2491
    assert decode(words(70000, 2), "int32") == 70000


def test_poll_fills_the_distribution_fields(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE and d.kwh)
        assert (d.soc, d.kwh, d.temp, d.power_actual) == (98, 5.12, 25.3, 100)
        assert (d.home_output, d.home_input, d.battery_output, d.battery_input) == (100, 0, 100, 0)
        assert (d.soc_min, d.soc_max, d.work_mode) == (12, 100, 1)
        assert d.power_profile[0].power == 2500 and d.power_profile[1].power == -2500
        assert fake.writes == [], "reading never writes"
        d.unload()
        await asyncio.sleep(0.05)
        assert fake.closed

    asyncio.run(scenario())


def test_v2_reads_its_own_registers(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch, "v2")
        fake.regs[32104] = 55
        fake.set(32202, -1800, 2)  # charging, a 32-bit register on v2
        fake.set(32102, 1850, 2)
        assert await wait_for(lambda: d.soc == 55)
        assert (d.power_actual, d.home_input, d.battery_input, d.battery_output) == (-1800, 1800, 1850, 0)
        d.unload()

    asyncio.run(scenario())


def test_power_takes_control_and_writes_the_setpoint(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)

        # discharge: control mode on, then both powers and force_mode
        d.power(800, DistributionMode.MANUAL)
        assert d.state == DeviceState.SENT
        assert await wait_for(lambda: d.state == DeviceState.CONFIRM)
        assert fake.writes == [(RS485, RS485_ON), (DISCHARGE, 800), (CHARGE, 0), (FORCE, 2)]

        # within the hold no second command; after it a charge, capped by the device limit it reports
        fake.writes.clear()
        d.power(-1000, DistributionMode.MANUAL)
        await asyncio.sleep(0.05)
        assert fake.writes == []
        fake.regs[44002] = 1200
        d._polls = 0  # the next poll reads the limits
        assert await wait_for(lambda: d.power_profile[1].power == -1200)
        d.power_since = 0.0
        d.power(-3000, DistributionMode.MANUAL)
        assert await wait_for(lambda: (FORCE, 1) in fake.writes)
        assert fake.writes == [(RS485, RS485_ON), (DISCHARGE, 0), (CHARGE, 1200), (FORCE, 1)]
        d.unload()

    asyncio.run(scenario())


def test_power_off_hands_back_the_old_work_mode(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.work_mode == 1)
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: (FORCE, 2) in fake.writes)
        fake.writes.clear()
        d.power_off()
        assert d.state == DeviceState.EXTERNAL
        assert await wait_for(lambda: (WORK_MODE, 1) in fake.writes)
        assert fake.writes == [(FORCE, 0), (CHARGE, 0), (DISCHARGE, 0), (RS485, RS485_OFF), (WORK_MODE, 1)]
        d.unload()

    asyncio.run(scenario())


def test_a_device_we_never_took_is_left_alone(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        d.power_off()
        d.unload()
        await asyncio.sleep(0.1)
        assert fake.writes == [] and fake.closed

    asyncio.run(scenario())


def test_unload_hands_back_a_device_under_control(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.work_mode == 1)
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: (FORCE, 2) in fake.writes)
        fake.writes.clear()
        d.unload()  # Home Assistant stops: the loop is gone, the hand back still goes out
        assert await wait_for(lambda: fake.closed)
        assert fake.writes == [(FORCE, 0), (CHARGE, 0), (DISCHARGE, 0), (RS485, RS485_OFF), (WORK_MODE, 1)]

    asyncio.run(scenario())


def test_silent_device_goes_offline_and_comes_back(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(venus_module, "POWER_MAX_TIME", 0.3)
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        fake.down = True
        fake.connected = False
        assert await wait_for(lambda: d.state == DeviceState.OFFLINE)
        assert d.power_actual == 0
        fake.down = False
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        d.unload()

    asyncio.run(scenario())


def test_without_allow_control_it_only_reports(monkeypatch) -> None:
    async def scenario() -> None:
        # the default: polled and reported, but out of the fleet and never written
        driver, d, fake = start(monkeypatch, allow_control=False)
        assert await wait_for(lambda: d.soc == 98)
        assert not d.allow_control and driver.distribution.devices == [] and fake.writes == []
        d.unload()

    asyncio.run(scenario())


def test_unknown_generation_is_skipped() -> None:
    driver = MarstekDriver(fake_hass(), "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    assert driver.init_device("sub1", "x", {"sn": IP, "product": "VenusC", "ip": IP}) is None
    assert MarstekDriver.device_family({"product": "vA"}) == "VenusA"


def test_coordinator_builds_and_drops_a_marstek() -> None:
    async def scenario() -> None:
        coord, entry = _coord()

        # a subentry of type marstek, as the flow files it, becomes the device on the Marstek driver, in the fleet
        coord.discovered({"sn": IP, "product": "v3", "ip": IP, "port": 502, "allow_control": True, "family": "VenusE"}, kind="marstek")
        (sub,) = [s for s in entry.subentries.values() if s.subentry_type != "energy_manager"]  # the coordinator files its own manager subentry
        assert (sub.subentry_type, sub.title, sub.unique_id) == ("marstek", "VenusE_17", f"marstek_{IP}")
        assert coord.sync_devices() is True
        d = coord.drivers["marstek"].devices[IP]
        assert d in coord.devices and d in coord.distribution.devices and (d.ip, d.port, d.model) == (IP, 502, "v3")

        # a removed subentry drops it again through its own driver
        coord.hass.async_create_task = asyncio.get_running_loop().create_task
        entry.subentries = {sid: s for sid, s in entry.subentries.items() if sid != sub.subentry_id}
        assert coord.sync_devices() is True
        assert d not in coord.devices and d not in coord.distribution.devices and not coord.drivers["marstek"].devices

    asyncio.run(scenario())
