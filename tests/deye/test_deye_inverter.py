"""Deye driver and inverter against a fake Solarman logger stick: an asyncio server that answers V5 reads from a register map."""

import asyncio
import struct
import time
import types

import custom_components.zenmaster.deye.inverter as inverter_module
from custom_components.zenmaster.deye.driver import DeyeDriver
from custom_components.zenmaster.deye.logger import crc16
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.sensor import HaSensor
from conftest import fake_hass
from test_subentries import _coord

HaSensor.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

SERIAL = 2712345678


def frame(control: int, seq: int, payload: bytes) -> bytes:
    head = struct.pack("<BHHHI", 0xA5, len(payload), control, seq, SERIAL) + payload
    return head + bytes([sum(head[1:]) & 0xFF, 0x15])


class FakeStick:
    """Answers function 3 from its register map; sends a heartbeat before every answer and records each request's function and serial."""

    def __init__(self, registers: dict[int, int]) -> None:
        self.registers = registers
        self.requests: list[tuple[int, int]] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def serve(self, reader, writer) -> None:
        try:
            while True:
                head = await reader.readexactly(3)
                request = head + await reader.readexactly(struct.unpack("<H", head[1:3])[0] + 10)
                rtu = request[26:-2]
                func, address, count = struct.unpack(">BHH", rtu[1:6])
                self.requests.append((func, struct.unpack("<I", request[7:11])[0]))
                answer = bytes([1, 0x03, 2 * count]) + struct.pack(f">{count}H", *(self.registers.get(address + i, 0) for i in range(count)))
                answer += struct.pack("<H", crc16(answer))
                writer.write(frame(0x4710, 0, bytes(1)) + frame(0x1510, request[5], struct.pack("<BBIII", 2, 1, 0, 0, 0) + answer))
                await writer.drain()
        except asyncio.IncompleteReadError:
            writer.close()


# an HV inverter: 401.2 V, 63 %, charging 1500 W (raw -150), 2000 W PV, the grid -300 / 500 / 2000 W per phase, 25.3 °C, Zero Export To CT;
# the high words say something else, so a test fails if they are ever read along
HV = {0x024A: 1253, 0x024B: 4012, 0x024C: 63, 0x024E: -150 & 0xFFFF, 0x02A0: 120, 0x02A1: 80, 0x008E: 2, 0x026E: -300 & 0xFFFF, 0x026F: 500, 0x0270: 2000}
HV |= {0x02AF + i: 1 for i in range(4)}


async def start(monkeypatch, registers: dict[int, int], product: str = "HV", port: int | None = None):
    # the fake stick on a free port, one inverter aimed at it; the pause shrunk so a test runs in tenths of a second
    monkeypatch.setattr(inverter_module, "POLL_S", 0.01)
    stick = FakeStick(registers)
    stick_port = await stick.start()
    hass = fake_hass()
    distribution = types.SimpleNamespace(devices=[], on_invalidate=lambda: None)
    driver = DeyeDriver(hass, "entry", [], distribution)
    d = driver.init_device("sub1", "Deye_5678", {"sn": str(SERIAL), "product": product, "ip": "127.0.0.1", "port": port or stick_port, "kwh": 10.24})
    for e in d.entities.values():
        if e is not None:
            e.hass = None
    return d, stick, driver, distribution


async def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def value(d, key: str):
    return d.entities[key].state


def test_poll_reads_an_hv_inverter_and_never_writes(monkeypatch) -> None:
    async def scenario() -> None:
        d, stick, _driver, distribution = await start(monkeypatch, HV)
        assert await wait_for(lambda: d.state == DeviceState.EXTERNAL)

        # the HV scales: 0.1 V and 10 W per raw unit; each phase from its signed low word, the total their sum
        assert (d.soc, d.kwh, d.temp, d.power_actual) == (63, 10.24, 25.3, -1500)
        assert (value(d, "battery_voltage"), value(d, "battery_input"), value(d, "battery_output"), value(d, "pv_power")) == (401.2, 1500, 0, 2000)
        assert [value(d, k) for k in ("phase1_power", "phase2_power", "phase3_power", "grid_power")] == [-300, 500, 2000, 2200]
        assert value(d, "work_mode") == 2

        # reads only, every one with the stick's serial; read only means out of the fleet
        assert {f for f, _ in stick.requests} == {0x03} and {s for _, s in stick.requests} == {SERIAL}
        assert distribution.devices == [] and d.subentry_data() == {"product": "HV", "sn": str(SERIAL), "ip": "127.0.0.1", "port": d.port, "kwh": 10.24}
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_poll_reads_an_lv_inverter_with_its_own_scales(monkeypatch) -> None:
    async def scenario() -> None:
        d, stick, _driver, _distribution = await start(monkeypatch, {0x024B: 5230, 0x024C: 80, 0x024E: 800, 0x02A0: 300}, product="LV")
        assert await wait_for(lambda: d.state == DeviceState.EXTERNAL)
        assert (value(d, "battery_voltage"), value(d, "battery_output"), value(d, "battery_input"), value(d, "pv_power"), d.power_actual) == (52.3, 800, 0, 300, 800)
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_an_unreachable_stick_leaves_the_inverter_offline(monkeypatch) -> None:
    async def scenario() -> None:
        # a port nobody listens on: every poll fails, the third marks the inverter offline; Windows takes about a second to refuse a connection
        d, stick, _driver, _distribution = await start(monkeypatch, HV, port=1)
        assert await wait_for(lambda: d.state == DeviceState.OFFLINE, timeout=10.0)
        assert d.power_actual == 0
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_unknown_battery_type_is_skipped() -> None:
    driver = DeyeDriver(fake_hass(), "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    assert driver.init_device("sub1", "x", {"sn": "1", "product": "MV", "ip": "h", "kwh": 5}) is None
    assert DeyeDriver.device_family({"product": "HV"}) == "Deye"


def test_coordinator_builds_a_deye_outside_the_fleet() -> None:
    async def scenario() -> None:
        coord, entry = _coord()
        coord.discovered({"sn": str(SERIAL), "product": "HV", "ip": "127.0.0.1", "port": 1, "kwh": 10.24, "family": "Deye"}, kind="deye")
        (sub,) = [s for s in entry.subentries.values() if s.subentry_type != "energy_manager"]  # the coordinator files its own manager subentry
        assert sub.subentry_type == "deye"
        assert coord.sync_devices() is True
        d = coord.drivers["deye"].devices[str(SERIAL)]
        assert d in coord.devices and d not in coord.distribution.devices and d.kwh == 10.24
        entry.subentries = {sid: s for sid, s in entry.subentries.items() if s is not sub}
        assert coord.sync_devices() is True and not coord.drivers["deye"].devices and d not in coord.devices

    asyncio.run(scenario())
