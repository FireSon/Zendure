"""Base entity and device classes for the Em integration."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def snakecase(value: str) -> str:
    """Convert to snake_case with only HA-valid chars (a-z, 0-9, _)."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").replace("+", "plus")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^a-z0-9]", "_", value.lower())
    return re.sub(r"_+", "_", value).strip("_")


class EmEntity(Entity):
    """Base for all Em entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    Empty: EmEntity  # sentinel — assigned at module load below

    def __init__(self, id: str) -> None:
        self.id = id
        self._state: Any = None

    def register(self, device: EmDevice, key: str) -> None:
        """Register to a device."""
        self.device = device
        self.key = key
        self._attr_unique_id = snakecase(f"{device.device_id}_{key}")
        self._attr_translation_key = snakecase(key)
        device.entities[self.id] = self

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info, or None if unbound (e.g. the Empty sentinel)."""
        return self.device.device_info if self.device is not None else None

    def update_value(self, value: Any) -> bool:
        """Update entity state. Called when new data arrives."""
        try:
            if not self.set_state(value):
                return False
            if self.hass and self.hass.loop.is_running():
                self.schedule_update_ha_state()
            return True
        except Exception as err:
            _LOGGER.error("Error updating %s: %s", self._attr_unique_id, err)
            return False

    def set_state(self, value: Any) -> bool:
        """Convert, compare, assign. Return True if changed. Override per platform."""
        return False

    def restore_state(self, value: Any) -> bool:
        """Apply a persisted state; platforms whose set_state converts device units override this."""
        return self.set_state(value)

    @property
    def asNumber(self):
        """Current state as a number, 0 when unset/non-numeric. Subclasses override
        with their own conversion; this base default keeps Empty/unset slots safe to read."""
        return self._state if isinstance(self._state, (int, float)) else 0

    @property
    def asInt(self) -> int:
        """Return current value as int, 0 if None."""
        return int(self._state) if isinstance(self._state, (int, float)) else 0

    async def _invoke(self, callback: Callable | None, *args: Any) -> None:
        """Invoke sync or async callback."""
        if callback is not None:
            if asyncio.iscoroutinefunction(callback):
                await callback(*args)
            else:
                callback(*args)


# Shared sentinel — DistributionDevice defaults every entity slot to this.
EmEntity.Empty = EmEntity("empty")


class EmDevice:
    """Base device that manages entities and state persistence."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        name: str,
        model: str = "",
        manufacturer: str = "",
        parent: str | None = None,
    ) -> None:
        """Initialize a Em device."""
        self.hass = hass
        self.device_id = device_id
        self.name = name
        self.entities: dict[str, EmEntity] = {}
        self._restore: list[str] = []
        self._restored: dict[str, Any] = {}
        self._store = Store(hass, version=1, key=f"{DOMAIN}.{device_id}", atomic_writes=True)

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=name,
            manufacturer=manufacturer,
            model=model,
        )
        if parent is not None:
            self.device_info["via_device"] = (DOMAIN, parent)

        if self.name:
            self.device_entities()
            for key, value in list(self.__dict__.items()):
                if isinstance(value, EmEntity) and value is not EmEntity.Empty:
                    value.register(self, key)

    def device_entities(self) -> None:
        """Override to declare entities: self.foo = EmXxx(...)."""

    async def load_state(self) -> None:
        self._restored = await self._store.async_load() or {}
        for key, value in self._restored.items():
            entity = getattr(self, key, None)
            if isinstance(entity, EmEntity):
                entity.restore_state(value)
            elif key in self._restore:
                setattr(self, key, value)  # plain restorable attribuut (bijv. _wear_snapshots)

    async def save_state(self) -> None:
        """Persist restorable entity states to Store."""
        data = {}
        for key in self._restore:
            entity = getattr(self, key, None)
            if isinstance(entity, EmEntity):
                data[key] = entity._state
            elif entity is not None:
                data[key] = entity  # plain restorable attribuut (bijv. _wear_snapshots)
        if data:
            await self._store.async_save(data)

    def entity_update(self, key: str, value: Any) -> bool:
        """Route incoming data to the correct entity."""
        if entity := self.entities.get(key):
            return entity.update_value(value)
        return False

    def entity_write(self, entity: EmEntity, value: Any) -> None:
        """Write value to hardware. Override in device subclasses."""
