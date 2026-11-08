"""Modbus register decoding shared by the vendors that speak Modbus TCP (Marstek, Anker)."""

from __future__ import annotations


def decode(registers: list[int], kind: str) -> int:
    # a 32-bit value comes high word first; the signed types wrap
    wide = kind in ("int32", "uint32")
    value = registers[0] << 16 | registers[1] if wide else registers[0]
    bits = 32 if wide else 16
    return value - (1 << bits) if kind.startswith("int") and value >= 1 << (bits - 1) else value


def encode(value: int, kind: str) -> list[int]:
    # the words to write: two's complement, a 32-bit value high word first
    value &= 0xFFFFFFFF if kind in ("int32", "uint32") else 0xFFFF
    return [value >> 16, value & 0xFFFF] if kind in ("int32", "uint32") else [value]


def text(registers: list[int]) -> str:
    # an ASCII string, two characters per register, high byte first, padded with zeros
    return bytes(b for word in registers for b in (word >> 8, word & 0xFF)).decode("ascii", "ignore").strip("\x00 ")
