"""DeyeDriver: Deye three-phase hybrid inverters through their Solarman logger stick; every inverter is added by hand."""

from __future__ import annotations

from typing import NamedTuple

from ..config import ConfigField
from ..driver import Driver
from .inverter import DeyeInverter


class Product(NamedTuple):
    family: str  # names the device (family_<serial tail>)
    volt_scale: float  # V per raw unit of the battery voltage
    power_scale: int  # W per raw unit of the battery and PV power


# the two battery types of ha-solarman's deye_p3 profile: every register with two scales takes the first on LV, the second on HV
PRODUCTS: dict[str, Product] = {
    "LV": Product("Deye", 0.01, 1),
    "HV": Product("Deye", 0.1, 10),
}


class DeyeDriver(Driver):
    """No discovery and no shared transport: every inverter talks to its own logger stick, so the driver only builds and drops them."""

    id = "deye"  # the settings section and the subentry type of the devices
    manufacturer = "Deye"
    LOGO = "solarman"  # Deye has none of its own, its logger stick is a Solarman
    PRODUCTS = PRODUCTS
    DEVICE = DeyeInverter
    ADD = [  # the battery type picks the scales, the stick's serial is the identity, the capacity is the battery's: the inverter does not know it
        ConfigField("product", {"select": {"options": list(PRODUCTS)}}, cast=str),
        ConfigField("sn", {"text": {}}, cast=str),
        ConfigField("kwh", {"number": {"min": 0.1, "step": 0.1, "unit_of_measurement": "kWh", "mode": "box"}}, cast=float),
        ConfigField("ip", {"text": {}}, cast=str),
        ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False),
    ]
