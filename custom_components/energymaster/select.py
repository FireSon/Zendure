"""Select platform for the Em integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
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
    """Set up Em selects."""
    EmSelect.add = async_add_entities


class EmSelect(SelectEntity, EmEntity):
    """Em select entity."""

    add: AddEntitiesCallback

    def __init__(
        self,
        id: str,
        options: dict[Any, str],
        on_changed: Callable | None = None,
        restore: bool = False,
    ) -> None:
        """Store config; binding happens in register() once the attribute name is known."""
        super().__init__(id)
        self._options = options
        self._attr_options = list(options.values())
        self._attr_current_option = self._attr_options[0]
        self._state = next(iter(options))
        self._on_changed = on_changed
        self._cfg_restore = restore

    def register(self, device: EmDevice, key: str) -> None:
        super().register(device, key)
        self.entity_description = SelectEntityDescription(key=key, name=key)
        if self._cfg_restore:
            device._restore.append(key)
        self.add([self])

    def set_state(self, value: Any) -> bool:
        option = self._options.get(value) if self._options else None
        if option is None or self._state == value:
            return False
        self._state = value
        self._attr_current_option = option
        return True

    async def async_select_option(self, option: str) -> None:
        """Handle option selected from UI."""
        self._attr_current_option = option
        self._state = self.value
        await self._invoke(self._on_changed, self, self._state)
        self.async_write_ha_state()
        if self._cfg_restore:
            await self.device.save_state()

    @property
    def value(self) -> Any:
        """Reverse lookup: current option string -> original key."""
        if self._options is not None:
            for key, val in self._options.items():
                if val == self._attr_current_option:
                    return key
        return None
