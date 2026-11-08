"""Deye register dump and TOU test over the Solarman V5 logger stick (TCP 8899); standard library only.

Use:  python deye_dump.py <ip> <logger serial>                                   read-only dump to a JSON file
      python deye_dump.py <ip> <logger serial> --tou --scale 10 --power 1000     the dump, then the TOU test; puts back every register it wrote
"""

import argparse
import json
import socket
import struct
import time
from datetime import datetime

PORT = 8899  # the Solarman V5 port of the logger stick
SLAVE = 1  # the inverter's Modbus address behind the stick
CHUNK = 64  # registers per read
PAUSE_S = 1.0  # between two requests: a logger stick is slow
SAMPLE_S = 10.0  # between two samples in the TOU test
DUMP = ((0x0000, 0x0300), (0x2700, 0x2B00))  # the inverter, then the battery modules (the blocks of ha-solarman's deye_p3 profile)
SAMPLE = ((0x024B, 6), (0x0270, 3), (0x02AF, 4))  # battery voltage, SOC, current, power; grid power; grid power 32-bit halves
WORK_MODE, TOU, TIMES, POWERS, SOCS, CHARGES = 0x008E, 0x0092, 0x0094, 0x009A, 0x00A6, 0x00AC
ZERO_EXPORT_CT, EXPORT_FIRST, TOU_WEEK = 2, 0, 0x00FF


