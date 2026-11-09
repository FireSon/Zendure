"""MarstekDriver: the Marstek Venus devices over Modbus TCP; every device holds its own connection, added by hand."""

from __future__ import annotations

from typing import NamedTuple

from ..config import ConfigField
from ..driver import Driver
from .venus import MarstekVenus


class Product(NamedTuple):
    family: str  # names the device (family_<ip tail>)
    pwr_max: int  # W both ways until the device reports its own limits (44002/44003)


# the register generations of ViperRNMC/marstek_venus_modbus (registers/e_v12, e_v3, a, d.yaml); the user picks one when adding a device
PRODUCTS: dict[str, Product] = {
    "v2": Product("VenusE", 2500),
    "v3": Product("VenusE", 2500),
    "vA": Product("VenusA", 1500),
    "vD": Product("VenusD", 2500),
}


class MarstekDriver(Driver):
    """No discovery and no shared transport: Modbus has neither, so the driver only builds and drops the devices."""

    id = "marstek"  # the settings section and the subentry type of the devices
    manufacturer = "Marstek"
    LOGO = "marstek"
    PRODUCTS = PRODUCTS
    DEVICE = MarstekVenus
    ADD = [ConfigField("product", {"select": {"options": list(PRODUCTS)}}, cast=str), ConfigField("ip", {"text": {}}, cast=str), ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False)]  # the register generation
    KEY = "ip"  # Modbus reports no identity: the ip is the serial
