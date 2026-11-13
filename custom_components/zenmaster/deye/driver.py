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

    @classmethod
    def product_row(cls, data):
        # a new Deye starts as LV; its first poll reads the device type and corrects it
        return cls.PRODUCTS.get(str(data.get("product") or "LV"))
    ADD = [  # the serial printed on the stick is the identity; the battery type comes from the inverter, an LV battery's capacity from its packs, an HV one's is entered here
        ConfigField("sn", {"text": {}}, cast=str),
        ConfigField("kwh", {"number": {"min": 0.1, "step": 0.1, "unit_of_measurement": "kWh", "mode": "box"}}, cast=float, required=False),
        ConfigField("ip", {"text": {}}, cast=str),
        ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False),
    ]
