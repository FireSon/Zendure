from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from .const import Phase
from .distribution import Distribution
from .zendure.device import ZendureDevice
from .zendure.driver import ZendureDriver
from .solver.consumers.ev import ElectricVehicle
from .solver.consumers.appliance import Appliance
from .solver.dynamicprice import DynamicPrice
from .solver.forecast import Forecast, SolarPanel
from .solver.heating import Heating

class EDIT_TYPE(IntEnum):
    int = 1
    float = 2
    string = 3
    bool = 4
    entity = 5
    dropdown = 6
    command = 7
    instance = 8
    list = 9
    listpoly = 10
    phase = 11
    device = 12


@dataclass
class ConfigField:
    id: str
    type: EDIT_TYPE
    is_key: bool = False
    required: bool = True
    readonly: bool = False
    options: dict | None = None
    min_value: float | None = None
    max_value: float | None = None
    expert: bool = False
    secret: bool = False  # password/token: masked in the UI (input type=password, dots in the read-only view)
    visible_when: dict | None = None  # {field_id: value or [values]} -> field shown only when it matches


# only these EDIT_TYPEs are (de)serialized; the value is the deserialize cast
_DESERIALIZERS = {
    EDIT_TYPE.int: int,
    EDIT_TYPE.float: float,
    EDIT_TYPE.string: str,
    EDIT_TYPE.bool: bool,
    EDIT_TYPE.entity: str,
    EDIT_TYPE.device: str,
    EDIT_TYPE.phase: lambda v: Phase(int(v)),
}

_PRICE_PROVIDERS = {
    "FrankProvider": "Frank Energie",
    "TibberProvider": "Tibber",
    "ZonneplanProvider": "Zonneplan",
    "EnergyChartsProvider": "Energy-Charts (spot)",
    "NextEnergyProvider": "NextEnergy",
}

CONFIG_SCHEMA: dict[type, list[ConfigField]] = {
    DynamicPrice: [
        ConfigField("provider", EDIT_TYPE.string, options=_PRICE_PROVIDERS),
        ConfigField("saldering", EDIT_TYPE.bool),
        ConfigField("email", EDIT_TYPE.string, required=False, visible_when={"provider": ["ZonneplanProvider"]}),
        ConfigField("access_token", EDIT_TYPE.string, required=False, visible_when={"provider": ["TibberProvider", "ZonneplanProvider"]}),
        ConfigField("auth_uuid", EDIT_TYPE.string, required=False, visible_when={"provider": ["ZonneplanProvider"]}),
    ],
    Forecast: [
        ConfigField("solar_panels", EDIT_TYPE.list),
    ],
    SolarPanel: [
        ConfigField("name", EDIT_TYPE.string, is_key=True),
        ConfigField("power_entity", EDIT_TYPE.entity),
        ConfigField("derate_entity", EDIT_TYPE.entity),
    ],
    Distribution: [
        ConfigField("max_kw", EDIT_TYPE.float),
        ConfigField("p1_device", EDIT_TYPE.device, required=False),
    ],
    ZendureDriver: [
        ConfigField("id", EDIT_TYPE.string, is_key=True),
        ConfigField("apptoken", EDIT_TYPE.string, required=False, secret=True),
        ConfigField("mqtt_host", EDIT_TYPE.string, required=False),
        ConfigField("mqtt_port", EDIT_TYPE.int, required=False),
        ConfigField("mqtt_user", EDIT_TYPE.string, required=False),
        ConfigField("mqtt_psw", EDIT_TYPE.string, required=False, secret=True),
        ConfigField("wifi_ssid", EDIT_TYPE.string, required=False),
        ConfigField("wifi_psw", EDIT_TYPE.string, required=False, secret=True),
    ],
    ZendureDevice: [
        ConfigField("name", EDIT_TYPE.string),
        ConfigField("model", EDIT_TYPE.string, readonly=True),
        ConfigField("prodkey", EDIT_TYPE.string, readonly=True),
        ConfigField("sn", EDIT_TYPE.string, readonly=True),
        ConfigField("ip", EDIT_TYPE.string, readonly=True),
        ConfigField("blemac", EDIT_TYPE.string, readonly=True),
        ConfigField("blename", EDIT_TYPE.string, visible_when={ "name": "-" }),
        ConfigField("capacity_kwh", EDIT_TYPE.float, readonly=True),
        ConfigField("phase", EDIT_TYPE.phase),
    ],
    ElectricVehicle: [
        ConfigField("id", EDIT_TYPE.string, is_key=True),
        ConfigField("charge_power_entity", EDIT_TYPE.entity),
        ConfigField("soc_entity", EDIT_TYPE.entity),
        ConfigField("connected_entity", EDIT_TYPE.entity),
        ConfigField("ev_max_soc", EDIT_TYPE.float),
        ConfigField("capacity_kwh", EDIT_TYPE.float),
        ConfigField("max_power_kw", EDIT_TYPE.float),
        ConfigField("charge_compensation_eur_kwh", EDIT_TYPE.float),
        ConfigField("departure_time", EDIT_TYPE.string),
        ConfigField("phase", EDIT_TYPE.phase),
    ],
    Heating: [
        ConfigField("id", EDIT_TYPE.string, is_key=True),
        ConfigField("kind", EDIT_TYPE.string, options={"house": "Ruimteverwarming", "boiler": "Boiler", "pool": "Zwembad"}, readonly=True),
        ConfigField("source", EDIT_TYPE.string, options={"hp": "Warmtepomp", "electric": "Elektrisch"}, readonly=True),
        ConfigField("name", EDIT_TYPE.string),
        ConfigField("temp_entity", EDIT_TYPE.entity),
        ConfigField("power_entity", EDIT_TYPE.entity),
        ConfigField("max_power", EDIT_TYPE.float),
        ConfigField("t_min", EDIT_TYPE.float),
        ConfigField("t_max", EDIT_TYPE.float),
        ConfigField("t_target", EDIT_TYPE.float),
        ConfigField("t_abs_min", EDIT_TYPE.float, visible_when={"kind": ["house"]}),
        ConfigField("t_min_night", EDIT_TYPE.float, visible_when={"kind": ["house"]}),
        ConfigField("ambient", EDIT_TYPE.float, visible_when={"kind": ["boiler", "pool"]}),
        ConfigField("ramp_kw", EDIT_TYPE.float),
        ConfigField("phase", EDIT_TYPE.phase),
    ],
    Appliance: [
        ConfigField("id", EDIT_TYPE.string, is_key=True),
        ConfigField("kind", EDIT_TYPE.string, options={"dishwasher": "Vaatwasser", "dryer": "Droger", "washingmachine": "Wasmachine", "custom": "Overig"}, readonly=True),
        ConfigField("switch_entity", EDIT_TYPE.entity),
        ConfigField("power_entity", EDIT_TYPE.entity),
        ConfigField("armed", EDIT_TYPE.bool),
        ConfigField("power_rating", EDIT_TYPE.float),
        ConfigField("cycle_duration_slots", EDIT_TYPE.int),
        ConfigField("latest_start_time", EDIT_TYPE.string),
        ConfigField("phase", EDIT_TYPE.phase),
        ConfigField("start", EDIT_TYPE.command, required=False, options={"action": "start"}),
        ConfigField("stop", EDIT_TYPE.command, required=False, options={"action": "stop"}),
        ConfigField("skip", EDIT_TYPE.command, required=False, options={"action": "skip"}),
    ],
}

