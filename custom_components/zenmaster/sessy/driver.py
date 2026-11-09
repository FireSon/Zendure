"""SessyDriver: Sessy batteries over the dongle's local HTTP API; every battery is added by hand with the credentials on its sticker."""

from __future__ import annotations

from typing import Any, NamedTuple

import aiohttp

from ..config import ConfigField
from ..driver import Driver
from .battery import TIMEOUT, SessyBattery, basic_auth


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


class SessyDriver(Driver):
    """No discovery: mDNS would not hand over the password, so the driver only builds and drops the batteries."""

    id = "sessy"  # the settings section and the subentry type of the devices
    manufacturer = "Sessy"
    PRODUCTS = PRODUCTS
    DEVICE = SessyBattery
    ADD = [ConfigField("product", {"select": {"options": list(PRODUCTS)}}, cast=str), ConfigField("ip", {"text": {}}, cast=str), ConfigField("username", {"text": {}}, cast=str), ConfigField("password", {"text": {"type": "password"}}, cast=str)]  # the model sets the capacity: the API reports none
    KEY = "ip"  # the host identifies the battery

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
