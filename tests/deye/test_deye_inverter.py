"""Deye driver and inverter against a fake Solarman logger stick: an asyncio server that answers V5 reads from a register map."""

import asyncio
import struct
import time
import types

import pytest

import custom_components.zenmaster.deye.inverter as inverter_module
from custom_components.zenmaster.deye.driver import DeyeDriver
from custom_components.zenmaster.deye.inverter import CHARGE_A, DISCHARGE_A, GRID_CHARGE, GRID_CHARGE_A, POWERS, RATED, SELL_POWER, SOCS, SOURCES, TOU, WORK_MODE
from custom_components.zenmaster.deye.logger import ModbusTcp, crc16
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.distribution import DistributionMode
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
        self.refused: set[int] = set()  # registers it answers with a Modbus exception

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

                # a refused register gets an exception; function 16 stores its one register and echoes address and count; function 3 answers from the map
                if address in self.refused:
                    answer = bytes([1, func | 0x80, 2])
                elif func == 0x10:
                    self.registers[address] = struct.unpack(">H", rtu[7:9])[0]
                    answer = bytes([1, 0x10]) + struct.pack(">HH", address, count)
                else:
                    answer = bytes([1, 0x03, 2 * count]) + struct.pack(f">{count}H", *(self.registers.get(address + i, 0) for i in range(count)))
                answer += struct.pack("<H", crc16(answer))
                writer.write(frame(0x4710, 0, bytes(1)) + frame(0x1510, request[5], struct.pack("<BBIII", 2, 1, 0, 0, 0) + answer))
                await writer.drain()
        except asyncio.IncompleteReadError:
            writer.close()


# an HV inverter: 401.2 V, 63 %, charging 1500 W (raw -150), 2000 W PV, the grid -300 / 500 / 2000 W per phase, 25.3 °C, Zero Export To CT;
# the high words say something else, so a test fails if they are ever read along
HV = {0x0000: 0x0006, 0x024A: 1253, 0x024B: 4012, 0x024C: 63, 0x024E: -150 & 0xFFFF, 0x02A0: 120, 0x02A1: 80, 0x008E: 2, 0x026E: -300 & 0xFFFF, 0x026F: 500, 0x0270: 2000}
HV |= {0x02AF + i: 1 for i in range(4)}