_BY_NAME = {cls.__name__: cls for cls in CONFIG_SCHEMA}


class Config:
    def __init__(self, path: str) -> None:
        self.payload: dict[str, Any] = {}
        self.path = Path(path)
   
    def read_config(self):
        self.payload = json.loads(self.path.read_text(encoding="utf-8"))

    def readlist(self, lst, items):
        for item in items:
            cls = _BY_NAME.get(item.get("type"))
            if cls is None:
                continue
            key = next((f for f in CONFIG_SCHEMA[cls] if f.is_key), None)
            child = next((c for c in lst if key is not None and getattr(c, key.id, None) == item.get(key.id)), None)
            if child is None:
                child = cls()
                lst.append(child)
            self.read(child, payload=item)

    def read(self, obj: Any, name: str | None = None, payload = None) -> None:
        fields = CONFIG_SCHEMA.get(type(obj))
        if fields is None:
            return
        
        if payload is None:
            payload = self.payload
        data = payload if name is None else payload.get(name) or {}
        for field in fields:
            if field.type == EDIT_TYPE.instance:
                self.read(getattr(obj, field.id), name=field.id, payload=data)
                continue
            if field.type == EDIT_TYPE.listpoly:
                self.readlist(getattr(obj, field.id), data.get(field.id) or [])
                continue
            cast = _DESERIALIZERS.get(field.type)
            if cast is None or field.id not in data:
                continue
            setattr(obj, field.id, cast(data[field.id]))

    def write(self, obj: Any, name: str | None = None, payload: dict | None = None) -> None:
        fields = CONFIG_SCHEMA.get(type(obj))
        if fields is None:
            return
        if payload is None:
            payload = self.payload
        data = payload if name is None else payload.setdefault(name, {})
        for field in fields:
            if field.type == EDIT_TYPE.instance:
                child = getattr(obj, field.id)
                self.write(child, name=field.id, payload=data)
                continue
            if field.type == EDIT_TYPE.listpoly:
                items = []
                for child in getattr(obj, field.id):
                    item = {"type": type(child).__name__}
                    self.write(child, payload=item)
                    items.append(item)
                data[field.id] = items
                continue
            if field.type not in _DESERIALIZERS:
                continue
            value = getattr(obj, field.id, None)
            if value is None:
                continue
            data[field.id] = value

