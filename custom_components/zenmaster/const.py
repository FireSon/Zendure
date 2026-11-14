"""Constants for the ZenMaster integration."""

from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from math import sqrt
from typing import Any

DOMAIN = "zenmaster"

PANEL_TITLE = "ZenMaster"
PANEL_ICON = "mdi:home-lightning-bolt"
STATIC_URL = f"/{DOMAIN}_static"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MODEL = "icon_seamless"  # weather model of the solar forecast and its calibration: best_match takes KNMI HARMONIE in NL, which misses low cloud


class Phase(IntEnum):
    all = 0
    l1 = 1
    l2 = 2
    l3 = 3

    def __str__(self) -> str:
        return self.name.upper()


PhaseModi = {None: "unknown", Phase.all: "all", Phase.l1: "l1", Phase.l2: "l2", Phase.l3: "l3"}  # the select options: the key is the semantic value, the label its name


def watt_factor(state: Any, phase: Phase = Phase.all) -> float:
    # multiplier that turns a sensor reading into watts; a current needs a voltage, and that follows from the phase
    unit = state.attributes.get("unit_of_measurement") if state is not None else None
    if unit in ("kW", "kilowatt", "kilowatts"):
        return 1000.0
    if unit == "A":
        return 400.0 * sqrt(3) if phase == Phase.all else 230.0
    return 1.0


def time_to_slot(time_str: str) -> int | None:
    # a wall-clock time as its quarter of the day; None when it is not readable
    try:
        h, m = time_str.split(":")[:2]  # "HH:MM", or "HH:MM:SS" the way HA's time entity keeps it
        return int(h) * 4 + int(m) // 15
    except (ValueError, AttributeError):
        return None


def stat_to_time(start: Any) -> datetime:
    # the start of a recorder statistics row as a UTC time: the recorder hands it over as seconds or as a datetime
    return datetime.fromtimestamp(start, timezone.utc) if isinstance(start, (int, float)) else start


class SchemaAction(StrEnum):
    add = "add"
    edit = "edit"
    delete = "delete"
