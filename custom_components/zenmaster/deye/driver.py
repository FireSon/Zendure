"""DeyeDriver: Deye three-phase hybrid inverters through their Solarman logger stick; every inverter is added by hand."""

from __future__ import annotations

import asyncio
from typing import Any, NamedTuple

from ..config import ConfigField
from ..driver import Driver
from .inverter import DeyeInverter


class Product(NamedTuple):
    family: str  # names the device (family_<serial tail>)
    volt_scale: float  # V per raw unit of the battery voltage
    power_scale: int  # W per raw unit of the battery and PV power


# the two battery types of ha-solarman's deye_p3 profile: every register with two scales takes the first on LV, the second on HV
DISCOVERY_PORT = 48899  # UDP: the stick answers a search text here with "ip,mac,serial" (ha-solarman discovery)
DISCOVERY = (b"WIFIKIT-214028-READ", b"HF-A11ASSISTHREAD")  # the two search texts a stick listens to
DISCOVERY_S = 3.0  # s to wait for its answer

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
    ADD = [  # the battery type picks the scales, the serial printed on the stick is the identity, the capacity is the battery's: the inverter does not know it
        ConfigField("product", {"select": {"options": list(PRODUCTS)}}, cast=str),
        ConfigField("sn", {"text": {}}, cast=str),
        ConfigField("kwh", {"number": {"min": 0.1, "step": 0.1, "unit_of_measurement": "kWh", "mode": "box"}}, cast=float),
        ConfigField("ip", {"text": {}}, cast=str),
        ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False),
    ]

    @classmethod
    async def identify(cls, hass, data: dict[str, Any]) -> dict[str, Any] | str:
        # the number that goes into every V5 frame comes from the stick itself, whatever serial is printed on it; no answer is an error key
        loop = asyncio.get_running_loop()
        reply = loop.create_future()

        class Answer(asyncio.DatagramProtocol):
            def datagram_received(self, payload: bytes, _address: Any) -> None:
                if not reply.done():
                    reply.set_result(payload)

        try:
            transport, _ = await loop.create_datagram_endpoint(Answer, remote_addr=(data["ip"], DISCOVERY_PORT))
        except OSError:
            return "cannot_connect"

        # both search texts go out, the first answer counts
        try:
            for message in DISCOVERY:
                transport.sendto(message)
            fields = (await asyncio.wait_for(reply, DISCOVERY_S)).decode(errors="ignore").split(",")
        except (TimeoutError, OSError):
            return "cannot_connect"
        finally:
            transport.close()
        return {"serial": int(fields[2])} if len(fields) == 3 and fields[2].strip().isdigit() else "cannot_connect"
