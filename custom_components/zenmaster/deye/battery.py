"""DeyeBattery: one battery pack behind the BMS of a Deye, its own device under the inverter."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from ..const import DOMAIN
from ..entity import EntityDevice, via_device
from ..modbus import decode
from ..sensor import HaSensor


class DeyeBattery(EntityDevice):
    def __init__(self, hass: HomeAssistant, inverter: Any, sn: str) -> None:
        # registered under the inverter and in its subentry, so it goes with it; the pack serial is its identity
        self.ah = 0.0  # its capacity, the inverter sums them into its kWh
        name = f"{inverter.name}_{sn[-4:]}"
        dr.async_get(hass).async_get_or_create(config_entry_id=inverter.driver.entry_id, config_subentry_id=inverter.subentry_id, identifiers={(DOMAIN, sn)}, name=name, manufacturer="Deye", model="Battery", **via_device(hass, inverter.sn))
        super().__init__(hass, sn, name, inverter.subentry_id)

    def entity_create(self) -> None:
        HaSensor(self, "soc", uom="%", device_class="battery", state_class="measurement", precision=1)
        HaSensor(self, "voltage", uom="V", device_class="voltage", state_class="measurement", precision=1)
        HaSensor(self, "current", uom="A", device_class="current", state_class="measurement", precision=1)
        HaSensor(self, "temp", uom="°C", device_class="temperature", state_class="measurement", precision=1)
        HaSensor(self, "soh", uom="%", state_class="measurement", precision=1)
        HaSensor(self, "capacity", uom="Ah", state_class="measurement")
        HaSensor(self, "cycles", state_class="total_increasing")
        HaSensor(self, "cell_max", uom="V", device_class="voltage", state_class="measurement", precision=3)
        HaSensor(self, "cell_min", uom="V", device_class="voltage", state_class="measurement", precision=3)

    def update(self, r: list[int]) -> None:
        # one pack block, offsets from its serial: tenths, a signed current, millivolt cells
        self.ah = r[0x12] / 10
        self.entity_update("voltage", r[0x08] / 10)
        self.entity_update("current", decode([r[0x09]], "int16") / 10)
        self.entity_update("temp", r[0x0A] / 10)
        self.entity_update("soc", r[0x0F] / 10)
        self.entity_update("soh", r[0x10] / 10)
        self.entity_update("capacity", self.ah)
        self.entity_update("cycles", r[0x18])
        self.entity_update("cell_max", r[0x16] / 1000)
        self.entity_update("cell_min", r[0x17] / 1000)
