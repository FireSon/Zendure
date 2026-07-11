"""Sensor platform for the Em integration."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EmDevice, EmEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Em sensors."""
    EmSensor.add = async_add_entities


class EmSensor(SensorEntity, EmEntity):
    """Em sensor entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        id: str,
        *,
        uom: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        precision: int | None = None,
        icon: str | None = None,
        aggr: bool = False,
        convert: Callable[[Any], Any] | None = None,
    ) -> None:
        """Store config; binding happens in register() once the attribute name is known."""
        super().__init__(id)
        self._cfg_uom = uom
        self._cfg_device_class = device_class
        self._cfg_state_class = state_class
        self._cfg_precision = precision
        self._cfg_icon = icon
        self._cfg_aggr = aggr
        self._convert = convert
        self._aggr: EmSensor | None = None

    def register(self, device: EmDevice, key: str) -> None:
        super().register(device, key)
        self.entity_description = SensorEntityDescription(
            key=key,
            name=key,
            native_unit_of_measurement=self._cfg_uom,
            device_class=self._cfg_device_class,
            state_class=self._cfg_state_class,
            icon=self._cfg_icon,
        )
        if self._cfg_precision is not None:
            self._attr_suggested_display_precision = self._cfg_precision
        if self._cfg_aggr:
            aggr_key = f"{key}_aggr"
            aggr = EmSensor(
                id=f"{self.id}_aggr",
                uom="kWh",
                device_class="energy",
                state_class="total_increasing",
            )
            aggr._last_value = 0.0
            aggr._last_update = 0.0
            self._aggr = aggr
            setattr(device, aggr_key, aggr)
            aggr.register(device, aggr_key)
            device._restore.append(aggr_key)
        self.add([self])

    def set_state(self, value: Any) -> bool:
        if self._convert is not None:
            value = self._convert(value)
        elif not self._cfg_precision or self._cfg_precision == 0:
            value = value
        else:
            value = round(value, self._cfg_precision)
        if self._state == value:
            return False
        self._state = value
        self._attr_native_value = value
        if self._aggr is not None:
            self._aggr._aggregate(value)
        return True

    def _aggregate(self, watts: float) -> None:
        """Accumulate kWh from watts."""
        now = time.time()
        if self._last_update > 0:
            dt_h = (now - self._last_update) / 3600.0
            kwh = (self._state or 0.0) + self._last_value * dt_h / 1000.0
            self._state = kwh
            self._attr_native_value = kwh
            if self.hass and self.hass.loop.is_running():
                self.schedule_update_ha_state()
        self._last_value = watts
        self._last_update = now
