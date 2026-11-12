"""Logger: the Solarman V5 logger stick of a Deye on TCP 8899; a Modbus RTU frame travels inside a V5 envelope to the inverter behind it."""

from __future__ import annotations

import asyncio
import struct

PORT = 8899  # the Solarman V5 port of the logger stick
SLAVE = 1  # the inverter's Modbus address behind the stick
TIMEOUT_S = 10.0  # a stick answers slowly


def crc16(data: bytes) -> int:
    # the Modbus RTU CRC
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class Refused(OSError):
    """The inverter answered with a Modbus exception: it refuses the request, the connection is fine."""


class Logger:
    """One TCP connection to the stick, opened on first use and dropped after any failure."""

    def __init__(self, ip: str, serial: int, port: int = PORT, slave: int = SLAVE) -> None:
        self.ip = ip
        self.port = port
        self.serial = serial
        self.slave = slave
        self.seq = 0
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
        self.reader = self.writer = None

    async def read(self, start: int, count: int) -> list[int]:
        # function 3, holding registers; the answer is function code, byte count, the words big endian
        data = await self.request(struct.pack(">BHH", 0x03, start, count))
        return list(struct.unpack(f">{count}H", data[2 : 2 + 2 * count]))

    async def write(self, address: int, value: int) -> None:
        # function 16 with one register: a Deye takes its settings only this way
        await self.request(struct.pack(">BHHBH", 0x10, address, 1, 2, value & 0xFFFF))

    async def request(self, pdu: bytes) -> bytes:
        # a failed request drops the connection: a late answer left behind would be taken for the next one
        try:
            async with asyncio.timeout(TIMEOUT_S):
                if self.writer is None:
                    self.reader, self.writer = await asyncio.open_connection(self.ip, self.port)

                # the envelope: start, length, control 0x4510, sequence, stick serial, then frame type 2, sensor type and three zero times before the RTU frame
                self.seq = (self.seq + 1) & 0xFF
                rtu = bytes([self.slave]) + pdu
                payload = struct.pack("<BHIII", 0x02, 0, 0, 0, 0) + rtu + struct.pack("<H", crc16(rtu))
                frame = struct.pack("<BHHHI", 0xA5, len(payload), 0x4510, self.seq, self.serial) + payload
                self.writer.write(frame + bytes([sum(frame[1:]) & 0xFF, 0x15]))
                await self.writer.drain()

                # the stick also sends frames of its own (heartbeat) and a late answer to an earlier request: only the answer to this one counts
                while (reply := await self.receive())[3:5] != b"\x10\x15" or reply[5] != self.seq:
                    pass
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            self.close()
            raise OSError(f"logger {self.ip}: {err!r}") from err

        # a stick asked without a number tells its own in the answer; every next request carries it (the way ha-solarman learns it)
        if not self.serial:
            self.serial = struct.unpack("<I", reply[7:11])[0]

        # the RTU frame sits after the 25-byte header of an answer; slave and CRC come off, function code and data stay
        modbus = reply[25:-2]
        if len(modbus) < 5 or crc16(modbus[:-2]) != struct.unpack("<H", modbus[-2:])[0]:
            raise OSError(f"no valid Modbus answer in {reply.hex()}")
        if modbus[1] & 0x80:
            raise Refused(f"Modbus exception {modbus[2]}")
        return modbus[1:-2]

    async def receive(self) -> bytes:
        # one V5 frame: 3 bytes give the payload length, then control, sequence, serial (8), the payload, checksum and end
        head = await self.reader.readexactly(3)
        if head[0] != 0xA5:
            raise OSError(f"not a V5 frame: {head.hex()}")
        frame = head + await self.reader.readexactly(struct.unpack("<H", head[1:3])[0] + 10)
        if frame[-1] != 0x15 or sum(frame[1:-2]) & 0xFF != frame[-2]:
            raise OSError(f"bad V5 frame: {frame.hex()}")
        return frame
