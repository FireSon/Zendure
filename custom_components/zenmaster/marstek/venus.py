"""MarstekVenus: a Marstek Venus battery over Modbus TCP, steered with RS485 control mode, force_mode and the charge/discharge setpoints."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from ..config import FLEET, IDENTITY, ConfigField
from ..const import DOMAIN
from ..device import DeviceState, PolledDevice, ProfileCharge, ProfileDischarge
from ..modbus import decode
from ..number import HaNumber
from ..sensor import HaSensor

if TYPE_CHECKING:
    from .driver import MarstekDriver, Product

_LOGGER = logging.getLogger(__name__)

PORT = 502
SLOW_EVERY = 15  # temperature, capacity, limits and work mode once per this many polls
DOD_DEFAULT = 88  # % the device discharges by default; soc_min starts at 100 - this

# the control registers, the same for every generation (ViperRNMC/marstek_venus_modbus registers/*.yaml)
RS485, FORCE, CHARGE, DISCHARGE, WORK_MODE = 42000, 42010, 42020, 42021, 43000
RS485_ON, RS485_OFF = 0x55AA, 0x55BB  # a command register: v3 rejects a plain 0/1
FORCE_NONE, FORCE_CHARGE, FORCE_DISCHARGE = 0, 1, 2

# telemetry per generation as key: (address, data type, scale), from marstek_venus_modbus's register tables
COMMON = {"temp": (35000, "int16", 0.1), "kwh": (32105, "uint16", 0.001), "charge_max": (44002, "uint16", 1), "discharge_max": (44003, "uint16", 1), "work_mode": (WORK_MODE, "uint16", 1)}
TELEMETRY = {
    "v2": {"soc": (32104, "uint16", 1), "ac_power": (32202, "int32", 1), "battery_power": (32102, "int32", 1)} | COMMON,
    # v3's soc at 37005, the whole percent: marstek_venus_modbus reads 34002 in tenths
    "v3": {"soc": (37005, "uint16", 1), "ac_power": (30006, "int16", 1), "battery_power": (30001, "int16", 1)} | COMMON,
    "vA": {"soc": (32104, "uint16", 1), "ac_power": (30006, "int16", 1), "battery_power": (30001, "int16", 1)} | COMMON,
    "vD": {"soc": (32104, "uint16", 1), "ac_power": (30006, "int16", 1), "battery_power": (30001, "int16", 1)} | COMMON,
}
FAST = ("soc", "ac_power", "battery_power")
SLOW = ("temp", "kwh", "charge_max", "discharge_max")
MESSAGE_WAIT_S = {"v2": 0.05, "v3": 0.15, "vA": 0.15, "vD": 0.15}  # pause before every message; v3, A and D need 150 ms


class MarstekVenus(PolledDevice):
    """A Venus E, A or D on its own Modbus TCP connection, directly or through an RS485 gateway."""

    POLL_S = 2.0  # telemetry every 2 s
    HOLD_S = 3.0  # a device is left alone this long after a command: the power follows in 2.5 to 3.5 s (ViperRNMC/marstek_venus_modbus#276)

    CONFIG = [*IDENTITY, ConfigField("sn", {"text": {}}, cast=str, readonly=True), ConfigField("ip", {"text": {}}, cast=str), ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False), ConfigField("allow_control", {"boolean": {}}, cast=bool), *FLEET, ConfigField("temp", {"entity": {}}, readonly=True)]

    def __init__(self, hass: HomeAssistant, driver: MarstekDriver, subentry_id: str | None, name: str, data: dict[str, Any], product: Product) -> None:
        self.driver = driver
        self.sn: str = data["sn"]  # the identity: the ip it was added with
        self.model: str = data["product"]  # the register generation: v2, v3, vA or vD
        self.ip: str = data.get("ip") or self.sn
        self.port = int(data.get("port") or PORT)
        self.allow_control: bool = bool(data.get("allow_control"))  # off by default: the control is not confirmed on a device
        self.home_input = self.home_output = self.battery_input = self.battery_output = 0
        self.work_mode: int | None = None  # user_work_mode before we took control, put back when we hand it back

        # the registry entry first: the entities find their device there
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=f"Venus ({self.model})")
        PolledDevice.__init__(self, hass, self.sn, [ProfileDischarge(product.pwr_max), ProfileCharge(-product.pwr_max)], name, subentry_id)
        self.entities["soc_min"].update_value(100 - DOD_DEFAULT)
        self.entities["soc_max"].update_value(100)

        # its own Modbus TCP connection, which the loop of PolledDevice polls and commands over
        self.registers = TELEMETRY[self.model]
        self.wait = MESSAGE_WAIT_S[self.model]
        self.client = AsyncModbusTcpClient(self.ip, port=self.port)

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "ip": self.ip, "port": self.port, "allow_control": self.allow_control}

    async def disconnect(self) -> None:
        self.client.close()

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement")
        HaNumber(self, "soc_min", uom="%", minimum=0, maximum=100, restore=True, on_changed=self.soc_changed)
        HaNumber(self, "soc_max", uom="%", minimum=0, maximum=100, restore=True, on_changed=self.soc_changed)
        HaSensor(self, "temp", uom="°C", device_class="temperature", state_class="measurement", precision=1)
        super().entity_create()

    async def read(self, key: str) -> float:
        address, kind, scale = self.registers[key]
        await asyncio.sleep(self.wait)
        result = await self.client.read_holding_registers(address, count=2 if kind == "int32" else 1)
        if result.isError():
            raise ModbusException(f"read {key} at {address}: {result}")
        return decode(result.registers, kind) * scale

    async def write(self, address: int, value: int) -> bool:
        await asyncio.sleep(self.wait)
        return not (await self.client.write_register(address, value)).isError()

    async def send(self, kind: str, watts: int) -> None:
        # a setpoint: control mode on (a new connection can drop it), then both powers and force_mode;
        # off: force_mode none, control mode off and the old work mode back, the only reliable way out of control mode (ViperRNMC#294);
        # a device we never took stays untouched
        if kind == "off" and not self._control:
            return
        try:
            if not self.client.connected:
                await self.client.connect()
            if kind == "power":
                force = FORCE_DISCHARGE if watts > 0 else FORCE_CHARGE if watts < 0 else FORCE_NONE
                ok = await self.write(RS485, RS485_ON) and await self.write(DISCHARGE, max(0, watts)) and await self.write(CHARGE, max(0, -watts)) and await self.write(FORCE, force)
                self._control = True
            else:
                ok = await self.write(FORCE, FORCE_NONE) and await self.write(CHARGE, 0) and await self.write(DISCHARGE, 0) and await self.write(RS485, RS485_OFF)
                if ok and self.work_mode is not None:
                    ok = await self.write(WORK_MODE, self.work_mode)
                self._control = not ok
        except (ModbusException, OSError, TimeoutError) as err:
            self.client.close()  # a late reply left in the buffer fails every next read on its transaction id (anker-charging#81): start clean
            _LOGGER.warning("Marstek %s: %s not sent (%s)", self.name, kind, err)
            return
        if not ok:
            _LOGGER.warning("Marstek %s: %s refused by the device", self.name, kind)
        elif kind == "power" and self.state == DeviceState.SENT:
            self.set_state(DeviceState.CONFIRM)

    async def poll(self) -> None:
        # soc and power every round; the rest changes slowly; the work mode only while it is the device's own
        self._polls += 1
        keys = FAST + SLOW + (() if self._control else ("work_mode",)) if self._polls % SLOW_EVERY == 1 else FAST
        try:
            if not self.client.connected:
                await self.client.connect()
            values = {key: await self.read(key) for key in keys}
        except (ModbusException, OSError, TimeoutError) as err:
            self.client.close()  # a late reply left in the buffer fails every next read on its transaction id (anker-charging#81): start clean
            self.poll_failed(err)
            return
        self._silent = 0
        self.update_status(values)

    def update_status(self, values: dict[str, float]) -> None:
        self.entity_update("soc", int(values["soc"]))
        if "temp" in values:
            self.entity_update("temp", round(values["temp"], 1))
        if "work_mode" in values:
            self.work_mode = int(values["work_mode"])

        # the capacity and the limits the device works with; a change reorders the fleet
        if self.update_limits(values) and self.on_invalidate is not None:
            self.on_invalidate()

        # the power at the AC side and in the pack; the direction from battery_power, positive for charge (marstek_venus_modbus sensor.py)
        battery = int(values["battery_power"])
        p = abs(int(values["ac_power"])) * (1 if battery < 0 else -1)
        self.entity_update("home_output", max(0, p))
        self.entity_update("home_input", max(0, -p))
        self.entity_update("battery_output", max(0, -battery))
        self.entity_update("battery_input", max(0, battery))
        self.power_actual = p
        if self.power_reached(time.time()):
            self.set_state(DeviceState.ACTIVE)
