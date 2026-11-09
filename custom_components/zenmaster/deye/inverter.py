"""DeyeInverter: a Deye three-phase hybrid inverter behind its Solarman logger stick, steered per fleet mode through its own settings."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ..config import IDENTITY, ConfigField
from ..const import DOMAIN
from ..device import DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from ..distribution import DistributionMode
from ..entity import EntityDevice
from ..modbus import decode
from ..sensor import HaSensor
from .logger import PORT, Logger, Refused

if TYPE_CHECKING:
    from .driver import DeyeDriver, Product

_LOGGER = logging.getLogger(__name__)

POLL_S = 5.0  # the fast set every 5 s; to be set from the measured latency when the Deye becomes the P1 meter
SLOW_EVERY = 12  # temperature and work mode once per this many polls
OFFLINE_POLLS = 3  # failed polls before the inverter counts as offline

# the registers of ha-solarman's deye_p3 profile
TEMP, VOLTAGE, SOC, BATTERY_POWER, WORK_MODE = 0x024A, 0x024B, 0x024C, 0x024E, 0x008E
PV = range(0x02A0, 0x02A4)  # the four MPPT inputs
GRID = (("phase1_power", 0x026E), ("phase2_power", 0x026F), ("phase3_power", 0x0270))  # key, low word: a signed 16-bit word holds any phase up to 63 A
FAST = ((VOLTAGE, 0x0271 - VOLTAGE), (PV[0], len(PV)))  # battery up to the grid phases (38), the PV inputs (4)

# the control registers of ha-solarman's deye_p3 profile; not verified on a device
CHARGE_A, DISCHARGE_A, GRID_CHARGE_A, GRID_CHARGE, SOLAR_SELL, TOU = 0x006C, 0x006D, 0x0080, 0x0082, 0x0091, 0x0092
SELL_POWER = 0x008F  # max sell power, W at the TOU scale: caps what goes to the grid
POWERS, SOCS, SOURCES = range(0x009A, 0x00A0), range(0x00A6, 0x00AC), range(0x00AC, 0x00B2)  # per TOU program: discharge power, SOC floor or target, charge source (1 = grid)
WRITES = (CHARGE_A, DISCHARGE_A, GRID_CHARGE_A, GRID_CHARGE, WORK_MODE, SELL_POWER, SOLAR_SELL, TOU, *POWERS, *SOCS, *SOURCES)  # every register we may change, put back on hand back
CONTROL = ((CHARGE_A, 2), (GRID_CHARGE_A, 3), (WORK_MODE, 5), (POWERS[0], 24))  # the blocks that hold them
ZERO_EXPORT_CT, EXPORT_FIRST, TOU_WEEK = 2, 0, 0x00FF
RATED = 0x0014  # the inverter's rated AC power, 0.1 W in two words, low word first
SLOW = ((TEMP, 1), (WORK_MODE, 1), (CHARGE_A, 2), (RATED, 2))  # temperature, work mode, its own maximum charge and discharge current, its rated power


# the grid meter of a Deye as its own device: chosen as P1 it carries the grid entities only
class DeyeMeter(EntityDevice):
    def entity_create(self) -> None:
        # the grid total and its phases
        HaSensor(self, "grid_power", uom="W", device_class="power", state_class="measurement")
        for key, _address in GRID:
            HaSensor(self, key, uom="W", device_class="power", state_class="measurement")


class DeyeInverter(DistributionDevice):
    """A Deye with its battery behind one logger stick; it regulates itself (CONTROLLER) until we steer it through its settings."""

    CONFIG = [
        *IDENTITY,
        ConfigField("sn", {"text": {}}, cast=str, readonly=True),
        ConfigField("ip", {"text": {}}, cast=str),
        ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False),
        ConfigField("kwh", {"number": {"step": 0.1, "unit_of_measurement": "kWh"}}, cast=float, readonly=True),
        *(ConfigField(k, {"entity": {}}, readonly=True) for k in ("soc", "battery_voltage", "battery_input", "battery_output", "pv_power", "temp", "work_mode")),
    ]

    def __init__(self, hass: HomeAssistant, driver: DeyeDriver, subentry_id: str | None, name: str, data: dict[str, Any], product: Product) -> None:
        self.driver = driver
        self.sn: str = str(data["sn"])  # the identity: the serial of the logger stick, also in every V5 frame
        self.model: str = data["product"]  # the battery type: LV or HV
        self.product = product
        self.ip: str = data["ip"]
        self.port = int(data.get("port") or PORT)
        self.on_config_changed: Any = None  # the coordinator: persist subentry_data(); nothing is learned over the stick

        # the registry entry first: the entities find their device there; the power profile follows from its own maximum currents at the first poll
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=f"Hybrid ({self.model})")
        DistributionDevice.__init__(self, hass, self.sn, [ProfileDischarge(0), ProfileCharge(0)], name, subentry_id)
        self.kwhsensor.update_value(float(data["kwh"]))

        # the grid meter as its own device in the same subentry, so it goes with it
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, f"{self.sn}_meter")}, name=f"{name}_meter", manufacturer=driver.manufacturer, model="Meter")
        self.meter = DeyeMeter(hass, f"{self.sn}_meter", f"{name}_meter", subentry_id)

        self.logger = Logger(self.ip, int(self.sn), self.port)
        self._silent = 0
        self._polls = 0

        # the control: the inverter's own settings while we steer it (in the subentry, so a crash keeps them), what we wrote, the request and the last one sent
        self.battery_voltage = 0.0  # V, the entity writes it: a grid charge current is W over this
        self.saved: dict[int, int] = {int(address): value for address, value in data.get("saved", {}).items()}
        self.written: dict[int, int] = {}
        self.request: tuple[int, DistributionMode] | None = None
        self.sent: tuple[int, DistributionMode] | None = None
        self.device_task = asyncio.get_running_loop().create_task(self.device_loop())

    def unload(self) -> None:
        # the loop stops; an inverter under our control is handed back on its way out, then the connection closes
        self.device_task.cancel()
        self.hass.async_create_task(self.close())

    async def close(self) -> None:
        try:
            if self.saved:
                await self.restore()
        finally:
            self.logger.close()

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "ip": self.ip, "port": self.port, "kwh": self.kwh, "saved": self.saved}

    def config_changed(self) -> None:
        if self.on_config_changed is not None:
            self.on_config_changed(self)

    def power(self, watts: int, mode: DistributionMode) -> int:
        # the loop sends a request once; the same one every tick changes nothing
        if self.state != DeviceState.OFFLINE:
            self.request = (watts, mode)
            self.power_setpoint = watts if mode in (DistributionMode.INDIVIDUAL, DistributionMode.MANUAL) else 0
        return self.power_actual

    def power_off(self) -> None:
        # hand back: without a request the loop puts the inverter's own settings back
        self.request = None
        self.power_setpoint = 0
        if self.state != DeviceState.OFFLINE:
            self.set_state(DeviceState.CONTROLLER)

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement")
        HaSensor(self, "battery_voltage", uom="V", device_class="voltage", state_class="measurement", precision=1)
        HaSensor(self, "battery_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "pv_power", uom="W", device_class="power", state_class="measurement")
        HaSensor(self, "temp", uom="°C", device_class="temperature", state_class="measurement", precision=1)
        HaSensor(self, "work_mode")
        self.kwhsensor = HaSensor(self, "kwh", uom="kWh", device_class="energy", restore=True)

    async def device_loop(self) -> None:
        while True:
            try:
                # hand back or a new request first, then the poll
                if self.request is None and self.saved:
                    await self.restore()
                elif self.request not in (None, self.sent):
                    await self.send(*self.request)
                await self.poll()
            except Exception:
                # the task must never die
                _LOGGER.exception("device task %s", self.device_id)
            await asyncio.sleep(POLL_S)

    async def send(self, watts: int, mode: DistributionMode) -> None:
        try:
            # the inverter's own settings, read once before the first write
            if not self.saved:
                saved: dict[int, int] = {}
                for start, count in CONTROL:
                    saved.update(zip(range(start, start + count), await self.logger.read(start, count)))
                self.saved = saved
                self.config_changed()

            # per mode what it wants: a setpoint through every TOU program, at rest both currents closed, SMART_* on its own CT with one current closed
            setpoint = mode in (DistributionMode.INDIVIDUAL, DistributionMode.MANUAL)
            charge, discharge = self.saved[CHARGE_A], self.saved[DISCHARGE_A]  # open: the inverter's own limits
            if setpoint and watts:
                amps = round(-watts / self.battery_voltage) if watts < 0 and self.battery_voltage else 0
                wanted = {
                    WORK_MODE: EXPORT_FIRST, TOU: TOU_WEEK, GRID_CHARGE: int(watts < 0), GRID_CHARGE_A: amps, CHARGE_A: charge, DISCHARGE_A: discharge,
                    SELL_POWER: max(self.saved[SELL_POWER], max(0, watts) // self.product.power_scale),
                }
                wanted |= (
                    dict.fromkeys(POWERS, max(0, watts) // self.product.power_scale) | dict.fromkeys(SOCS, int(self.soc_min if watts > 0 else self.soc_max))
                    | {a: self.saved[a] & ~1 | int(watts < 0) for a in SOURCES}  # only the grid bit; the other bits (Sell on newer firmware) stay the inverter's
                )
            elif setpoint or mode == DistributionMode.OFF:
                wanted = {CHARGE_A: 0, DISCHARGE_A: 0, SOLAR_SELL: 1}
            else:
                bypass = mode == DistributionMode.SMART_DISCHARGING
                wanted = {
                    WORK_MODE: ZERO_EXPORT_CT, TOU: 0, GRID_CHARGE: 0, SOLAR_SELL: 1 if bypass else self.saved[SOLAR_SELL],
                    CHARGE_A: 0 if bypass else charge, DISCHARGE_A: 0 if mode in (DistributionMode.SMART_CHARGING, DistributionMode.STORE_SOLAR) else discharge,
                }

            # only what differs from what we wrote goes to the inverter
            for address, value in wanted.items():
                if self.written.get(address) != value:
                    await self.write(address, value)
                    self.written[address] = value
        except OSError as err:
            _LOGGER.warning("Deye %s: settings not written (%s)", self.name, err)
            return

        # sent: a setpoint or rest is ours, SMART_* regulate themselves on our settings
        self.sent = (watts, mode)
        self.set_state(DeviceState.ACTIVE if setpoint or mode == DistributionMode.OFF else DeviceState.EXTERNAL)

    async def restore(self) -> None:
        # hand back: every register we may have changed gets the inverter's own value again; a failure tries again next round
        try:
            for address in WRITES:
                await self.write(address, self.saved[address])
        except OSError as err:
            _LOGGER.warning("Deye %s: hand back failed (%s)", self.name, err)
            return
        self.saved, self.written, self.sent = {}, {}, None
        self.config_changed()

    async def write(self, address: int, value: int) -> None:
        # one setting; a register the inverter refuses is logged and left, the rest still goes
        try:
            await self.logger.write(address, value)
        except Refused as err:
            _LOGGER.warning("Deye %s: register 0x%04X refused (%s)", self.name, address, err)

    async def poll(self) -> None:
        # the fast set every round in two reads; temperature and work mode now and then
        self._polls += 1
        try:
            values: dict[int, int] = {}
            for start, count in FAST + (SLOW if self._polls % SLOW_EVERY == 1 else ()):
                values.update(zip(range(start, start + count), await self.logger.read(start, count)))
        except OSError as err:
            self._silent += 1
            _LOGGER.debug("Deye %s: poll failed (%s)", self.name, err)
            if self._silent >= OFFLINE_POLLS and self.state != DeviceState.OFFLINE:
                self.power_actual = 0
                self.set_state(DeviceState.OFFLINE)
            return
        self._silent = 0
        self.update_status(values)

    def update_status(self, r: dict[int, int]) -> None:
        # the battery; its power is taken positive for discharge (to verify on a device)
        self.entity_update("soc", r[SOC])
        self.entity_update("battery_voltage", round(r[VOLTAGE] * self.product.volt_scale, 2))
        battery = decode([r[BATTERY_POWER]], "int16") * self.product.power_scale
        self.entity_update("battery_output", max(0, battery))
        self.entity_update("battery_input", max(0, -battery))
        self.entity_update("pv_power", sum(r[a] for a in PV) * self.product.power_scale)

        # the grid at the Deye's own meter: each phase from its low word alone, so no value mixes two moments; the total is their sum
        phases = [decode([r[address]], "int16") for _key, address in GRID]
        for (key, _address), watts in zip(GRID, phases):
            self.meter.entity_update(key, watts)
        self.meter.entity_update("grid_power", sum(phases))
        if TEMP in r:
            self.entity_update("temp", round((r[TEMP] - 1000) * 0.1, 1))
            self.entity_update("work_mode", r[WORK_MODE])

        # its power limits: its own maximum currents at the battery voltage, never above its rated power; while we steer it the saved currents, since we close them ourselves
        if CHARGE_A in r:
            currents, rated, changed = self.saved or r, (r[RATED + 1] << 16 | r[RATED]) // 10, False
            for profile, address, sign in ((self.power_profile[0], DISCHARGE_A, 1), (self.power_profile[1], CHARGE_A, -1)):
                by_current = round(currents[address] * self.battery_voltage, -2)
                if (limit := sign * int(min(by_current, rated) if rated else by_current)) != profile.power:
                    profile.power = limit
                    profile.__post_init__()  # floor and bands follow the new limit
                    changed = True
            if changed and self.on_invalidate is not None:
                self.on_invalidate()

        # it regulates itself while we do not steer it
        self.power_actual = battery
        if not self.saved:
            self.set_state(DeviceState.CONTROLLER)
