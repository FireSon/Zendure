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
from ..const import DOMAIN, PhaseModi
from ..device import DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from ..modbus import decode
from ..number import HaNumber
from ..select import HaSelect
from ..sensor import HaSensor

if TYPE_CHECKING:
    from ..distribution import DistributionMode
    from .driver import MarstekDriver

_LOGGER = logging.getLogger(__name__)

PORT = 502
POLL_S = 2.0  # telemetry every 2 s, Omnibattery's fast interval
HOLD_S = 3.0  # a device is left alone this long after a command: the power follows in 2.5 to 3.5 s (ViperRNMC/marstek_venus_modbus#276)
SLOW_EVERY = 15  # temperature, capacity, limits and work mode once per this many polls
OFFLINE_POLLS = 3  # failed polls before the device counts as offline
POWER_SET = 40  # W: the measured power is this close to the setpoint, the command is in
POWER_MAX_TIME = 10  # s: or this long after the command
RESEND_S = 30  # s: an accepted setpoint goes again only after this long, in case the device lost it
DOD_DEFAULT = 88  # % the device discharges by default; soc_min starts at 100 - this

# the control registers, the same for every generation (ffunes/Omnibattery const/registers_common.py)
RS485, FORCE, CHARGE, DISCHARGE, WORK_MODE = 42000, 42010, 42020, 42021, 43000
RS485_ON, RS485_OFF = 0x55AA, 0x55BB  # a command register: v3 rejects a plain 0/1
FORCE_NONE, FORCE_CHARGE, FORCE_DISCHARGE = 0, 1, 2

# telemetry per generation as key: (address, data type, scale), from Omnibattery's register tables; ac_power is taken positive for discharge,
# the opposite of battery_power as Omnibattery reads them (control/phase_power_limit.py, control/charge_delay.py; to verify on a device)
COMMON = {"temp": (35000, "int16", 0.1), "kwh": (32105, "uint16", 0.001), "charge_max": (44002, "uint16", 1), "discharge_max": (44003, "uint16", 1), "work_mode": (WORK_MODE, "uint16", 1)}
TELEMETRY = {
    "v2": {"soc": (32104, "uint16", 1), "ac_power": (32202, "int32", 1), "battery_power": (32102, "int32", 1)} | COMMON,
    "v3": {"soc": (37005, "uint16", 1), "ac_power": (30006, "int16", 1), "battery_power": (30001, "int16", 1)} | COMMON,
    "vA": {"soc": (32104, "uint16", 1), "ac_power": (30006, "int16", 1), "battery_power": (30001, "int16", 1)} | COMMON,
    "vD": {"soc": (32104, "uint16", 1), "ac_power": (30006, "int16", 1), "battery_power": (30001, "int16", 1)} | COMMON,
}
FAST = ("soc", "ac_power", "battery_power")
SLOW = ("temp", "kwh", "charge_max", "discharge_max")
MESSAGE_WAIT_S = {"v2": 0.05, "v3": 0.15, "vA": 0.15, "vD": 0.15}  # pause before every message; v3, A and D need 150 ms (Omnibattery MESSAGE_WAIT_MS)


