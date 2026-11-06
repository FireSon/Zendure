"""The HA face of a consumer: its status sensor, a button per row action, and every setting as a config entity."""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial

from homeassistant.const import EntityCategory

from .button import HaButton
from .entity import EntityDevice
from .number import HaNumber
from .select import HaSelect
from .sensor import HaSensor
from .switch import HaSwitch
from .time import HaTime


class ConsumerDevice(EntityDevice):
    # Every field without a cast is a setting that lives as an entity on the consumer's device: the device Store keeps
    # its last value, the attribute follows the entity, and the coordinator hears of a change through on_setting.
    on_setting: Callable | None = None  # coordinator hook, called with the consumer: a setting changed, the LP needs a rebuild
    on_action: Callable | None = None  # coordinator hook, awaited with (consumer, action id): a row button was pressed
    _meter = (0.0, 0.0)  # (last power in W, its timestamp) for the energy integration
    _pending_kwh = 0.0  # energy measured since the last quarter settlement

    def on_power(self, watts: float, now: float | None = None) -> None:
        # integrate the previous reading into this quarter's energy: W over seconds -> kWh
        now = now if now is not None else time.time()
        last, stamp = self._meter
        if stamp and now > stamp:
            self._pending_kwh += last * (now - stamp) / 3_600_000.0
        self._meter = (watts, now)

    def entity_create(self) -> None:
        HaSensor(self, "state")
        self.entity_update("state", self.status.name)
        for action in type(self).ACTIONS:
            HaButton(self, action.id, press=partial(self._press, action.id))
        for f in type(self).CONFIG:
            if f.cast is not None or f.readonly or f.hidden or f.id == "name" or (kind := next((k for k in ("boolean", "number", "select", "time") if k in f.selector), None)) is None:
                continue
            spec = f.selector[kind]
            if kind == "boolean":
                entity = HaSwitch(self, f.id, on_changed=self._setting, restore=True)
            elif kind == "number":
                entity = HaNumber(self, f.id, uom=spec.get("unit_of_measurement"), minimum=spec.get("min", -1000.0), maximum=spec.get("max", 100000.0), step=spec.get("step", 1.0), restore=True)
            elif kind == "select":
                keys = int if isinstance(getattr(self, f.id, None), int) else str  # the attribute's type says how an option value reads
                entity = HaSelect(self, f.id, {keys(o["value"]): o["label"] for o in spec["options"]}, on_changed=self._setting, restore=True)
            else:
                entity = HaTime(self, f.id, on_changed=self._setting, restore=True)
            entity._attr_entity_category = EntityCategory.CONFIG
            entity.update_value(getattr(self, f.id))  # the field default until device_load hands it the stored value

    def entity_write(self, entity, value) -> None:
        # the number platform's write path; it runs before update_value moves the attribute, and the rebuild waits a second, so the new value is in by then
        self._setting(entity, value)

    def _setting(self, _entity, _value) -> None:
        if self.on_setting is not None:
            self.on_setting(self)

    async def _press(self, action: str, _entity) -> None:
        if self.on_action is not None:
            await self.on_action(self, action)
