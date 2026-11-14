"""DeyeInverter: a Deye three-phase hybrid inverter behind its Solarman logger stick, steered per fleet mode through its own settings."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ..config import IDENTITY, ConfigField
from ..const import DOMAIN, PANEL_TITLE
from ..device import DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from ..distribution import DistributionMode
from ..entity import EntityDevice
from ..modbus import decode, text
from ..sensor import HaSensor
from .battery import DeyeBattery
from .logger import MODBUS_PORT, PORT, Logger, ModbusTcp, Refused

if TYPE_CHECKING:
    from .driver import DeyeDriver, Product

_LOGGER = logging.getLogger(__name__)

POLL_S = 5.0  # the fast set every 5 s; to be set from the measured latency when the Deye becomes the P1 meter
STOP_S = 10.0  # s the hand back on unload waits for the loop to stop
MIN_W = 260  # W: the smallest a setpoint gets as a current, 5 A on LV at 52 V, 1 A on HV at 400 V
SLOW_EVERY = 12  # temperature and work mode once per this many polls
OFFLINE_POLLS = 3  # failed polls before the inverter counts as offline

# the registers of ha-solarman's deye_p3 profile
TEMP, VOLTAGE, SOC, BATTERY_POWER, WORK_MODE = 0x024A, 0x024B, 0x024C, 0x024E, 0x008E
PV = range(0x02A0, 0x02A4)  # the four MPPT inputs
GRID = (("phase1_power", 0x026E), ("phase2_power", 0x026F), ("phase3_power", 0x0270))  # key, low word: a signed 16-bit word holds any phase up to 63 A
FAST = ((VOLTAGE, 0x0271 - VOLTAGE), (PV[0], len(PV)))  # battery up to the grid phases (38), the PV inputs (4)

# the control registers of ha-solarman's deye_p3 profile, writable there with the same values
CHARGE_A, DISCHARGE_A, GRID_CHARGE_A, GRID_CHARGE, SOLAR_SELL, TOU = 0x006C, 0x006D, 0x0080, 0x0082, 0x0091, 0x0092
SELL_POWER = 0x008F  # max sell power, W at the TOU scale: caps what goes to the grid
POWERS, SOCS, SOURCES = range(0x009A, 0x00A0), range(0x00A6, 0x00AC), range(0x00AC, 0x00B2)  # per TOU program: discharge power, SOC floor or target, charge source (1 = grid)
WRITES = (CHARGE_A, DISCHARGE_A, GRID_CHARGE_A, GRID_CHARGE, WORK_MODE, SELL_POWER, SOLAR_SELL, TOU, *POWERS, *SOCS, *SOURCES)  # every register we may change, put back on hand back
CONTROL = ((CHARGE_A, 2), (GRID_CHARGE_A, 3), (WORK_MODE, 5), (POWERS[0], 24))  # the blocks that hold them
ZERO_EXPORT_CT, EXPORT_FIRST, TOU_WEEK = 2, 0, 0x00FF
RATED = 0x0014  # the inverter's rated AC power, 0.1 W in two words, low word first
ENERGY = (0x0202, 0x0218 - 0x0202)  # the battery charge and discharge, the grid import and export and the production, today and in total
BMS = (0x00D4, 0x00DC - 0x00D4)  # the BMS: its charge and discharge current, then its max charge and discharge current (0x00DA, 0x00DB)
SLOW = ((TEMP, 1), (WORK_MODE, 1), (CHARGE_A, 2), (RATED, 2), ENERGY, BMS)  # temperature, work mode, its own maximum charge and discharge current, its rated power, the energy counters, the BMS current limits

# the device type, as ha-solarman tells the families apart: only the three-phase hybrids of the deye_p3 profile are read and steered
DEVICE = 0x0000
TYPES = {0x0005: "LV", 0x0500: "LV", 0x0006: "HV", 0x0007: "HV", 0x0008: "HV", 0x0600: "HV", 0x0601: "HV"}

# the packs behind the BMS: their number, then one block of registers each from its serial on
PACKS, PACK, PACK_SIZE = 0x2712, 0x2730, 0x26
LV_NOMINAL_V = 51.2  # an LV pack's nominal voltage: its Ah times this is its capacity


# the grid meter of a Deye as its own device: chosen as P1 it carries the grid entities only
class DeyeMeter(EntityDevice):
    def entity_create(self) -> None:
        # the grid total and its phases
        HaSensor(self, "grid_power", uom="W", device_class="power", state_class="measurement")
        for key, _address in GRID:
            HaSensor(self, key, uom="W", device_class="power", state_class="measurement")

        # the voltages and frequency at the grid port, and the import and export the inverter counts
        for n in (1, 2, 3):
            HaSensor(self, f"voltage_l{n}", uom="V", device_class="voltage", state_class="measurement", precision=1)
        HaSensor(self, "frequency", uom="Hz", device_class="frequency", state_class="measurement", precision=2)
        for key in ("import", "export"):
            HaSensor(self, f"{key}_today", uom="kWh", device_class="energy", state_class="total_increasing", precision=1)
            HaSensor(self, f"{key}_total", uom="kWh", device_class="energy", state_class="total_increasing", precision=1)


class DeyeInverter(DistributionDevice):
    """A Deye with its battery behind one logger stick; it regulates itself until we steer it through its settings: EXTERNAL when it may be steered, else CONTROLLER."""

    CONFIG = [
        *IDENTITY,
        ConfigField("sn", {"text": {}}, cast=str, readonly=True),
        ConfigField("ip", {"text": {}}, cast=str),
        ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False),
        ConfigField("allow_control", {"boolean": {}}, cast=bool),
        ConfigField("kwh", {"number": {"step": 0.1, "unit_of_measurement": "kWh"}}, cast=float, readonly=True),
        *(ConfigField(k, {"entity": {}}, readonly=True) for k in ("soc", "battery_voltage", "battery_input", "battery_output", "pv_power", "temp", "work_mode", "home_input", "home_output", "home_input_aggr", "home_output_aggr", "solar")),
    ]

    def __init__(self, hass: HomeAssistant, driver: DeyeDriver, subentry_id: str | None, name: str, data: dict[str, Any], product: Product) -> None:
        self.driver = driver
        self.sn: str = str(data["sn"])  # the identity: the serial printed on the logger stick, letters allowed
        self.model: str = data.get("product") or "LV"  # the battery type: LV or HV, read from the inverter at its first poll
        self.product = product
        self.ip: str = data["ip"]
        self.port = int(data.get("port") or PORT)
        self.allow_control: bool = bool(data.get("allow_control"))  # off by default: the control is not confirmed on a device

        # the registry entry first: the entities find their device there; the power profile follows from its own maximum currents at the first poll
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=f"Hybrid ({self.model})")
        DistributionDevice.__init__(self, hass, self.sn, [ProfileDischarge(0), ProfileCharge(0)], name, subentry_id)
        self.kwhsensor.update_value(float(data.get("kwh") or 0))  # an LV battery gets it from its packs at the first slow poll

        # the grid meter as its own device in the same subentry, so it goes with it
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, f"{self.sn}_meter")}, name=f"{name}_meter", manufacturer=driver.manufacturer, model="Meter")
        self.meter = DeyeMeter(hass, f"{self.sn}_meter", f"{name}_meter", subentry_id)

        # 502 is a stick with plain Modbus TCP, any other port a V5 stick; a printed serial with letters is no V5 number: 0 lets the stick tell its own in its first answer
        self.logger = ModbusTcp(self.ip, self.port) if self.port == MODBUS_PORT else Logger(self.ip, int(self.sn) if self.sn.isdigit() else 0, self.port)
        self._silent = 0
        self._polls = 0
        self.supported: bool | None = None  # the device type, read once: None until then
        self.packs: dict[str, DeyeBattery] = {}  # the packs behind the BMS, by their serial

        # the control: the inverter's own settings while we steer it (in the subentry, so a crash keeps them), what we wrote, the request and the last one sent
        self.battery_voltage = 0.0  # V, the entity writes it: a grid charge current is W over this
        self.saved: dict[int, int] = {int(address): value for address, value in data.get("saved", {}).items()}
        self.written: dict[int, int] = {}
        self.refused: set[int] = set()  # registers this inverter refuses to write: they keep its own value, a hand back does not wait for them
        self.request: tuple[int, DistributionMode] | None = None
        self.sent: tuple[int, DistributionMode] | None = None
        self.stopped = False  # set on unload: the loop ends even when a request swallows the cancel
        self.device_task = asyncio.get_running_loop().create_task(self.device_loop())

    def unload(self) -> None:
        # the loop stops; an inverter under our control is handed back on its way out, then the connection closes
        self.stopped = True
        self.device_task.cancel()
        self.hass.async_create_task(self.close())

    async def close(self) -> None:
        try:
            # the loop may still be inside a send: wait until it has stopped, so it cannot write over the hand back
            await asyncio.wait({self.device_task}, timeout=STOP_S)
            if self.saved:
                await self.restore()
        finally:
            self.logger.close()

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "ip": self.ip, "port": self.port, "kwh": self.kwh, "saved": self.saved, "allow_control": self.allow_control}

    def power(self, watts: int, mode: DistributionMode) -> int:
        watts = self.clamp(watts)
        # the loop sends a request once; the same one every tick changes nothing
        if self.state != DeviceState.OFFLINE:
            self.request = (watts, mode)
            self.power_setpoint = watts if mode in (DistributionMode.INDIVIDUAL, DistributionMode.MANUAL) else 0
            self.setpoint.update_value(self.power_setpoint)
        return self.power_actual

    def power_off(self) -> None:
        # hand back: without a request the loop puts the inverter's own settings back
        self.request = None
        self.power_setpoint = 0
        self.setpoint.update_value(0)
        if self.state != DeviceState.OFFLINE:
            self.set_state(DeviceState.EXTERNAL if self.allow_control else DeviceState.CONTROLLER)  # one we may steer stays in the plan

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement")
        HaSensor(self, "battery_voltage", uom="V", device_class="voltage", state_class="measurement", precision=1)
        # the panel reads every battery on the same keys: the battery power is this inverter's home side, its PV its solar
        HaSensor(self, "battery_input", "home_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_output", "home_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        # what ZenMaster asks the inverter, so the command side is visible next to what it does
        self.setpoint = HaSensor(self, "power_setpoint", uom="W", device_class="power", state_class="measurement")
        self.setpoint.update_value(0)  # no command sent yet
        HaSensor(self, "pv_power", "solar", uom="W", device_class="power", state_class="measurement")
        self.entities["home_input_aggr"], self.entities["home_output_aggr"] = self.entities["battery_input_aggr"], self.entities["battery_output_aggr"]
        HaSensor(self, "temp", uom="°C", device_class="temperature", state_class="measurement", precision=1)
        # the BMS current limits, so a cap on what the battery delivers shows next to what we ask
        for key in ("bms_charge_a", "bms_discharge_a", "bms_max_charge_a", "bms_max_discharge_a"):
            HaSensor(self, key, uom="A", device_class="current", state_class="measurement")
        HaSensor(self, "work_mode")
        self.kwhsensor = HaSensor(self, "kwh", uom="kWh", device_class="energy", restore=True)
        HaSensor(self, "pv_today", uom="kWh", device_class="energy", state_class="total_increasing", precision=1)
        HaSensor(self, "pv_total", uom="kWh", device_class="energy", state_class="total_increasing", precision=1)
        HaSensor(self, "battery_charge_total", uom="kWh", device_class="energy", state_class="total_increasing", precision=1)
        HaSensor(self, "battery_discharge_total", uom="kWh", device_class="energy", state_class="total_increasing", precision=1)

    async def device_loop(self) -> None:
        while not self.stopped:
            try:
                # hand back or a new request first, then the poll; a Deye of another family is never written to
                if self.request is None and self.saved:
                    await self.restore()
                elif self.request not in (None, self.sent) and self.supported:
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
                self.saved = await self.read_control()
                self.config_changed()

            # per mode what it wants: a setpoint through the battery currents (the TOU power is no hard limit), at rest both currents closed, SMART_* on its own CT with one current closed
            setpoint = mode in (DistributionMode.INDIVIDUAL, DistributionMode.MANUAL)
            charge, discharge = self.saved[CHARGE_A], self.saved[DISCHARGE_A]  # open: the inverter's own limits
            if setpoint and watts:
                # the setpoint as a current at the battery voltage: discharging exactly that with Export First, what the house does not take goes to the grid;
                # or charging that much from the grid up to soc_max on its own CT
                # at least MIN_W, so a start at RANGE_MIN shows in the battery power and the distribution counts it as running
                amps = max(round(MIN_W / self.battery_voltage), round(abs(watts) / self.battery_voltage)) if self.battery_voltage else 0
                wanted = {
                    WORK_MODE: EXPORT_FIRST if watts > 0 else ZERO_EXPORT_CT, TOU: TOU_WEEK, GRID_CHARGE: int(watts < 0), GRID_CHARGE_A: amps if watts < 0 else 0,
                    CHARGE_A: charge, DISCHARGE_A: amps if watts > 0 else 0, SELL_POWER: max(self.saved[SELL_POWER], max(0, watts) // self.product.power_scale),
                }
                wanted |= (
                    {a: max(self.saved[a], abs(watts) // self.product.power_scale) for a in POWERS}  # the active program's power limits too: never under the setpoint
                    | dict.fromkeys(SOCS, int(self.soc_min if watts > 0 else self.soc_max))
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

            # only what differs from what the inverter holds goes to it
            held = await self.read_control()
            for address, value in wanted.items():
                if held[address] != value and address not in self.refused:
                    await self.write(address, value)

            # what the inverter holds now next to what we asked, so a test compares both with what it does
            held = await self.read_control()
            _LOGGER.info("Deye %s: %s %d W, asked %s, holds %s", self.name, mode.name, watts, wanted, {a: held[a] for a in wanted})
            # a setting it does not hold yet is sent again next round
            if missing := [a for a in wanted if a not in self.refused and held[a] != wanted[a]]:
                _LOGGER.warning("Deye %s: not held yet %s, again next round", self.name, ", ".join(f"0x{a:04X}" for a in missing))
                return
        except OSError as err:
            _LOGGER.warning("Deye %s: settings not written (%s)", self.name, err)
            return

        # sent: a setpoint or rest is ours, SMART_* regulate themselves on our settings
        self.sent = (watts, mode)
        self.set_state(DeviceState.ACTIVE if setpoint or mode == DistributionMode.OFF else DeviceState.EXTERNAL)

    async def restore(self) -> None:
        # hand back: every register we may have changed gets the inverter's own value again; only a read back that shows them all clears what we saved,
        # else the next round tries again, so a new control can never take our own values for the inverter's
        try:
            for address in WRITES:
                await self.write(address, self.saved[address])
            back = await self.read_control()
        except OSError as err:
            _LOGGER.warning("Deye %s: hand back failed (%s)", self.name, err)
            return
        if differ := [a for a in WRITES if a not in self.refused and back[a] != self.saved[a]]:
            _LOGGER.warning("Deye %s: hand back not taken yet for %s, again next round", self.name, ", ".join(f"0x{a:04X}" for a in differ))
            return
        self.saved, self.written, self.sent = {}, {}, None
        self.config_changed()

    async def read_control(self) -> dict[int, int]:
        # the blocks that hold every register we may change
        values: dict[int, int] = {}
        for start, count in CONTROL:
            values.update(zip(range(start, start + count), await self.logger.read(start, count)))
        return values

    async def write(self, address: int, value: int) -> None:
        # one setting; a register the inverter refuses is logged and left, the rest still goes
        try:
            await self.logger.write(address, value)
        except Refused as err:
            self.refused.add(address)
            _LOGGER.warning("Deye %s: register 0x%04X refused (%s)", self.name, address, err)

    async def poll(self) -> None:
        # the fast set every round in two reads; temperature, work mode, counters and packs now and then
        self._polls += 1
        try:
            # the device type once: a Deye of another family has other registers and is left alone
            if self.supported is None:
                self.check_type((await self.logger.read(DEVICE, 1))[0])
            if not self.supported:
                return
            values: dict[int, int] = {}
            for start, count in FAST + (SLOW if self._polls % SLOW_EVERY == 1 else ()):
                values.update(zip(range(start, start + count), await self.logger.read(start, count)))
            if self._polls % SLOW_EVERY == 1:
                await self.poll_packs()
        except OSError as err:
            self._silent += 1
            _LOGGER.debug("Deye %s: poll failed (%s)", self.name, err)
            if self._silent >= OFFLINE_POLLS and self.state != DeviceState.OFFLINE:
                # offline: what it has is no longer known, back online the request goes out again
                self.power_actual, self.sent = 0, None
                self.set_state(DeviceState.OFFLINE)
            return
        self._silent = 0
        self.update_status(values)
        self.update_meter(values)

    def check_type(self, kind: int) -> None:
        # LV or HV as ha-solarman tells them apart; another family regulates itself, out of every plan, with one notification
        self.supported = (model := TYPES.get(kind)) is not None
        if not self.supported:
            self.set_state(DeviceState.CONTROLLER)
            persistent_notification.async_create(self.hass, f"Deye {self.name}: device type 0x{kind:04X} is not supported, only the three-phase hybrids", PANEL_TITLE, f"{DOMAIN}_deye_{self.sn}")
        elif model != self.model:
            self.model, self.product = model, self.driver.PRODUCTS[model]
            self.config_changed()

    async def poll_packs(self) -> None:
        # every pack behind the BMS: their number, then one block each; a pack seen for the first time becomes its own device
        for n in range((await self.logger.read(PACKS, 1))[0]):
            r = await self.logger.read(PACK + n * PACK_SIZE, PACK_SIZE)
            if sn := text(r[:8]):
                (self.packs.get(sn) or self.packs.setdefault(sn, DeyeBattery(self.hass, self, sn))).update(r)

        # the capacity of an LV battery from its packs; an HV battery keeps the kWh it was added with
        if self.model == "LV" and self.packs and (kwh := round(sum(p.ah for p in self.packs.values()) * LV_NOMINAL_V / 1000, 2)) != self.kwh:
            self.kwhsensor.update_value(kwh)
            self.config_changed()
            if self.on_invalidate is not None:
                self.on_invalidate()

    def update_meter(self, r: dict[int, int]) -> None:
        # the voltages and frequency at the grid port, inside the fast block
        for n, address in enumerate((0x0256, 0x0257, 0x0258), 1):
            self.meter.entity_update(f"voltage_l{n}", r[address] / 10)
        self.meter.entity_update("frequency", r[0x0261] / 100)

        # the counters the inverter keeps: today in one word, a total in two words low word first, all in 0.1 kWh
        if ENERGY[0] in r:
            total = lambda a: (r[a + 1] << 16 | r[a]) / 10
            self.meter.entity_update("import_today", r[0x0208] / 10)
            self.meter.entity_update("export_today", r[0x0209] / 10)
            self.meter.entity_update("import_total", total(0x020A))
            self.meter.entity_update("export_total", total(0x020C))
            self.entity_update("pv_today", r[0x0211] / 10)
            self.entity_update("pv_total", total(0x0216))
            self.entity_update("battery_charge_total", total(0x0204))
            self.entity_update("battery_discharge_total", total(0x0206))

    def update_status(self, r: dict[int, int]) -> None:
        # the battery; its power positive for discharge, as ha-solarman's battery state reads it
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
            for key, address in (("bms_charge_a", 0x00D4), ("bms_discharge_a", 0x00D5), ("bms_max_charge_a", 0x00DA), ("bms_max_discharge_a", 0x00DB)):
                self.entity_update(key, r[address])

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

        # it regulates itself while we do not steer it; one we may steer counts in the plan as external, so a fleet of Deyes alone can start
        self.power_actual = battery
        if not self.saved:
            self.set_state(DeviceState.EXTERNAL if self.allow_control else DeviceState.CONTROLLER)
