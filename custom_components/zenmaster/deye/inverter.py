"""DeyeInverter: a Deye three-phase hybrid inverter read through its Solarman logger stick; read only until the control step."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ..config import IDENTITY, ConfigField
from ..const import DOMAIN
from ..device import DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from ..modbus import decode
from ..sensor import HaSensor
from .logger import PORT, Logger

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
SLOW = ((TEMP, 1), (WORK_MODE, 1))


class DeyeInverter(DistributionDevice):
    """A Deye with its battery behind one logger stick; it regulates itself, so it reports EXTERNAL."""

    CONFIG = [
        *IDENTITY,
        ConfigField("sn", {"text": {}}, cast=str, readonly=True),
        ConfigField("ip", {"text": {}}, cast=str),
        ConfigField("port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False),
        ConfigField("kwh", {"number": {"step": 0.1, "unit_of_measurement": "kWh"}}, cast=float, readonly=True),
        *(ConfigField(k, {"entity": {}}, readonly=True) for k in ("soc", "battery_voltage", "battery_input", "battery_output", "pv_power", "phase1_power", "phase2_power", "phase3_power", "grid_power", "temp", "work_mode")),
    ]

    def __init__(self, hass: HomeAssistant, driver: DeyeDriver, subentry_id: str | None, name: str, data: dict[str, Any], product: Product) -> None:
        self.driver = driver
        self.sn: str = str(data["sn"])  # the identity: the serial of the logger stick, also in every V5 frame
        self.model: str = data["product"]  # the battery type: LV or HV
        self.product = product
        self.ip: str = data["ip"]
        self.port = int(data.get("port") or PORT)
        self.on_config_changed: Any = None  # the coordinator: persist subentry_data(); nothing is learned over the stick

        # the registry entry first: the entities find their device there; no power profile until the control step
        dr.async_get(hass).async_get_or_create(config_entry_id=driver.entry_id, config_subentry_id=subentry_id, identifiers={(DOMAIN, self.sn)}, name=name, manufacturer=driver.manufacturer, model=f"Hybrid ({self.model})")
        DistributionDevice.__init__(self, hass, self.sn, [ProfileDischarge(0), ProfileCharge(0)], name, subentry_id)
        self.kwhsensor.update_value(float(data["kwh"]))

        self.logger = Logger(self.ip, int(self.sn), self.port)
        self._silent = 0
        self._polls = 0
        self.device_task = asyncio.get_running_loop().create_task(self.device_loop())

    def unload(self) -> None:
        self.device_task.cancel()
        self.logger.close()

    def subentry_data(self) -> dict[str, Any]:
        return {"product": self.model, "sn": self.sn, "ip": self.ip, "port": self.port, "kwh": self.kwh}

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement")
        HaSensor(self, "battery_voltage", uom="V", device_class="voltage", state_class="measurement", precision=1)
        HaSensor(self, "battery_input", uom="W", device_class="power", state_class="measurement")
        HaSensor(self, "battery_output", uom="W", device_class="power", state_class="measurement")
        HaSensor(self, "pv_power", uom="W", device_class="power", state_class="measurement")
        for key, _address in GRID:
            HaSensor(self, key, uom="W", device_class="power", state_class="measurement")
        HaSensor(self, "grid_power", uom="W", device_class="power", state_class="measurement")
        HaSensor(self, "temp", uom="°C", device_class="temperature", state_class="measurement", precision=1)
        HaSensor(self, "work_mode")
        self.kwhsensor = HaSensor(self, "kwh", uom="kWh", device_class="energy", restore=True)

    async def device_loop(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception:
                # the task must never die
                _LOGGER.exception("device task %s", self.device_id)
            await asyncio.sleep(POLL_S)

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
            self.entity_update(key, watts)
        self.entity_update("grid_power", sum(phases))
        if TEMP in r:
            self.entity_update("temp", round((r[TEMP] - 1000) * 0.1, 1))
            self.entity_update("work_mode", r[WORK_MODE])

        # it regulates itself: never part of our control yet
        self.power_actual = battery
        self.set_state(DeviceState.EXTERNAL)