async def start(monkeypatch, registers: dict[int, int], product: str = "HV", port: int | None = None):
    # the fake stick on a free port, one inverter aimed at it; the pause shrunk so a test runs in tenths of a second
    monkeypatch.setattr(inverter_module, "POLL_S", 0.01)
    stick = FakeStick(registers)
    stick_port = await stick.start()
    hass = fake_hass()
    hass.async_create_task = asyncio.get_running_loop().create_task  # unload hands back on a task of its own
    distribution = types.SimpleNamespace(devices=[], on_invalidate=lambda: None)
    driver = DeyeDriver(hass, "entry", [], distribution)
    d = driver.init_device("sub1", "Deye_5678", {"sn": str(SERIAL), "product": product, "ip": "127.0.0.1", "port": port or stick_port, "kwh": 10.24, "allow_control": True})
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
        assert await wait_for(lambda: d.state == DeviceState.CONTROLLER)

        # the HV scales: 0.1 V and 10 W per raw unit; each phase from its signed low word, the total their sum
        assert (d.soc, d.kwh, d.temp, d.power_actual) == (63, 10.24, 25.3, -1500)
        assert (value(d, "battery_voltage"), value(d, "battery_input"), value(d, "battery_output"), value(d, "pv_power")) == (401.2, 1500, 0, 2000)
        assert [value(d.meter, key) for key in ("phase1_power", "phase2_power", "phase3_power", "grid_power")] == [-300, 500, 2000, 2200]
        assert value(d, "work_mode") == 2

        # reads only, every one with the stick's serial; in the fleet as a controller, which counts in no plan or regulation
        assert {f for f, _ in stick.requests} == {0x03} and {s for _, s in stick.requests} == {SERIAL}
        assert distribution.devices == [d] and d.subentry_data() == {"product": "HV", "sn": str(SERIAL), "ip": "127.0.0.1", "port": d.port, "kwh": 10.24, "saved": {}, "allow_control": True}
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_poll_reads_an_lv_inverter_with_its_own_scales(monkeypatch) -> None:
    async def scenario() -> None:
        d, stick, _driver, _distribution = await start(monkeypatch, {0x0000: 0x0005, 0x024B: 5230, 0x024C: 80, 0x024E: 800, 0x02A0: 300}, product="LV")
        assert await wait_for(lambda: d.state == DeviceState.CONTROLLER)
        assert (value(d, "battery_voltage"), value(d, "battery_output"), value(d, "battery_input"), value(d, "pv_power"), d.power_actual) == (52.3, 800, 0, 300, 800)
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_lv_packs_become_devices_and_give_the_kwh(monkeypatch) -> None:
    async def scenario() -> None:
        # two packs of 100 Ah behind the BMS, the energy counters in 0.1 kWh (totals low word first), the grid port at 230.1 V and 50.02 Hz
        packs = {0x2712: 2}
        for n, sn in enumerate(("PACK0001", "PACK0002")):
            base = 0x2730 + n * 0x26
            packs |= {base + i: ord(sn[2 * i]) << 8 | ord(sn[2 * i + 1]) for i in range(4)} | {base + 0x08: 530, base + 0x09: -58 & 0xFFFF, base + 0x0A: 1135, base + 0x0F: 978, base + 0x12: 1000, base + 0x16: 3315, base + 0x18: 504}
        energy = {0x0204: 46000, 0x0205: 1, 0x0206: 41000, 0x0208: 355, 0x0209: 58, 0x020A: 12377, 0x020C: 3985, 0x0211: 54, 0x0216: 4270, 0x0256: 2301, 0x0261: 5002}
        d, stick, _driver, _distribution = await start(monkeypatch, {0x0000: 0x0005, 0x024B: 5230} | packs | energy, product="")
        assert await wait_for(lambda: len(d.packs) == 2 and d.kwh == 10.24)
        pack = d.packs["PACK0001"]
        assert (value(pack, "voltage"), value(pack, "current"), value(pack, "temp"), value(pack, "soc"), value(pack, "capacity"), value(pack, "cell_max"), value(pack, "cycles")) == (53.0, -5.8, 13.5, 97.8, 100.0, 3.315, 504)
        assert [value(d.meter, k) for k in ("import_today", "export_today", "import_total", "export_total", "voltage_l1", "frequency")] == [35.5, 5.8, 1237.7, 398.5, 230.1, 50.02]
        assert (value(d, "pv_today"), value(d, "pv_total"), d.model) == (5.4, 427.0, "LV")
        assert (value(d, "battery_charge_total"), value(d, "battery_discharge_total")) == (11153.6, 4100.0)  # 65536 + 46000 tenths, low word first
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_an_hv_added_as_lv_takes_the_hv_scales(monkeypatch) -> None:
    async def scenario() -> None:
        # the device type says HV: the model and the scales follow at the first poll
        d, stick, _driver, _distribution = await start(monkeypatch, HV, product="LV")
        assert await wait_for(lambda: d.state == DeviceState.CONTROLLER)
        assert (d.model, value(d, "battery_voltage")) == ("HV", 401.2)
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_another_deye_family_is_left_alone(monkeypatch) -> None:
    async def scenario() -> None:
        # a single-phase hybrid (type 3) has other registers: one notification, out of the fleet, never written to, only the type read
        notes = []
        monkeypatch.setattr(inverter_module.persistent_notification, "async_create", lambda hass, message, *a: notes.append(message))
        d, stick, _driver, _distribution = await start(monkeypatch, HV | {0x0000: 0x0003})
        assert await wait_for(lambda: d.state == DeviceState.EXTERNAL and notes)
        d.power(1500, DistributionMode.INDIVIDUAL)
        await asyncio.sleep(0.2)
        assert {f for f, _ in stick.requests} == {0x03} and len(notes) == 1 and "0x0003" in notes[0]
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


def test_smart_charging_closes_the_discharge_and_hands_back(monkeypatch) -> None:
    async def scenario() -> None:
        # its own settings: 100 A charge, 120 A discharge, Zero Export To Load
        d, stick, _driver, _distribution = await start(monkeypatch, HV | {CHARGE_A: 100, DISCHARGE_A: 120, WORK_MODE: 1})
        assert await wait_for(lambda: d.state == DeviceState.CONTROLLER)

        # SMART_CHARGING: Zero Export To CT, the discharge current closed, the charge current its own; it regulates itself on our settings
        d.power(0, DistributionMode.SMART_CHARGING)
        assert await wait_for(lambda: d.state == DeviceState.EXTERNAL)
        assert (stick.registers[WORK_MODE], stick.registers[DISCHARGE_A], stick.registers[CHARGE_A], stick.registers[TOU]) == (2, 0, 100, 0)

        # the next slow poll reads the closed discharge current, the profile keeps its own
        polls = d._polls
        assert await wait_for(lambda: d._polls > polls + inverter_module.SLOW_EVERY)
        assert d.power_profile[0].power == 48100

        # hand back: its own settings again, and it regulates itself
        d.power_off()
        assert await wait_for(lambda: not d.saved)
        assert (stick.registers[WORK_MODE], stick.registers[DISCHARGE_A], d.state) == (1, 120, DeviceState.CONTROLLER)
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_individual_goes_through_every_tou_program(monkeypatch) -> None:
    async def scenario() -> None:
        # newer firmware: the Sell bit in every charge source, max sell power 1000 W, rated 12 kW (120000 in 0.1 W); programs 4-6 refuse a write over Modbus
        d, stick, _driver, _distribution = await start(monkeypatch, HV | {CHARGE_A: 100, DISCHARGE_A: 120, SELL_POWER: 100, RATED: 54464, RATED + 1: 1} | dict.fromkeys(SOURCES, 0x20))
        stick.refused = set(POWERS[3:])
        assert await wait_for(lambda: d.state == DeviceState.CONTROLLER and d.battery_voltage)

        # its power profile: 120 A and 100 A at 401.2 V would give 48100 and 40100 W, its rated 12 kW caps both
        assert (d.power_profile[0].power, d.power_profile[1].power) == (12000, -12000)

        # discharge 1500 W: programs 1-3 150 at the HV scale of 10 (4-6 refused, the rest still goes), the SOC floor, the Sell bit kept, max sell power up to the setpoint
        d.power(1500, DistributionMode.INDIVIDUAL)
        assert await wait_for(lambda: d.state == DeviceState.ACTIVE)
        assert [stick.registers.get(a, 0) for a in POWERS] == [150] * 3 + [0] * 3 and [stick.registers[a] for a in SOCS] == [10] * 6 and [stick.registers[a] for a in SOURCES] == [0x20] * 6
        assert (stick.registers[WORK_MODE], stick.registers[TOU], stick.registers[GRID_CHARGE], stick.registers[SELL_POWER]) == (0, 0x00FF, 0, 150)

        # charge 2000 W from the grid: 2000 W over 401.2 V is 5 A, the SOC target, the grid bit next to the Sell bit
        d.power(-2000, DistributionMode.INDIVIDUAL)
        assert await wait_for(lambda: d.sent == (-2000, DistributionMode.INDIVIDUAL)) and stick.registers[GRID_CHARGE_A] == 5
        assert stick.registers[GRID_CHARGE] == 1 and [stick.registers[a] for a in SOCS] == [90] * 6 and [stick.registers[a] for a in SOURCES] == [0x21] * 6
        d.power_off()
        d.unload()
        stick.server.close()

    asyncio.run(scenario())


