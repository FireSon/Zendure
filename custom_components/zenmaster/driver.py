"""Driver: what every brand's driver shares; the brand adds its products, its device class and its own discovery."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .config import ConfigField
from .device import DistributionDevice
from .entity import EntityDevice

if TYPE_CHECKING:
    from .distribution import Distribution

_LOGGER = logging.getLogger(__name__)


class Driver:
    id: str  # the settings section and the subentry type of the devices
    manufacturer: str
    LOGO = ""  # its icon's domain on Home Assistant's brands CDN; empty keeps the battery icon
    PRODUCTS: dict[str, Any]  # product key -> the brand's Product row; its family names the device
    DEVICE: type  # the device class the brand builds, called with the product row
    CONFIG = [ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True)]  # the settings section of the driver: only its id

    def __init__(self, hass: HomeAssistant, entry_id: str, all_devices: list[EntityDevice], distribution: Distribution) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._all_devices = all_devices
        self.distribution = distribution
        self.devices: dict[str, Any] = {}
        self.on_discovered: Callable[[dict[str, Any]], None] | None = None  # the coordinator: a new device's subentry data plus its "family"

    async def connect(self) -> None:
        """Every device connects itself."""

    async def unload(self) -> None:
        """Every device closes its own connection in its unload."""

    @classmethod
    def product_row(cls, data: dict[str, Any]) -> Any:
        # the brand's Product row for the product in the subentry data; None for an unknown product
        return cls.PRODUCTS.get(str(data.get("product", "")))

    def build_device(self, subentry_id: str, name: str, data: dict[str, Any], row: Any) -> EntityDevice:
        # the brand's device class, called with the product row
        return self.DEVICE(self.hass, self, subentry_id, name, data, row)

    def init_device(self, subentry_id: str, name: str, data: dict[str, Any]) -> EntityDevice | None:
        # an unknown product is skipped; a device built again replaces the old one
        if not (row := self.product_row(data)):
            _LOGGER.warning("%s: skipping %s: unknown product %s", self.manufacturer, data.get("sn"), data.get("product"))
            return None
        if (old := self.devices.get(data["sn"])) is not None:
            self.remove_device(old)

        # a power device joins the fleet when it may be steered; every device is in the coordinator's list
        device = self.build_device(subentry_id, name, data, row)
        if isinstance(device, DistributionDevice) and device.allow_control:
            self.distribution.devices.append(device)
            device.on_invalidate = self.distribution.on_invalidate
        self.devices[device.sn] = device
        self._all_devices.append(device)
        return device

    def remove_device(self, device: EntityDevice) -> None:
        # unloaded and out of every list it is in
        device.unload()
        self.devices.pop(getattr(device, "sn", ""), None)
        for collection in (self._all_devices, self.distribution.devices):
            if device in collection:
                collection.remove(device)
        # the fleet order and its capacity without it from the next tick
        self.distribution.on_invalidate()

    @classmethod
    def device_family(cls, data: dict[str, Any]) -> str | None:
        row = cls.product_row(data)
        return row.family if row else None
