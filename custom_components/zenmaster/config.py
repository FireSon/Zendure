from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from .device import DistributionDevice
from .distribution import Distribution
from .zendure.driver import ZendureDriver
from .zendure.smartmeter import ZendureSmartMeter
from .zendure.solarflow import ZendureSolarFlow

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


# the card's view of the shared classes, which come from Zendure-HA v2 and carry no CONFIG of their own
FIELDS: dict[type, list[ConfigField]] = {
    Distribution: [
        ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True),
        ConfigField("max_kw", {"number": {"min": 0, "step": 0.1, "unit_of_measurement": "kW"}}, cast=float),
        ConfigField("max_output", {"number": {"min": 0, "step": 100, "unit_of_measurement": "W"}}, cast=int, required=False),
        ConfigField("p1_device", {"device": {}}, cast=str, required=False),
        ConfigField("mode", {"entity": {}}, readonly=True),
        ConfigField("manual_power", {"entity": {}}, readonly=True),
        ConfigField("setpoint_correction", {"entity": {}}, readonly=True),
        ConfigField("soc", {"entity": {}}, readonly=True),
        ConfigField("available_kwh", {"entity": {}}, readonly=True),
        ConfigField("capacity_kwh", {"entity": {}}, readonly=True),
        ConfigField("charge_power", {"entity": {}}, readonly=True),
        ConfigField("discharge_power", {"entity": {}}, readonly=True),
        ConfigField("charge_power_aggr", {"entity": {}}, readonly=True),
        ConfigField("discharge_power_aggr", {"entity": {}}, readonly=True),
        ConfigField("grid_import", {"entity": {}}, readonly=True),
        ConfigField("grid_export", {"entity": {}}, readonly=True),
        ConfigField("grid_l1", {"entity": {}}, readonly=True),
        ConfigField("grid_l2", {"entity": {}}, readonly=True),
        ConfigField("grid_l3", {"entity": {}}, readonly=True),
        ConfigField("optimize", {"entity": {}}, readonly=True),
        ConfigField("savings_today", {"entity": {}}, readonly=True),
    ],
    ZendureDriver: [
        ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True),
        ConfigField("apptoken", {"text": {"type": "password"}}, cast=str, required=False, section_key="cloud"),
        ConfigField("cloud_url", {"text": {}}, cast=str, required=False, expert=True, section_key="cloud"),
        ConfigField("mqtt_host", {"text": {}}, cast=str, required=False, section_key="local_broker"),
        ConfigField("mqtt_port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False, section_key="local_broker"),
        ConfigField("mqtt_user", {"text": {}}, cast=str, required=False, section_key="local_broker"),
        ConfigField("mqtt_psw", {"text": {"type": "password"}}, cast=str, required=False, section_key="local_broker"),
        ConfigField("mqtt_log", {"boolean": {}}, cast=bool, section_key="local_broker"),
        ConfigField("wifi_ssid", {"text": {}}, cast=str, required=False, section_key="wifi"),
        ConfigField("wifi_psw", {"text": {"type": "password"}}, cast=str, required=False, section_key="wifi"),
    ],
    ZendureSolarFlow: [
        ConfigField("device_id", {"text": {}}, cast=str, is_key=True, readonly=True),
        ConfigField("name", {"text": {}}, cast=str, readonly=True),
        ConfigField("model", {"text": {}}, cast=str, readonly=True),
        ConfigField("prodkey", {"text": {}}, cast=str, readonly=True),
        ConfigField("sn", {"text": {}}, cast=str, readonly=True),
        ConfigField("ip", {"text": {}}, cast=str, required=False),
        ConfigField("blemac", {"text": {}}, cast=str, required=False),
        ConfigField("ble_name", {"text": {}}, cast=str, visible_when={"name": "-"}, readonly=True),
        ConfigField("kwh", {"number": {"step": 0.1, "unit_of_measurement": "kWh"}}, cast=float, readonly=True),
        ConfigField("soc", {"entity": {}}, readonly=True),
        ConfigField("soc_min", {"entity": {}}, readonly=True),
        ConfigField("soc_max", {"entity": {}}, readonly=True),
        ConfigField("home_input", {"entity": {}}, readonly=True),
        ConfigField("home_output", {"entity": {}}, readonly=True),
        ConfigField("home_input_aggr", {"entity": {}}, readonly=True),
        ConfigField("home_output_aggr", {"entity": {}}, readonly=True),
        ConfigField("rte", {"entity": {}}, readonly=True),
        ConfigField("bypass", {"entity": {}}, readonly=True),
        ConfigField("solar", {"entity": {}}, readonly=True),
        ConfigField("power_phase", {"entity": {}}, readonly=True),
        ConfigField("temp", {"entity": {}}, readonly=True),
        ConfigField("connection", {"number": {}}, cast=int, readonly=True),
    ],
    ZendureSmartMeter: [
        ConfigField("device_id", {"text": {}}, cast=str, is_key=True, readonly=True),
        ConfigField("name", {"text": {}}, cast=str, readonly=True),
        ConfigField("model", {"text": {}}, cast=str, readonly=True),
        ConfigField("sn", {"text": {}}, cast=str, readonly=True),
        ConfigField("totalPower", {"entity": {}}, readonly=True),
        ConfigField("phase1_power", {"entity": {}}, readonly=True),
        ConfigField("phase2_power", {"entity": {}}, readonly=True),
        ConfigField("phase3_power", {"entity": {}}, readonly=True),
        ConfigField("phase1_current", {"entity": {}}, readonly=True),
        ConfigField("phase2_current", {"entity": {}}, readonly=True),
        ConfigField("phase3_current", {"entity": {}}, readonly=True),
    ],
}


def fields_of(obj: Any) -> list[ConfigField] | None:
    # a shared class from FIELDS (subclasses included), a solver class from its own CONFIG
    return next((fields for cls, fields in FIELDS.items() if isinstance(obj, cls)), None) or getattr(type(obj), "CONFIG", None)


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
