"""AnkerDriver: Anker SOLIX Solarbanks over Modbus TCP; found over mDNS or added by hand, every device holds its own connection."""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from ..config import ConfigField
from ..driver import Driver
from ..modbus import text
from .solarbank import PORT, SERIAL, AnkerSolarbank

_LOGGER = logging.getLogger(__name__)

SERVICE = "_anker_power._udp.local."  # what an Anker with Modbus TCP on advertises; its properties carry the serial


class Product(NamedTuple):
    family: str  # names the device (family_<serial tail>)
    dc_pv: bool  # the PV array sits behind the pack: 10008 is pack power and the AC side is pack plus PV (Omnibattery #366)


# the product codes Anker's own integration maps to these models (anker-charging/ha-anker-solix-official config/8fcbb87c..., 58f0132b...)
PRODUCTS: dict[str, Product] = {
    "DMWH": Product("MaxAC", False),
    "DMXU": Product("MaxAC", False),
    "E25H": Product("MaxAC", False),
    "DNMS": Product("XEAC", False),
    "DPP4": Product("XEAC", False),
    "DNN3": Product("XEAC", False),
    "DN7M": Product("E5000Pro", True),
    "DPM4": Product("E5000Pro", True),
    # Solarbank Max and XE: the same register map as the Max AC (config/482b2e95..., 3c176e10...); whether their PV sits behind the pack is not known, so counted like the Max AC until a tester confirms
    "DMY6": Product("Max", False),
    "DN9U": Product("Max", False),
    "DPTK": Product("Max", False),
    "DNMT": Product("XE", False),
    "DPP5": Product("XE", False),
    "DNN4": Product("XE", False),
    "DRE4": Product("XE", False),
    "E3DS": Product("XE", False),
}


def product_code(sn: str) -> str:
    # the code sits in the serial: characters 4-6 of a 16-digit one, 4-7 of a 17-digit one (Anker's product_mapping.py)
    return sn[3:6] if len(sn) == 16 else sn[3:7] if len(sn) == 17 else ""


class AnkerDriver(Driver):
    """Takes the Anker mDNS finds the coordinator resolves; a new one becomes a subentry, which comes back through init_device."""

    id = "anker"  # the settings section and the subentry type of the devices
    manufacturer = "Anker"
    PRODUCTS = PRODUCTS
    DEVICE = AnkerSolarbank
    ZEROCONF = [{"type": SERVICE}]  # the coordinator browses it and hands every find to zeroconf_found
    ADD = [ConfigField("ip", {"text": {}}, cast=str), ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False)]  # the serial is asked from the device

    async def zeroconf_found(self, _name: str, ip: str, properties: dict) -> None:
        # the advertisement's properties carry the serial
        self.found(str(properties.get("sn") or ""), ip)

    def found(self, sn: str, ip: str) -> None:
        # a device we know follows a new ip; a new one goes to the coordinator
        if (known := self.devices.get(sn)) is not None:
            if known.ip != ip:
                known.moved(ip)
            return
        if self.on_discovered is None or not sn:
            return
        if (row := PRODUCTS.get(code := product_code(sn))) is None:
            _LOGGER.warning("Anker: skipping %s: unsupported product code %s", sn, code)
            return
        _LOGGER.info("Found Anker device: %s (%s) at %s", sn, row.family, ip)
        self.on_discovered({"sn": sn, "product": code, "ip": ip, "port": PORT, "family": row.family})

    @classmethod
    async def probe(cls, ip: str, port: int = PORT) -> str | None:
        # the serial a device added by hand reports; None when nothing answers there
        client = AsyncModbusTcpClient(ip, port=port)
        try:
            if not await client.connect():
                return None
            result = await client.read_input_registers(SERIAL, count=12)
            return None if result.isError() else text(result.registers)
        except (ModbusException, OSError, TimeoutError) as err:
            _LOGGER.debug("Anker probe of %s:%s failed: %s", ip, port, err)
            return None
        finally:
            client.close()

    @classmethod
    async def identify(cls, hass, data: dict[str, Any]) -> dict[str, Any] | str:
        # the serial a device added by hand reports names it and picks its model; an error key when nothing answers
        if not (sn := await cls.probe(data["ip"], int(data.get("port") or PORT))):
            return "cannot_connect"
        return {"sn": sn, "product": product_code(sn)}
