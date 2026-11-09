"""Anker driver and Solarbank device over Modbus TCP, against a fake Modbus client that holds the Solarbank's registers."""

import asyncio
import time
import types

import custom_components.zenmaster.anker.solarbank as solarbank_module
from custom_components.zenmaster.anker.driver import AnkerDriver, product_code
from custom_components.zenmaster.anker.solarbank import MODE, MODE_MASK, SETPOINT, THIRD_PARTY
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.modbus import decode, encode, text
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.distribution import DistributionMode
from conftest import fake_hass
from test_subentries import _coord

for cls in (HaSensor, HaNumber, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

SN = "123DMWH4567890123"  # a 17-digit serial with product code DMWH: a Solarbank Max AC
IP = "192.168.1.30"


class Reply:
    def __init__(self, registers: list[int] | None = None) -> None:
        self.registers = registers or []

    def isError(self) -> bool:
        return False


class FakeModbus:
    """A Solarbank Max AC in self-consumption (mode 0) at 60 %, charging 800 W, limits 1200 W, soc limits 10 to 100 %."""

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.down = False
        self.writes: list[tuple[int, int | list[int]]] = []
        self.input: dict[int, int] = {}
        self.holding: dict[int, int] = {MODE: 0, 60000: 100, 60001: 10}
        for address, value, kind in ((10014, 60, "uint16"), (10008, -800, "int32"), (10002, 0, "int32"), (10156, 253, "int16"), (10250, 50, "uint32"), (10036, 1200, "int32"), (10038, 1200, "int32")):
            self.set(address, value, kind)
        self.input[MODE_MASK] = 0b1111111  # every mode offered

    def set(self, address: int, value: int, kind: str) -> None:
        for i, word in enumerate(encode(value, kind)):
            self.input[address + i] = word

    async def connect(self) -> bool:
        self.connected = not self.down
        return self.connected

    def close(self) -> None:
        self.closed = True
        self.connected = False

    async def read_input_registers(self, address: int, count: int = 1) -> Reply:
        if self.down:
            raise OSError("no route to host")
        return Reply([self.input.get(address + i, 0) for i in range(count)])

    async def read_holding_registers(self, address: int, count: int = 1) -> Reply:
        if self.down:
            raise OSError("no route to host")
        return Reply([self.holding.get(address + i, 0) for i in range(count)])

    async def write_register(self, address: int, value: int) -> Reply:
        self.writes.append((address, value))
        self.holding[address] = value
        return Reply()

    async def write_registers(self, address: int, values: list[int]) -> Reply:
        self.writes.append((address, values))
        return Reply()


def start(monkeypatch, sn: str = SN):
    # the driver with one device on a fake client; the pauses shrunk so a test runs in tenths of a second
    monkeypatch.setattr(solarbank_module, "POLL_S", 0.01)
    loop = asyncio.get_running_loop()
    hass = fake_hass()
    hass.async_create_task = loop.create_task
    driver = AnkerDriver(hass, "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    d = driver.init_device("sub1", "MaxAC_23", {"sn": sn, "product": product_code(sn), "ip": IP, "port": 502})
    d.client = FakeModbus()
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


def test_registers_decode_and_encode() -> None:
    assert decode(encode(-1200, "int32"), "int32") == -1200
    assert decode(encode(51200, "uint32"), "uint32") == 51200
    assert text([0x3132, 0x3344, 0x4D00]) == "123DM"
    assert (product_code(SN), product_code("123QNA4567890123"), product_code("short")) == ("DMWH", "QNA", "")


def test_discovery_hands_a_new_solarbank_to_the_coordinator() -> None:
    driver = AnkerDriver(fake_hass(), "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    found: list = []
    driver.on_discovered = found.append
    driver.found(SN, IP)
    driver.found("123ZZZZ4567890123", "192.168.1.31")  # a product code we do not know
    assert found == [{"sn": SN, "product": "DMWH", "ip": IP, "port": 502, "family": "MaxAC"}]


def test_poll_fills_the_distribution_fields(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE and d.kwh)
        assert (d.soc, d.kwh, d.temp, d.power_actual) == (60, 5.0, 25.3, -800)
        assert (d.home_output, d.home_input, d.battery_output, d.battery_input) == (0, 800, 0, 800)
        assert (d.soc_min, d.soc_max, d.mode) == (10, 100, 0)
        assert d.power_profile[0].power == 1200 and d.power_profile[1].power == -1200
        assert fake.writes == [], "reading never writes"
        d.unload()

    asyncio.run(scenario())


def test_e5000_counts_its_pv_on_the_ac_side(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch, "123DN7M4567890123")
        fake.set(10008, -300, "int32")  # the pack charges 300 W while the array makes 1000 W: 700 W goes out on the AC side
        fake.set(10002, 1000, "int32")
        assert await wait_for(lambda: d.power_actual == 700)
        assert (d.home_output, d.battery_input) == (700, 300)
        d.unload()

    asyncio.run(scenario())


def test_power_takes_third_party_control_and_writes_the_signed_setpoint(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE and d.mode == 0)

        # a discharge: the mode, then the setpoint as one 32-bit write, positive
        d.power(800, DistributionMode.MANUAL)
        assert await wait_for(lambda: d.state == DeviceState.CONFIRM)
        assert fake.writes == [(MODE, THIRD_PARTY), (SETPOINT, encode(800, "int32"))]

        # a charge goes negative and is capped by the limit; under 100 W becomes 0
        fake.writes.clear()
        d.power_since = 0.0
        d.power(-3000, DistributionMode.MANUAL)
        assert await wait_for(lambda: len(fake.writes) == 2)
        assert fake.writes[1] == (SETPOINT, encode(-1200, "int32"))
        fake.writes.clear()
        d.power_since = 0.0
        d.power(60, DistributionMode.MANUAL)
        assert await wait_for(lambda: len(fake.writes) == 2)
        assert fake.writes[1] == (SETPOINT, [0, 0])
        d.unload()

    asyncio.run(scenario())


def test_an_accepted_setpoint_is_not_repeated_while_the_power_lags(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE and d.mode == 0)

        # the device accepts 800 W but keeps charging: the same request past HOLD_S sends nothing
        d.power(800, DistributionMode.MANUAL)
        assert await wait_for(lambda: d.state == DeviceState.CONFIRM)
        fake.writes.clear()
        d.power_since = 0.0
        d.power(800, DistributionMode.MANUAL)
        await asyncio.sleep(0.05)
        assert fake.writes == [] and d.state == DeviceState.CONFIRM

        # after RESEND_S it goes once more, in case the device lost it
        monkeypatch.setattr(solarbank_module, "RESEND_S", 0.0)
        d.power(800, DistributionMode.MANUAL)
        assert await wait_for(lambda: fake.writes == [(MODE, THIRD_PARTY), (SETPOINT, encode(800, "int32"))])
        d.unload()

    asyncio.run(scenario())


def test_power_off_and_unload_hand_back_the_old_mode(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.mode == 0)
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: (MODE, THIRD_PARTY) in fake.writes)
        fake.writes.clear()
        d.power_off()
        assert await wait_for(lambda: (MODE, 0) in fake.writes)
        assert fake.writes == [(SETPOINT, [0, 0]), (MODE, 0)]

        # under control again, then Home Assistant stops: the hand back still goes out
        d.power_since = 0.0
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: fake.writes[-1] == (SETPOINT, encode(500, "int32")))
        fake.writes.clear()
        d.unload()
        assert await wait_for(lambda: fake.closed)
        assert fake.writes == [(SETPOINT, [0, 0]), (MODE, 0)]

    asyncio.run(scenario())


def test_a_mode_change_under_our_control_is_not_learned(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(solarbank_module, "SLOW_EVERY", 2)  # the mode on every other poll
        driver, d, fake = start(monkeypatch)
        saved: list = []
        d.on_config_changed = lambda device: saved.append(device.subentry_data()["mode"])
        fake.holding[MODE] = 7  # the user's own mode, dynamic pricing
        assert await wait_for(lambda: d.mode == 7)
        assert saved[-1] == 7, "the mode lands in the subentry"

        # under our control the device falls back to Custom (4) by itself: that is not the mode to hand back
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: (MODE, THIRD_PARTY) in fake.writes)
        fake.holding[MODE] = 4
        await asyncio.sleep(0.1)
        assert d.mode == 7
        fake.writes.clear()
        d.power_off()
        assert await wait_for(lambda: (MODE, 7) in fake.writes)
        d.unload()

    asyncio.run(scenario())


def test_after_a_crash_the_saved_mode_is_handed_back(monkeypatch) -> None:
    async def scenario() -> None:
        # HA died while in control: the device still sits in mode 3, the subentry holds mode 7
        monkeypatch.setattr(solarbank_module, "POLL_S", 0.01)
        hass = fake_hass()
        hass.async_create_task = asyncio.get_running_loop().create_task
        driver = AnkerDriver(hass, "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
        d = driver.init_device("sub1", "MaxAC_23", {"sn": SN, "product": product_code(SN), "ip": IP, "port": 502, "mode": 7})
        d.client = fake = FakeModbus()
        fake.holding[MODE] = THIRD_PARTY
        for e in d.entities.values():
            if e is not None:
                e.hass = None
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        assert d.mode == 7, "mode 3 is never learned"
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: fake.writes[-1:] == [(SETPOINT, encode(500, "int32"))])
        fake.writes.clear()
        d.power_off()
        assert await wait_for(lambda: (MODE, 7) in fake.writes)
        d.unload()

    asyncio.run(scenario())


def test_the_mode_mask_guards_taking_and_handing_back(monkeypatch) -> None:
    async def scenario() -> None:
        # a firmware without Third-Party Control: nothing is written
        driver, d, fake = start(monkeypatch)
        fake.input[MODE_MASK] = 0b1011111
        assert await wait_for(lambda: d.mode_mask == 0b1011111)
        d.power(500, DistributionMode.MANUAL)
        await asyncio.sleep(0.05)
        assert fake.writes == []

        # dynamic pricing (7) dropped out of the mask after an update: the hand back goes to self-consumption
        fake.input[MODE_MASK] = 0b0111111
        fake.holding[MODE] = 7
        assert await wait_for(lambda: d.mode_mask == 0b0111111 and d.mode == 7)
        d.power_since = 0.0
        d.power(500, DistributionMode.MANUAL)
        assert await wait_for(lambda: (MODE, THIRD_PARTY) in fake.writes)
        fake.writes.clear()
        d.power_off()
        assert await wait_for(lambda: len(fake.writes) == 2)
        assert fake.writes == [(SETPOINT, [0, 0]), (MODE, 0)]
        d.unload()

    asyncio.run(scenario())


def test_a_failed_read_closes_the_socket(monkeypatch) -> None:
    async def scenario() -> None:
        # a stale reply in the buffer: the read fails and the next round starts on a fresh connection
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        original = fake.read_input_registers

        async def stale(address: int, count: int = 1):
            fake.read_input_registers = original
            raise solarbank_module.ModbusException("request uses transaction id=7 but received 6")

        fake.read_input_registers = stale
        assert await wait_for(lambda: fake.closed)
        assert await wait_for(lambda: fake.connected), "the next poll reconnects"
        d.unload()

    asyncio.run(scenario())


def test_a_solarbank_we_never_took_is_left_alone(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        d.power_off()
        d.unload()
        await asyncio.sleep(0.1)
        assert fake.writes == [] and fake.closed

    asyncio.run(scenario())


def test_silent_device_goes_offline(monkeypatch) -> None:
    async def scenario() -> None:
        driver, d, fake = start(monkeypatch)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        fake.down = True
        fake.connected = False
        assert await wait_for(lambda: d.state == DeviceState.OFFLINE)
        assert d.power_actual == 0

        # power_off on an offline device leaves it offline, so it counts in no capacity
        d.power_off()
        assert d.state == DeviceState.OFFLINE
        d.unload()

    asyncio.run(scenario())


def test_coordinator_builds_an_anker() -> None:
    async def scenario() -> None:
        coord, entry = _coord()
        coord.discovered({"sn": SN, "product": "DMWH", "ip": IP, "port": 502, "family": "MaxAC"}, kind="anker")
        (sub,) = [s for s in entry.subentries.values() if s.subentry_type != "energy_manager"]  # the coordinator files its own manager subentry
        assert (sub.subentry_type, sub.title) == ("anker", "MaxAC_23")
        assert coord.sync_devices() is True
        d = coord.drivers["anker"].devices[SN]
        assert d in coord.distribution.devices and not d.dc_pv
        coord.hass.async_create_task = asyncio.get_running_loop().create_task
        entry.subentries = {sid: s for sid, s in entry.subentries.items() if s is not sub}
        assert coord.sync_devices() is True and not coord.drivers["anker"].devices

    asyncio.run(scenario())
