"""Number platform for the Em integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
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
    """Set up Em numbers."""
    EmNumber.add = async_add_entities


class EmNumber(NumberEntity, EmEntity):
    """Em number entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        id: str,
        *,
        uom: str | None = None,
        factor: float = 1.0,
        minimum: float = 0,
        maximum: float = 2000,
        mode: NumberMode = NumberMode.AUTO,
        on_write: Callable | None = None,
        restore: bool = False,
    ) -> None:
        """Store config; binding happens in register() once the attribute name is known."""
        super().__init__(id)
        self._cfg_uom = uom
        self.factor = factor
        self._cfg_minimum = minimum
        self._cfg_maximum = maximum
        self._cfg_mode = mode
        self._on_write = on_write
        self._cfg_restore = restore

    def register(self, device: EmDevice, key: str) -> None:
        super().register(device, key)
        self.entity_description = NumberEntityDescription(
            key=key,
            name=key,
            native_unit_of_measurement=self._cfg_uom,
        )
        self._attr_native_min_value = self._cfg_minimum
        self._attr_native_max_value = self._cfg_maximum
        self._attr_mode = self._cfg_mode
        if self._cfg_restore:
            device._restore.append(key)
        self.add([self])

    def set_state(self, value: Any) -> bool:
        value = int(float(value) * self.factor)
        if self._state == value:
            return False
        self._state = value
        self._attr_native_value = value
        return True

    async def async_set_native_value(self, value: float) -> None:
        """Handle value set from UI.

        Forwards the new value to the on_write hook (which typically publishes
        to the device via MQTT) and immediately updates the local entity state.
        Optimistic update — soc_min/soc_max and similar knobs aren't echoed
        back by the device, so without this the UI never reflects the change.
        """
        await self._invoke(self._on_write, self, int(value / self.factor))
        self.update_value(value / self.factor)
        if self._cfg_restore:
            await self.device.save_state()

    def restore_state(self, value: Any) -> bool:
        """Persisted value already has the factor applied; assign directly."""
        self._state = value
        self._attr_native_value = value
        return True

    @property
    def asNumber(self) -> int | float:
        """Return current value as number, 0 if None. factor is already applied in
        _set_state, so return _state directly (do not apply factor twice)."""
        return self._state if isinstance(self._state, (int, float)) else 0

    def update_range(self, minimum: float, maximum: float) -> None:
        """Update min/max range."""
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        if self.hass and self.hass.loop.is_running():
            self.schedule_update_ha_state()
