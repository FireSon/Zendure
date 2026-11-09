"""AnkerSolarbank: an Anker SOLIX Solarbank over Modbus TCP, steered in Third-Party Control with one signed power setpoint."""

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
from ..modbus import decode, encode
from ..number import HaNumber
from ..select import HaSelect
from ..sensor import HaSensor

if TYPE_CHECKING:
    from ..distribution import DistributionMode
    from .driver import AnkerDriver, Product

_LOGGER = logging.getLogger(__name__)

PORT = 502
POLL_S = 2.0  # telemetry every 2 s; Anker's own integration polls every 5 s
HOLD_S = 2.0  # a device is left alone this long after a command; Anker applies a setpoint at once (anker-charging#91), Omnibattery counts 1 s
SLOW_EVERY = 15  # temperature, capacity, limits and operating mode once per this many polls
OFFLINE_POLLS = 3  # failed polls before the device counts as offline
POWER_SET = 40  # W: the measured power is this close to the setpoint, the command is in
POWER_MAX_TIME = 10  # s: or this long after the command
RESEND_S = 30  # s: an accepted setpoint goes again only after this long, in case a reboot reset it to 0 (anker-charging#91)
MIN_W = 100  # 1 to 99 W is refused by the device (anker-charging#103): such a request becomes 0
PWR_MAX = 3500  # W both ways until the device reports its own limits (10036/10038); Omnibattery's envelope for the family

# holding registers: the operating mode and the setpoint, which lives in RAM and may be written as often as needed (anker-charging#91),
# and the charge and discharge soc limits the device keeps itself, which become soc_max and soc_min
MODE, SETPOINT, SOC_MAX, SOC_MIN = 10064, 10071, 60000, 60001
THIRD_PARTY = 3  # operating mode Third-Party Control; 0 is self-consumption, 7 dynamic pricing
SERIAL = 10100  # input registers: the serial, 12 registers of ASCII

# 10064 value to its bit in the mode mask (input 32774, Anker's config/8fcbb87c... option_capability_bits); a mode without its bit falls back to 4 (anker-charging#157)
MODE_BIT = {0: 0, 1: 1, 3: 5, 4: 2, 5: 4, 6: 3, 7: 6}
MODE_MASK = 32774
FALLBACK = 0  # self-consumption: handed back when the saved mode is no longer offered

# input registers as key: (address, data type, scale), from Anker's own register map (anker-charging/ha-anker-solix-official config/8fcbb87c...)
FAST = {"soc": (10014, "uint16", 1), "battery_power": (10008, "int32", 1), "pv": (10002, "int32", 1)}
SLOW = {"temp": (10156, "int16", 0.1), "kwh": (10250, "uint32", 0.1), "charge_max": (10036, "int32", 1), "discharge_max": (10038, "int32", 1)}


