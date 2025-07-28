"""Interfaces with the Zendure Integration binairy sensors."""

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template

from .entity import EntityDevice, EntityZendure

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(_hass: HomeAssistant, _config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the Zendure binary_sensor."""
    ZendureBinarySensor.add = async_add_entities


class ZendureBinarySensor(EntityZendure, BinarySensorEntity):
    add: AddEntitiesCallback

    def __init__(
        self,
        device: EntityDevice,
        uniqueid: str,
        template: Template | None = None,
        deviceclass: Any | None = None,
    ) -> None:
        """Initialize a binary sensor entity."""
        super().__init__(device, uniqueid, "binary_sensor")
        self.entity_description = BinarySensorEntityDescription(key=uniqueid, name=uniqueid, device_class=deviceclass)
        self._attr_is_on = False
        self._value_template: Template | None = template
        device.call_threadsafe(self.add, [self])

    def update_value(self, value: Any) -> bool:
        try:
            is_on = bool(
                int(self._value_template.async_render_with_possible_json_value(value, None)) != 0 if self._value_template is not None else int(value) != 0
            )

            if self._attr_is_on == is_on:
                return False

            _LOGGER.info(f"Update binary_sensor: {self._attr_unique_id} => {is_on}")

            self._attr_is_on = is_on
            if self.hass and self.hass.loop.is_running():
                self.schedule_update_ha_state()
        except Exception as err:
            _LOGGER.error(f"Error {err} setting state: {self._attr_unique_id} => {value}")

        return True