def crc16(data: bytes) -> int:
    # the Modbus RTU CRC
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class Logger:
    """One TCP connection to the stick; a Modbus RTU frame travels inside a V5 envelope."""

    def __init__(self, ip: str, serial: int, port: int = PORT, slave: int = SLAVE, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((ip, port), timeout=timeout)
        self.serial = serial
        self.slave = slave
        self.seq = 0

    def request(self, pdu: bytes) -> bytes:
        # the envelope: start, length, control 0x4510, sequence, stick serial, then frame type 2, sensor type and three zero times before the RTU frame
        self.seq = (self.seq + 1) & 0xFF
        rtu = bytes([self.slave]) + pdu
        payload = struct.pack("<BHIII", 0x02, 0, 0, 0, 0) + rtu + struct.pack("<H", crc16(rtu))
        frame = struct.pack("<BHHHI", 0xA5, len(payload), 0x4510, self.seq, self.serial) + payload
        self.sock.sendall(frame + bytes([sum(frame[1:]) & 0xFF, 0x15]))

        # the stick also sends frames of its own (heartbeat) and a late answer to an earlier request: only the answer to this one counts
        while (reply := self.receive())[3:5] != b"\x10\x15" or reply[5] != self.seq:
            pass

        # the RTU frame sits after the 25-byte header of an answer; slave and CRC come off, function code and data stay
        modbus = reply[25:-2]
        if len(modbus) < 5 or crc16(modbus[:-2]) != struct.unpack("<H", modbus[-2:])[0]:
            raise OSError(f"no valid Modbus answer in {reply.hex()}")
        if modbus[1] & 0x80:
            raise OSError(f"Modbus exception {modbus[2]}")
        return modbus[1:-2]

    def receive(self) -> bytes:
        # one V5 frame: 3 bytes give the payload length, then control, sequence, serial (8), the payload, checksum and end
        head = self._read(3)
        if head[0] != 0xA5:
            raise OSError(f"not a V5 frame: {head.hex()}")
        frame = head + self._read(struct.unpack("<H", head[1:3])[0] + 10)
        if frame[-1] != 0x15 or sum(frame[1:-2]) & 0xFF != frame[-2]:
            raise OSError(f"bad V5 frame: {frame.hex()}")
        return frame

    def _read(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            if not (chunk := self.sock.recv(size - len(data))):
                raise OSError("connection closed by the stick")
            data += chunk
        return data

    def read(self, start: int, count: int) -> list[int]:
        # function 3, holding registers; the answer is function code, byte count, the words big endian
        data = self.request(struct.pack(">BHH", 0x03, start, count))
        time.sleep(PAUSE_S)
        return list(struct.unpack(f">{count}H", data[2 : 2 + 2 * count]))

    def write(self, address: int, value: int) -> None:
        # function 16 with one register: a Deye takes its settings only this way
        self.request(struct.pack(">BHHBH", 0x10, address, 1, 2, value & 0xFFFF))
        time.sleep(PAUSE_S)


def dump(logger: Logger) -> dict[str, int | str]:
    # every block in chunks; a chunk the inverter refuses is noted and the rest goes on
    result: dict[str, int | str] = {}
    for first, last in DUMP:
        for start in range(first, last, CHUNK):
            try:
                result.update({f"0x{start + i:04X}": v for i, v in enumerate(logger.read(start, CHUNK))})
            except OSError as err:
                result[f"0x{start:04X}"] = f"error: {err}"
                print(f"  0x{start:04X}: {err}")
    return result


def program_now(times: list[int], now: datetime) -> tuple[int, int]:
    # a program runs from its own start to the next one's (HHMM); before the first start the last one still runs; returns the program and its minutes left
    starts = [t // 100 * 60 + t % 100 for t in times]
    minute = now.hour * 60 + now.minute
    k = max(range(6), key=lambda i: (starts[i] <= minute, starts[i]))
    return k, (starts[(k + 1) % 6] - minute) % 1440


def sample(logger: Logger, phase: str, minutes: float) -> list[dict]:
    # the battery and grid registers every SAMPLE_S, raw: the scale is decoded afterwards
    rows = []
    end = time.time() + minutes * 60
    while time.time() < end:
        row = {"time": datetime.now().isoformat(timespec="seconds"), "phase": phase}
        for start, count in SAMPLE:
            row.update({f"0x{start + i:04X}": v for i, v in enumerate(logger.read(start, count))})
        rows.append(row)
        print(f"  {row}")
        time.sleep(SAMPLE_S)
    return rows


def tou_test(logger: Logger, power: int, scale: int, soc: int, minutes: float, result: dict, save) -> None:
    # the program that runs now gets the discharge power; it has to last both phases
    times = logger.read(TIMES, 6)
    k, left = program_now(times, datetime.now())
    print(f"Program times: {[f'{t // 100:02d}:{t % 100:02d}' for t in times]}; program {k + 1} runs now, {left} minutes left")
    if left < 2 * minutes + 2:
        raise SystemExit(f"Program {k + 1} ends in {left} minutes, the test needs {2 * minutes + 2}. Try again in another program window.")

    # every register the test writes, read first so it can be put back; also in the file in case the script stops halfway
    touched = (WORK_MODE, TOU, POWERS + k, SOCS + k, CHARGES + k)
    saved = {a: logger.read(a, 1)[0] for a in touched}
    result["tou"] = {"program": k + 1, "power_w": power, "scale": scale, "soc": soc, "saved": {f"0x{a:04X}": v for a, v in saved.items()}, "samples": []}
    save()
    print(f"Saved (write these back in the app if the script is interrupted): {result['tou']['saved']}")
    if input(f"Write TOU program {k + 1} = {power} W (raw {round(power / scale)}), floor SOC {soc}%, no grid charge, then {minutes} min Zero Export To CT and {minutes} min Export First? [y/N] ").lower() != "y":
        return

    try:
        # TOU on for the whole week, the program a discharge limit without grid charge: its SOC is the floor
        logger.write(TOU, TOU_WEEK)
        logger.write(POWERS + k, round(power / scale))
        logger.write(SOCS + k, soc)
        logger.write(CHARGES + k, 0)

        # the same program under both work modes
        for mode, phase in ((ZERO_EXPORT_CT, "zero_export_to_ct"), (EXPORT_FIRST, "export_first")):
            print(f"Phase {phase}")
            logger.write(WORK_MODE, mode)
            result["tou"]["samples"] += sample(logger, phase, minutes)
            save()
    finally:
        # everything back as it was, each register on its own so one failure does not stop the rest
        for address, value in saved.items():
            try:
                logger.write(address, value)
                print(f"Restored 0x{address:04X} = {value}")
            except OSError as err:
                print(f"RESTORE FAILED 0x{address:04X} = {value}: {err}; set it back in the app")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deye register dump and TOU test over the Solarman V5 logger stick")
    parser.add_argument("ip", help="the IP address of the logger stick")
    parser.add_argument("serial", type=int, help="the serial number of the logger stick (10 digits, on its label)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--slave", type=int, default=SLAVE)
    parser.add_argument("--tou", action="store_true", help="after the dump, run the TOU test; it writes registers and puts them back")
    parser.add_argument("--scale", type=int, choices=(1, 10), help="W per raw unit of the program power, see the test instructions; required with --tou")
    parser.add_argument("--power", type=int, default=1000, help="W discharge power of the test program")
    parser.add_argument("--soc", type=int, default=20, help="%% floor SOC of the test program")
    parser.add_argument("--minutes", type=float, default=5, help="minutes per work mode")
    args = parser.parse_args()
    if args.tou and args.scale is None:
        parser.error("--tou needs --scale")

    # the result goes to one file, written again after every step
    path = f"deye_{args.serial}_{datetime.now():%Y%m%d_%H%M%S}.json"
    result: dict = {"serial": args.serial, "started": datetime.now().isoformat(timespec="seconds")}

    def save() -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)

    logger = Logger(args.ip, args.serial, args.port, args.slave)
    print("Reading registers (read only)...")
    result["dump"] = dump(logger)
    save()
    print(f"Dump written to {path}")
    if args.tou:
        tou_test(logger, args.power, args.scale, args.soc, args.minutes, result, save)
        save()
        print(f"TOU test written to {path}")


if __name__ == "__main__":
    main()
