"""SessyDriver: Sessy batteries over the dongle's local HTTP API; every battery is added by hand with the credentials on its sticker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, NamedTuple

import aiohttp

from ..config import ConfigField
from ..entity import EntityDevice
from .battery import TIMEOUT, SessyBattery, basic_auth

_LOGGER = logging.getLogger(__name__)


class Product(NamedTuple):
    family: str  # names the device (family_<host tail>)
    kwh: float  # usable capacity: the API reports none
    charge_w: int  # W the battery charges with at most
    discharge_w: int  # W it delivers at most


# the models on Sessy's data sheet (5 kWh: 5.2 kWh usable) and its 10 kWh model; both 2.2 kW in, 1.7 kW out
PRODUCTS: dict[str, Product] = {
    "5kWh": Product("Sessy", 5.2, 2200, 1700),
    "10kWh": Product("Sessy10", 10.0, 2200, 1700),
}


class SessyDriver:
    """No discovery: mDNS would not hand over the password, so the driver only builds and drops the batteries."""

    id = "sessy"  # the settings section and the subentry type of the devices
    manufacturer = "Sessy"
    CONFIG = [ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True)]  # the settings section of the driver: only its id
    ADD = [ConfigField("product", {"select": {"options": list(PRODUCTS)}}, cast=str), ConfigField("ip", {"text": {}}, cast=str), ConfigField("username", {"text": {}}, cast=str), ConfigField("password", {"text": {"type": "password"}}, cast=str)]  # the model sets the capacity: the API reports none
    KEY = "ip"  # the host identifies the battery

    def __init__(self, hass, entry_id: str, all_devices: list[EntityDevice], distribution) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._all_devices = all_devices
        self.distribution = distribution
        self.devices: dict[str, SessyBattery] = {}
        self.on_discovered: Callable[[dict[str, Any]], None] | None = None  # set by the coordinator like every driver; nothing is ever found

    async def connect(self) -> None:
        """Every battery opens its own session."""

    async def unload(self) -> None:
        """Every battery closes its own session in its unload."""

    @classmethod
    async def probe(cls, host: str, username: str, password: str) -> str | None:
        # the strategy needs the credentials, power/status not on every firmware (Omnibattery sessy.py): None when they work, else the error key
        try:
            async with aiohttp.ClientSession(headers={"Authorization": basic_auth(username, password)}, timeout=TIMEOUT) as session, session.get(f"http://{host}/api/v1/power/active_strategy") as response:
                return "invalid_auth" if response.status == 401 else None if response.ok else "cannot_connect"
        except (aiohttp.ClientError, TimeoutError):
            return "cannot_connect"

    @classmethod
    async def identify(cls, hass, data: dict[str, Any]) -> dict[str, Any] | str:
        # the credentials are tried on the dongle before the battery is added: a wrong password would only show on the first command
        return await cls.probe(data["ip"], data.get("username", ""), data.get("password", "")) or {}

    def init_device(self, subentry_id: str, name: str, data: dict[str, Any]) -> EntityDevice | None:
        if not (row := PRODUCTS.get(str(data.get("product", "")))):
            _LOGGER.warning("Sessy: skipping %s: unknown model %s", data.get("sn"), data.get("product"))
            return None
        if (old := self.devices.get(data["sn"])) is not None:
            self.remove_device(old)
        device = SessyBattery(self.hass, self, subentry_id, name, data, row)
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