def test_a_serial_with_letters_learns_the_number_from_the_stick(monkeypatch) -> None:
    # the printed serial has letters: the first request goes out without a number, the answer carries the stick's own, and the next requests use it
    async def scenario() -> None:
        monkeypatch.setattr(inverter_module, "POLL_S", 0.01)
        stick = FakeStick(dict(HV))
        port = await stick.start()
        hass = fake_hass()
        hass.async_create_task = asyncio.get_running_loop().create_task  # unload hands back on a task of its own
        driver = DeyeDriver(hass, "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
        d = driver.init_device("sub1", "Deye_D0", {"sn": "D263299220D0", "product": "HV", "ip": "127.0.0.1", "port": port, "kwh": 10.24})
        assert await wait_for(lambda: len(stick.requests) >= 3)
        assert stick.requests[0][1] == 0 and stick.requests[-1][1] == SERIAL and d.logger.serial == SERIAL and d.sn == "D263299220D0"
        d.unload()

    asyncio.run(scenario())


def test_unknown_battery_type_is_skipped() -> None:
    driver = DeyeDriver(fake_hass(), "entry", [], types.SimpleNamespace(devices=[], on_invalidate=lambda: None))
    assert driver.init_device("sub1", "x", {"sn": "1", "product": "MV", "ip": "h", "kwh": 5}) is None
    assert DeyeDriver.device_family({"product": "HV"}) == "Deye"


def test_coordinator_builds_a_deye_in_the_fleet() -> None:
    async def scenario() -> None:
        coord, entry = _coord()
        coord.discovered({"sn": str(SERIAL), "product": "HV", "ip": "127.0.0.1", "port": 1, "kwh": 10.24, "allow_control": True, "family": "Deye"}, kind="deye")
        (sub,) = [s for s in entry.subentries.values() if s.subentry_type != "energy_manager"]  # the coordinator files its own manager subentry
        assert sub.subentry_type == "deye"
        assert coord.sync_devices() is True
        d = coord.drivers["deye"].devices[str(SERIAL)]
        assert d in coord.devices and d in coord.distribution.devices and d.kwh == 10.24
        coord.hass.async_create_task = asyncio.get_running_loop().create_task  # unload hands back on a task of its own
        entry.subentries = {sid: s for sid, s in entry.subentries.items() if sid != sub.subentry_id}
        assert coord.sync_devices() is True and not coord.drivers["deye"].devices and d not in coord.devices

    asyncio.run(scenario())


def test_modbus_tcp_reads_and_an_unreachable_stick_is_an_oserror() -> None:
    # a stick with plain Modbus TCP: an MBAP header, unit 1, function 3; nobody on the port gives an OSError like the V5 path
    async def scenario() -> None:
        async def handle(reader, writer):
            tid, _pid, _length, unit, _fc, address, count = struct.unpack(">HHHBBHH", await reader.readexactly(12))
            data = struct.pack(f">{count}H", *(0x0005 + address + i for i in range(count)))
            writer.write(struct.pack(">HHHBBB", tid, 0, 3 + len(data), unit, 3, len(data)) + data)
            await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        assert await ModbusTcp("127.0.0.1", port).read(0x0000, 2) == [0x0005, 0x0006]
        server.close()
        with pytest.raises(OSError):
            await ModbusTcp("127.0.0.1", 1).read(0x0000, 1)

    asyncio.run(scenario())
