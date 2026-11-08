"""The Deye dump script against a fake Solarman V5 logger stick that holds the inverter's registers."""

import socket
import struct
import threading
from datetime import datetime

import deye_dump
from deye_dump import CHARGES, POWERS, SOCS, TIMES, TOU, WORK_MODE, Logger, crc16, dump, program_now, tou_test

SERIAL = 2712345678


class FakeStick:
    """Answers V5 requests on a loopback port; sends a heartbeat before every answer and refuses one block."""

    def __init__(self) -> None:
        self.registers = {a: a & 0xFF for a in range(0x3000)}
        self.writes: list[tuple[int, int]] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    @staticmethod
    def frame(control: int, seq: int, payload: bytes) -> bytes:
        head = struct.pack("<BHHHI", 0xA5, len(payload), control, seq, SERIAL) + payload
        return head + bytes([sum(head[1:]) & 0xFF, 0x15])

    def serve(self) -> None:
        conn, _ = self.server.accept()
        while head := conn.recv(3):
            request = head + conn.recv(struct.unpack("<H", head[1:3])[0] + 10)
            seq = request[5]
            rtu = request[26:-2]
            assert crc16(rtu[:-2]) == struct.unpack("<H", rtu[-2:])[0]

            # function 3 reads, function 16 writes one register; the block at 0x2800 does not exist
            func, address, count = struct.unpack(">BHH", rtu[1:6])
            if func == 0x03 and address == 0x2800:
                answer = bytes([1, 0x83, 2])
            elif func == 0x03:
                answer = bytes([1, 0x03, 2 * count]) + struct.pack(f">{count}H", *(self.registers[address + i] for i in range(count)))
            else:
                value = struct.unpack(">H", rtu[7:9])[0]
                self.registers[address] = value
                self.writes.append((address, value))
                answer = bytes([1, 0x10]) + struct.pack(">HH", address, 1)
            answer += struct.pack("<H", crc16(answer))

            # a heartbeat of the stick first, then the answer: frame type, status, three times, the RTU frame
            conn.sendall(self.frame(0x4710, 0, bytes(1)) + self.frame(0x1510, seq, struct.pack("<BBIII", 2, 1, 0, 0, 0) + answer))


def _logger(stick: FakeStick) -> Logger:
    deye_dump.PAUSE_S = deye_dump.SAMPLE_S = 0
    return Logger("127.0.0.1", SERIAL, stick.port)


def test_dump_reads_every_block_and_notes_a_refused_one():
    stick = FakeStick()
    result = dump(_logger(stick))
    assert result["0x0000"] == 0 and result["0x008E"] == 0x8E and result["0x02FF"] == 0xFF and result["0x2AFF"] == 0xFF
    assert result["0x2800"] == "error: Modbus exception 2" and "0x2801" not in result


def test_program_now_wraps_past_midnight():
    times = [100, 500, 900, 1300, 1700, 2100]
    assert program_now(times, datetime(2026, 11, 8, 10, 30)) == (2, 150)
    assert program_now(times, datetime(2026, 11, 8, 0, 30)) == (5, 30)
    assert program_now(times, datetime(2026, 11, 8, 22, 0)) == (5, 180)


def test_tou_test_writes_the_program_and_puts_everything_back(monkeypatch):
    stick = FakeStick()
    times = [0, 400, 800, 1200, 1600, 2000]
    for i, t in enumerate(times):
        stick.registers[TIMES + i] = t
    before = dict(stick.registers)
    logger = _logger(stick)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(deye_dump, "program_now", lambda _times, _now: (2, 120))
    result: dict = {}
    tou_test(logger, 1000, 10, 20, 0, result, lambda: None)

    # the program is set before the two work modes, and every touched register is back as it was
    assert stick.writes[:6] == [(TOU, 0xFF), (POWERS + 2, 100), (SOCS + 2, 20), (CHARGES + 2, 0), (WORK_MODE, 2), (WORK_MODE, 0)]
    assert stick.registers == before
    assert result["tou"]["saved"] == {f"0x{a:04X}": before[a] for a in (WORK_MODE, TOU, POWERS + 2, SOCS + 2, CHARGES + 2)}


def test_tou_test_refuses_a_program_that_ends_too_soon(monkeypatch):
    stick = FakeStick()
    monkeypatch.setattr(deye_dump, "program_now", lambda _times, _now: (0, 5))
    try:
        tou_test(_logger(stick), 1000, 10, 20, 5, {}, lambda: None)
        raise AssertionError("no refusal")
    except SystemExit as stop:
        assert "ends in 5 minutes" in str(stop)
    assert stick.writes == []