class AnkerSolarbank(DistributionDevice):
    """A Solarbank Max AC, XE AC or 4 E5000 Pro on its own Modbus TCP connection."""

    CONFIG = [*IDENTITY, ConfigField("sn", {"text": {}}, cast=str, readonly=True), ConfigField("ip", {"text": {}}, cast=str), ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False), *FLEET, ConfigField("temp", {"entity": {}}, readonly=True)]

    def __init__(self, hass: HomeAssistant, driver: AnkerDriver, subentry_id: str | None, name: str, data: dict[str, Any], product: Product) -> None:
        self.driver = driver
        self.sn: str = data["sn"]
        self.model: str = data["product"]  # the product code from the serial
        self.ip: str = data["ip"]
        self.port = int(data.get("port") or PORT)
        self.dc_pv = product.dc_pv
        self.on_config_changed: Any = None  # the coordinator: persist subentry_data() after the ip was learned
        self.mode_mask: int | None = None  # the modes the firmware offers now; None until read
        self.home_input = self.home_output = self.battery_input = self.battery_output = 0
        self.mode: int | None = data.get("mode")  # the operating mode before we took control, put back when we hand it back; kept in the subentry so a crash does not lose it

        # the registry entry first: the entities find their device there
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=f"Solarbank ({self.model})")
        DistributionDevice.__init__(self, hass, self.sn, [ProfileDischarge(PWR_MAX), ProfileCharge(-PWR_MAX)], name, subentry_id)

        # the loop sends a pending command first, else polls
        self.client = AsyncModbusTcpClient(self.ip, port=self.port)
        self._pending: tuple[str, int] | None = None  # ("power", W) or ("off", 0)
        self._control = False  # Third-Party Control is ours since the last power()
        self._silent = 0
        self._polls = 0
        self._wake = asyncio.Event()
        self.device_task = asyncio.get_running_loop().create_task(self.device_loop())

    def unload(self) -> None:
        # the loop stops; a device under our control is handed back on its way out, then the connection closes.
        # This matters more here than anywhere: the Solarbank keeps running its last setpoint without a timeout (anker-charging#150)
        self.device_task.cancel()
        self.hass.async_create_task(self.close())

    async def close(self) -> None:
        try:
            if self._control:
                await self.send("off", 0)
        finally:
            self.client.close()

    def moved(self, ip: str) -> None:
        # mDNS found the device on a new address: a new connection there, and the subentry follows
        self.client.close()
        self.ip = ip
        self.client = AsyncModbusTcpClient(ip, port=self.port)
        self.config_changed()

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "ip": self.ip, "port": self.port, "mode": self.mode}

    def config_changed(self) -> None:
        if self.on_config_changed is not None:
            self.on_config_changed(self)

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement")
        HaSensor(self, "soc_min", uom="%", state_class="measurement")  # the device's own discharge limit
        HaSensor(self, "soc_max", uom="%", state_class="measurement")  # the device's own charge limit
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

    def power(self, watts: int, mode: DistributionMode) -> int:
        watts = max(self.power_profile[1].power, min(self.power_profile[0].power, watts))  # clamp to the device limits, both directions
        if abs(watts) < MIN_W:
            watts = 0

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
        # the device returns to the operating mode it had before we took control
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

    async def read(self, address: int, kind: str, holding: bool = False) -> int:
        count = 2 if kind in ("int32", "uint32") else 1
        result = await (self.client.read_holding_registers if holding else self.client.read_input_registers)(address, count=count)
        if result.isError():
            raise ModbusException(f"read {address}: {result}")
        return decode(result.registers, kind)

    async def write(self, address: int, value: int, kind: str = "uint16") -> bool:
        # a 32-bit value goes as one multi-register write (FC16), a 16-bit one as a single register (FC06)
        if kind == "int32":
            return not (await self.client.write_registers(address, encode(value, kind))).isError()
        return not (await self.client.write_register(address, value)).isError()

    async def send(self, kind: str, watts: int) -> None:
        # a setpoint: Third-Party Control, then the signed setpoint (positive discharges, Anker's own number.py writes a charge negative);
        # off: setpoint 0 and the old operating mode back; a device we never took stays untouched
        if kind == "off" and not self._control:
            return
        try:
            if not self.client.connected:
                await self.client.connect()
            if kind == "power":
                # a firmware that does not offer Third-Party Control would fall back to Custom: we do not take it
                if self.mode_mask is not None and not self.mode_mask >> MODE_BIT[THIRD_PARTY] & 1:
                    _LOGGER.warning("Anker %s: Third-Party Control not offered by the firmware (mode mask %s)", self.name, self.mode_mask)
                    return
                ok = await self.write(MODE, THIRD_PARTY) and await self.write(SETPOINT, watts, "int32")
                self._control = True
            else:
                ok = await self.write(SETPOINT, 0, "int32")
                if ok and self.mode is not None:
                    # only a mode the firmware still offers goes back, else self-consumption
                    mode = self.mode if self.mode in MODE_BIT and (self.mode_mask is None or self.mode_mask >> MODE_BIT[self.mode] & 1) else FALLBACK
                    ok = await self.write(MODE, mode)
                self._control = not ok
        except (ModbusException, OSError, TimeoutError) as err:
            self.client.close()  # a late reply left in the buffer fails every next read on its transaction id (anker-charging#81): start clean
            _LOGGER.warning("Anker %s: %s not sent (%s)", self.name, kind, err)
            return
        if not ok:
            _LOGGER.warning("Anker %s: %s refused by the device", self.name, kind)
        elif kind == "power" and self.state == DeviceState.SENT:
            self.set_state(DeviceState.CONFIRM)

    async def poll(self) -> None:
        # soc and power every round; the rest changes slowly; the operating mode only while it is the device's own
        self._polls += 1
        slow = self._polls % SLOW_EVERY == 1
        try:
            if not self.client.connected:
                await self.client.connect()
            values = {key: await self.read(address, kind) * scale for key, (address, kind, scale) in (FAST | SLOW if slow else FAST).items()}
            if slow:
                values |= {key: await self.read(address, "uint16", holding=True) for key, address in (("soc_max", SOC_MAX), ("soc_min", SOC_MIN), ("mode", MODE))}
                values["mode_mask"] = await self.read(MODE_MASK, "uint16")
        except (ModbusException, OSError, TimeoutError) as err:
            self.client.close()  # a late reply left in the buffer fails every next read on its transaction id (anker-charging#81): start clean
            self._silent += 1
            _LOGGER.debug("Anker %s: poll failed (%s)", self.name, err)
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
        if "mode_mask" in values:
            self.mode_mask = int(values["mode_mask"])

        # the mode to hand back is only learned while the device is its own: a revert to Smart or Custom during our control is not the user's choice
        if "mode" in values and (mode := int(values["mode"])) != THIRD_PARTY and not self._control and mode != self.mode:
            self.mode = mode
            self.config_changed()

        # the capacity, the soc limits and the power limits the device works with; a change reorders the fleet
        changed = False
        for key in ("soc_min", "soc_max"):
            if key in values and self.entity_update(key, int(values[key])):
                changed = True
        if values.get("kwh") and round(values["kwh"], 3) != self.kwh:
            self.kwhsensor.update_value(round(values["kwh"], 3))
            changed = True
        for profile, key, sign in ((self.power_profile[0], "discharge_max", 1), (self.power_profile[1], "charge_max", -1)):
            if (limit := int(values.get(key) or 0)) and sign * limit != profile.power:
                profile.power = sign * limit
                profile.__post_init__()  # floor and bands follow the new limit
                changed = True
        if changed and self.on_invalidate is not None:
            self.on_invalidate()

        # 10008 is positive while discharging; on a model with its PV behind the pack the AC side is pack plus PV (Omnibattery #366)
        battery = int(values["battery_power"])
        p = battery + int(values["pv"]) if self.dc_pv else battery
        self.entity_update("home_output", max(0, p))
        self.entity_update("home_input", max(0, -p))
        self.entity_update("battery_output", max(0, battery))
        self.entity_update("battery_input", max(0, -battery))
        self.power_actual = p
        if abs(self.power_actual - self.power_setpoint) < POWER_SET or (time.time() - self.state_since) > POWER_MAX_TIME:
            self.set_state(DeviceState.ACTIVE)