class MarstekVenus(DistributionDevice):
    """A Venus E, A or D on its own Modbus TCP connection, directly or through an RS485 gateway."""

    CONFIG = [*IDENTITY, ConfigField("sn", {"text": {}}, cast=str, readonly=True), ConfigField("ip", {"text": {}}, cast=str), ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False), *FLEET, ConfigField("temp", {"entity": {}}, readonly=True)]

    def __init__(self, hass: HomeAssistant, driver: MarstekDriver, subentry_id: str | None, name: str, data: dict[str, Any], pwr_max: int) -> None:
        self.driver = driver
        self.sn: str = data["sn"]  # the identity: the ip it was added with
        self.model: str = data["product"]  # the register generation: v2, v3, vA or vD
        self.ip: str = data.get("ip") or self.sn
        self.port = int(data.get("port") or PORT)
        self.on_config_changed: Any = None  # the coordinator: persist subentry_data(); nothing is learned over Modbus
        self.home_input = self.home_output = self.battery_input = self.battery_output = 0
        self.work_mode: int | None = None  # user_work_mode before we took control, put back when we hand it back

        # the registry entry first: the entities find their device there
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=f"Venus ({self.model})")
        DistributionDevice.__init__(self, hass, self.sn, [ProfileDischarge(pwr_max), ProfileCharge(-pwr_max)], name, subentry_id)
        self.entities["soc_min"].update_value(100 - DOD_DEFAULT)
        self.entities["soc_max"].update_value(100)

        # the loop sends a pending command first, else polls
        self.registers = TELEMETRY[self.model]
        self.wait = MESSAGE_WAIT_S[self.model]
        self.client = AsyncModbusTcpClient(self.ip, port=self.port)
        self._pending: tuple[str, int] | None = None  # ("power", W) or ("off", 0)
        self._control = False  # RS485 control mode is on: ours since the last power()
        self._silent = 0
        self._polls = 0
        self._wake = asyncio.Event()
        self.device_task = asyncio.get_running_loop().create_task(self.device_loop())

    def unload(self) -> None:
        # the loop stops; a device under our control is handed back on its way out, then the connection closes
        self.device_task.cancel()
        self.hass.async_create_task(self.close())

    async def close(self) -> None:
        try:
            if self._control:
                await self.send("off", 0)
        finally:
            self.client.close()

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "ip": self.ip, "port": self.port}

    def config_changed(self) -> None:
        if self.on_config_changed is not None:
            self.on_config_changed(self)

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement")
        HaNumber(self, "soc_min", uom="%", minimum=0, maximum=100, restore=True, on_changed=self.soc_changed)
        HaNumber(self, "soc_max", uom="%", minimum=0, maximum=100, restore=True, on_changed=self.soc_changed)
        HaNumber(self, "individual", uom="W", minimum=-10000, maximum=10000, on_changed=lambda _entity, _value: None)  # the setpoint of the individual mode, EM's own: nothing goes to the device

        HaSensor(self, "home_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "home_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "temp", uom="°C", device_class="temperature", state_class="measurement", precision=1)
        self.setpoint = HaSensor(self, "power_setpoint", uom="W", device_class="power", state_class="measurement")
        HaSelect(self, "power_phase", PhaseModi, on_changed=self.update_phase, restore=True)

        self.kwhsensor = HaSensor(self, "kwh", uom="kWh", device_class="energy", restore=True)
        HaSensor(self, "rte", uom="%", state_class="measurement", precision=1)

    def soc_changed(self, entity: Any, value: Any) -> None:
        # soc_min and soc_max are ZenMaster's own limits, nothing goes to the device; the fleet order follows them
        setattr(self, entity.attr, value)
        if self.on_invalidate is not None:
            self.on_invalidate()

    def power(self, watts: int, mode: DistributionMode) -> int:
        watts = max(self.power_profile[1].power, min(self.power_profile[0].power, watts))  # clamp to the device limits, both directions

        # the device is busy with its last command, or unreachable
        if time.time() - self.power_since < HOLD_S or self.state == DeviceState.OFFLINE:
            return self.power_actual

        # an accepted setpoint is not sent again while the power is on its way; after RESEND_S once more, in case the device lost it
        if watts == self.power_setpoint and self.state >= DeviceState.CONFIRM and time.time() - self.state_since < RESEND_S:
            return self.power_actual

        # no new command while both the measured power and the last setpoint sit within 10 W of the new target
        if abs(self.power_actual - watts) < 10 and abs(self.power_setpoint - watts) < 10:
            return self.power_actual
        if self.power_setpoint != watts:
            self.power_since = time.time()
        self.setpoint.update_value(watts)  # publish the setpoint we are about to send, so the recorder holds the command side too
        self._pending = ("power", watts)
        self._wake.set()
        self.set_state(DeviceState.SENT)  # SENT, CONFIRM once the writes are accepted, ACTIVE once the power follows
        return self.power_actual

    def power_off(self) -> None:
        # the device returns to the work mode it had before we took control
        self._pending = ("off", 0)
        self._wake.set()
        self.setpoint.update_value(0)
        if self.state != DeviceState.OFFLINE:
            self.set_state(DeviceState.EXTERNAL)  # an offline device stays offline: it counts in no capacity until it reports again

    async def device_loop(self) -> None:
        while True:
            try:
                if (command := self._pending) is not None:
                    self._pending = None
                    await self.send(*command)
                else:
                    await self.poll()

                # a new command wakes the loop at once
                try:
                    async with asyncio.timeout(POLL_S):
                        await self._wake.wait()
                except TimeoutError:
                    pass
                self._wake.clear()

            except asyncio.CancelledError:
                raise
            except Exception:
                # the task must never die
                _LOGGER.exception("device task %s", self.device_id)

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
        # a setpoint: control mode on (a new connection can drop it), then both powers and force_mode, like Omnibattery's apply_setpoint;
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
            self._silent += 1
            _LOGGER.debug("Marstek %s: poll failed (%s)", self.name, err)
            if self._silent >= OFFLINE_POLLS and self.state != DeviceState.OFFLINE:
                self.power_actual = 0
                self.set_state(DeviceState.OFFLINE)
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
        changed = False
        if "kwh" in values and values["kwh"] and round(values["kwh"], 3) != self.kwh:
            self.kwhsensor.update_value(round(values["kwh"], 3))
            changed = True
        for profile, key, sign in ((self.power_profile[0], "discharge_max", 1), (self.power_profile[1], "charge_max", -1)):
            if (limit := int(values.get(key) or 0)) and sign * limit != profile.power:
                profile.power = sign * limit
                profile.__post_init__()  # floor and bands follow the new limit
                changed = True
        if changed and self.on_invalidate is not None:
            self.on_invalidate()

        # the power at the AC side and in the pack; the direction comes from ac_power
        p = int(values["ac_power"])
        battery = abs(int(values["battery_power"]))
        self.entity_update("home_output", max(0, p))
        self.entity_update("home_input", max(0, -p))
        self.entity_update("battery_output", battery if p > 0 else 0)
        self.entity_update("battery_input", battery if p < 0 else 0)
        self.power_actual = p
        if abs(self.power_actual - self.power_setpoint) < POWER_SET or (time.time() - self.state_since) > POWER_MAX_TIME:
            self.set_state(DeviceState.ACTIVE)
