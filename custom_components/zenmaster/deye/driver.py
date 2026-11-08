"""DeyeDriver: Deye three-phase hybrid inverters through their Solarman logger stick; every inverter is added by hand."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, NamedTuple

from ..config import ConfigField
from ..entity import EntityDevice
from .inverter import DeyeInverter

_LOGGER = logging.getLogger(__name__)


class Product(NamedTuple):
    family: str  # names the device (family_<serial tail>)
    volt_scale: float  # V per raw unit of the battery voltage
    power_scale: int  # W per raw unit of the battery and PV power


# the two battery types of ha-solarman's deye_p3 profile: every register with two scales takes the first on LV, the second on HV
PRODUCTS: dict[str, Product] = {
    "LV": Product("Deye", 0.01, 1),
    "HV": Product("Deye", 0.1, 10),
}


class DeyeDriver:
    """No discovery and no shared transport: every inverter talks to its own logger stick, so the driver only builds and drops them."""

    id = "deye"  # the settings section and the subentry type of the devices
    manufacturer = "Deye"
    CONFIG = [ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True)]  # the settings section of the driver: only its id
    ADD = [  # the battery type picks the scales, the stick's serial is the identity, the capacity is the battery's: the inverter does not know it
        ConfigField("product", {"select": {"options": list(PRODUCTS)}}, cast=str),
        ConfigField("sn", {"text": {}}, cast=str),
        ConfigField("kwh", {"number": {"min": 0.1, "step": 0.1, "unit_of_measurement": "kWh", "mode": "box"}}, cast=float),
        ConfigField("ip", {"text": {}}, cast=str),
        ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False),
    ]

    def __init__(self, hass, entry_id: str, all_devices: list[EntityDevice], distribution) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._all_devices = all_devices
        self.distribution = distribution
        self.devices: dict[str, DeyeInverter] = {}
        self.on_discovered: Callable[[dict[str, Any]], None] | None = None  # set by the coordinator like every driver; nothing is ever found

    async def connect(self) -> None:
        """Every inverter connects itself."""

    async def unload(self) -> None:
        """Every inverter closes its own connection in its unload."""

    def init_device(self, subentry_id: str, name: str, data: dict[str, Any]) -> EntityDevice | None:
        if not (row := PRODUCTS.get(str(data.get("product", "")))):
            _LOGGER.warning("Deye: skipping %s: unknown battery type %s", data.get("sn"), data.get("product"))
            return None
        if (old := self.devices.get(data["sn"])) is not None:
            self.remove_device(old)

        # read only until the control step: not in the fleet, or the solver would plan with a battery nothing steers
        device = DeyeInverter(self.hass, self, subentry_id, name, data, row)
        self.devices[device.sn] = device
        self._all_devices.append(device)
        return device

    def remove_device(self, device: EntityDevice) -> None:
        device.unload()
        self.devices.pop(getattr(device, "sn", ""), None)
        if device in self._all_devices:
            self._all_devices.remove(device)

    @classmethod
    def device_family(cls, data: dict[str, Any]) -> str | None:
        row = PRODUCTS.get(str(data.get("product", "")))
        return row.family if row else None
