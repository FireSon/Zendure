"""Switch platform for the Em integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
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
    """Set up Em switches."""
    EmSwitch.add = async_add_entities


class EmSwitch(SwitchEntity, EmEntity):
    """Em switch entity."""

    add: AddEntitiesCallback

    def __init__(self, id: str, on_changed: Callable | None = None, restore: bool = False, default: bool = False) -> None:
        """Store config; binding happens in register() once the attribute name is known."""
        super().__init__(id)
        self._on_changed = on_changed
        self._cfg_restore = restore
        self._state = default
        self._attr_is_on = default

    def register(self, device: EmDevice, key: str) -> None:
        super().register(device, key)
        self.entity_description = SwitchEntityDescription(key=key, name=key)
        if self._cfg_restore:
            device._restore.append(key)
        self.add([self])

    def set_state(self, value: Any) -> bool:
        v = value if isinstance(value, bool) else str(value).lower() in ("on", "true", "1")
        if self._state == v:
            return False
        self._state = v
        self._attr_is_on = v
        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply(False)

    async def _apply(self, on: bool) -> None:
        self._state = on
        self._attr_is_on = on
        await self._invoke(self._on_changed, self, on)
        self.async_write_ha_state()
        if self._cfg_restore:
            await self.device.save_state()

    @property
    def asNumber(self) -> int:
        return 1 if self._state else 0
