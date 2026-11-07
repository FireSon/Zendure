"""MarstekDriver: the Marstek Venus devices over Modbus TCP; every device holds its own connection, added by hand."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, NamedTuple

from ..entity import EntityDevice
from .venus import MarstekVenus

_LOGGER = logging.getLogger(__name__)


class Product(NamedTuple):
    family: str  # names the device (family_<ip tail>)
    pwr_max: int  # W both ways until the device reports its own limits (44002/44003)


# the register generations Omnibattery maps (ffunes/Omnibattery const/registers_common.py); the user picks one when adding a device
PRODUCTS: dict[str, Product] = {
    "v2": Product("VenusE", 2500),
    "v3": Product("VenusE", 2500),
    "vA": Product("VenusA", 1500),
    "vD": Product("VenusD", 2500),
}


class MarstekDriver:
    """No discovery and no shared transport: Modbus has neither, so the driver only builds and drops the devices."""

    id = "marstek"  # the settings section and the subentry type of the devices
    manufacturer = "Marstek"

    def __init__(self, hass, entry_id: str, all_devices: list[EntityDevice], distribution) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._all_devices = all_devices
        self.distribution = distribution
        self.devices: dict[str, MarstekVenus] = {}
        self.on_discovered: Callable[[dict[str, Any]], None] | None = None  # set by the coordinator like every driver; Modbus never finds anything

    async def connect(self) -> None:
        """Every device connects itself."""

    async def unload(self) -> None:
        """Every device closes its own connection in its unload."""

    def init_device(self, subentry_id: str, name: str, data: dict[str, Any]) -> EntityDevice | None:
        if not (row := PRODUCTS.get(str(data.get("product", "")))):
            _LOGGER.warning("Marstek: skipping %s: unknown generation %s", data.get("sn"), data.get("product"))
            return None
        if (old := self.devices.get(data["sn"])) is not None:
            self.remove_device(old)
        device = MarstekVenus(self.hass, self, subentry_id, name, data, row.pwr_max)
        self.distribution.devices.append(device)
        device.on_invalidate = self.distribution.on_invalidate
        self.devices[device.sn] = device
        self._all_devices.append(device)
        return device

    def remove_device(self, device: EntityDevice) -> None:
        device.unload()
        self.devices.pop(getattr(device, "sn", ""), None)
        for collection in (self._all_devices, self.distribution.devices):
            if device in collection:
                collection.remove(device)

    @classmethod
    def device_family(cls, data: dict[str, Any]) -> str | None:
        row = PRODUCTS.get(str(data.get("product", "")))
        return row.family if row else None
