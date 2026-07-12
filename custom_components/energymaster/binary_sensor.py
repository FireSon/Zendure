"""Binary sensor platform for the Em integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import EmDevice, EmEntity


async def async_setup_entry(
    _hass: HomeAssistant,
    _config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Em binary sensors."""
    EmBinarySensor.add = async_add_entities


class EmBinarySensor(BinarySensorEntity, EmEntity):
    """Em binary sensor entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        id: str,
        *,
        device_class: str | None = None,
    ) -> None:
        """Store config; binding happens in register() once the attribute name is known."""
        super().__init__(id)
        self._cfg_device_class = device_class
        self._attr_is_on = False

    def register(self, device: EmDevice, key: str) -> None:
        super().register(device, key)
        self.entity_description = BinarySensorEntityDescription(
            key=key,
            name=key,
            device_class=self._cfg_device_class,
        )
        self.add([self])

    def set_state(self, value: Any) -> bool:
        # device payloads mix numbers, numeric strings and on/true words; anything else counts as off
        try:
            value = float(value) != 0
        except (TypeError, ValueError):
            value = str(value).strip().lower() in ("on", "true", "yes")
        if self._state == value:
            return False
        self._state = value
        self._attr_is_on = value
        return True
