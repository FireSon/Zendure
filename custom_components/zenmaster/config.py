from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .device import DistributionDevice

if TYPE_CHECKING:
    from homeassistant.helpers.storage import Store


@dataclass
class ConfigField:
    id: str
    selector: dict  # HA selector spec, rendered by <ha-selector>
    cast: Callable | None = None  # deserialize cast; None = not persisted
    is_key: bool = False
    required: bool = True
    readonly: bool = False
    expert: bool = False
    transient: bool = False  # a one-shot input (e.g. a login code): shown, never stored, never read back
    hidden: bool = False  # sent to the card but never drawn; only there to drive another field's visible_when
    visible_when: dict | None = None  # {field_id: value or [values]} -> field shown only when it matches
    section_key: str | None = None  # display grouping in the UI card; a technical key, the UI translates it


# what every fleet device shows: its identity, and its battery; a brand puts its own fields around them
IDENTITY = [ConfigField("device_id", {"text": {}}, cast=str, is_key=True, readonly=True), ConfigField("name", {"text": {}}, cast=str, readonly=True), ConfigField("model", {"text": {}}, cast=str, readonly=True)]
FLEET = [ConfigField("kwh", {"number": {"step": 0.1, "unit_of_measurement": "kWh"}}, cast=float, readonly=True), *(ConfigField(k, {"entity": {}}, readonly=True) for k in ("soc", "soc_min", "soc_max", "home_input", "home_output", "home_input_aggr", "home_output_aggr", "rte", "power_phase"))]


def fields_of(obj: Any) -> list[ConfigField] | None:
    # every class carries its own fields, a subclass those of its parent
    return getattr(type(obj), "CONFIG", None)


class Config:
    def __init__(self, store: Store | None = None) -> None:
        self.payload: dict[str, Any] = {}
        self._store = store

    async def load(self) -> None:
        self.payload = (await self._store.async_load() if self._store else {}) or {}

    async def save(self) -> None:
        if self._store is not None:
            await self._store.async_save(self.payload)

    def section(self, group: str, name: str) -> dict[str, Any]:
        return self.payload.setdefault(group, {}).setdefault(name, {})

    @staticmethod
    def _apply(obj: Any, fields: list[ConfigField], payload: dict) -> None:
        # every persisted field the payload holds goes onto the object, through its cast
        for field in fields:
            if field.cast is not None and not field.transient and field.id in payload:
                setattr(obj, field.id, field.cast(payload[field.id]))

    def deserialize(self, obj: Any, fields: list[ConfigField]) -> None:
        # get the key
        if not (fid := next((f for f in fields if f.is_key), None)) or not (id := getattr(obj, fid.id, None)):
            return

        # get the payload
        if not (payload := self.payload.get(id)):
            return
        self._apply(obj, fields, payload)

    def deserialize_poly(self, objects: list[Any], payload: dict) -> Any:
        # find the member whose key field matches the payload, then update its attributes
        for obj in objects:
            fields = fields_of(obj)
            if fields is None or not (fid := next((f for f in fields if f.is_key), None)):
                continue
            if payload.get(fid.id) != getattr(obj, fid.id, None):
                continue
            self._apply(obj, fields, payload)
            return obj
        return None

    def serialize_poly(self, obj: Any, config: dict, schema: dict) -> None:
        # one member of a polymorphic collection, under its own key field: its fields for the UI, its values for the payload
        if (fields := fields_of(obj)) is None:
            return
        if not (fid := next((f for f in fields if f.is_key), None)) or not (name := getattr(obj, fid.id, None)):
            return
        schema[name] = [{k: v for k, v in asdict(f).items() if k != "cast"} for f in fields]
        self.serialize(obj, name, config)
        config[name]["kind"] = type(obj).__name__  # the UI picks its card on this
        config[name]["fleet"] = isinstance(obj, DistributionDevice)  # the UI groups the battery fleet on this, not the class name
        config[name]["subentry_id"] = getattr(obj, "subentry_id", None)  # a device is edited and removed through its subentry

    def serialize(self, obj: Any, name: str | None = None, payload: dict | None = None) -> None:
        fields = fields_of(obj)
        if fields is None:
            return
        if payload is None:
            payload = self.payload
        data = payload if name is None else payload.setdefault(name, {})
        for field in fields:
            if field.transient:
                continue
            if field.cast is None:
                # an entity field: the value is the entity_id that HA assigned
                if (entity := getattr(obj, "entities", {}).get(field.id)) is not None and (eid := getattr(entity, "entity_id", None)):
                    data[field.id] = eid
                continue
            value = getattr(obj, field.id, None)
            if value is None:
                continue
            data[field.id] = value
