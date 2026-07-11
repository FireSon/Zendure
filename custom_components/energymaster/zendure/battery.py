"""ZendureBattery — battery pack with per-cell monitoring entities.

Each battery pack is a child device of its parent ZendureDevice.
Identified by serial number prefix, discovered via MQTT packData.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from ..entity import EmDevice
from ..sensor import EmSensor


def _temp_convert(v) -> float:
    """Convert decikelvin to Celsius: (value - 2731) / 10."""
    return round((v - 2731) / 10, 1)


def _current_convert(v) -> float:
    """Convert signed 16-bit deciamps to Amps."""
    if v >= 32768:
        v = v - 65536
    return v / 10.0


class ZendureBattery(EmDevice):
    """Battery pack with per-cell monitoring.

    Entities:
        packState   — battery pack state
        socLevel    — SOC (%)
        state       — state
        power       — battery power (W)
        maxTemp     — max cell temperature (C)
        totalVol    — total voltage (V)
        batcur      — battery current (A)
        maxVol      — max cell voltage (V)
        minVol      — min cell voltage (V)
    """

    _registry: dict[str, ZendureBattery] = {}

    def __init__(self, hass: HomeAssistant, sn: str, parent: EmDevice) -> None:
        ZendureBattery._registry[sn] = self
        self.sn = sn
        self.kWh = 0.0

        match sn[0]:
            case "A":
                if sn[3] == "3":
                    model = "AIO2400"
                    self.kWh = 2.4
                else:
                    model = "AB1000"
                    self.kWh = 0.96
            case "B":
                model = "AB1000S"
                self.kWh = 0.96
            case "C":
                model = "AB2000" + ("S" if sn[3] == "F" else "X" if sn[3] == "E" else "")
                self.kWh = 1.92
            case "F":
                model = "AB3000"
                self.kWh = 2.88
            case "J":
                model = "AB3000L"
                self.kWh = 2.88
            case _:
                model = "Unknown"
                self.kWh = 0.0

        self.model = model
        device_id = f"{model}_{sn[-8:]}"

        super().__init__(
            hass,
            device_id,
            f"{model} {sn[-8:]}",
            model=model,
            manufacturer="Zendure",
            parent=parent.device_id,
        )

    def device_entities(self):
        self.packState = EmSensor("packState")
        self.soc = EmSensor("socLevel", uom="%", device_class="battery", state_class="measurement")
        self.state = EmSensor("state")
        self.power = EmSensor("power", uom="W", device_class="power", state_class="measurement")
        self.maxTemp = EmSensor("maxTemp", uom="°C", device_class="temperature", state_class="measurement", convert=_temp_convert)
        self.totalVol = EmSensor("totalVol", uom="V", device_class="voltage", state_class="measurement")
        self.batcur = EmSensor("batcur", uom="A", device_class="current", state_class="measurement", convert=_current_convert)
        self.maxVol = EmSensor("maxVol", uom="V", device_class="voltage", state_class="measurement", convert=lambda v: v / 100.0)
        self.minVol = EmSensor("minVol", uom="V", device_class="voltage", state_class="measurement", convert=lambda v: v / 100.0)

    def update_properties(self, payload: dict, now: float) -> None:
        """Update battery properties from MQTT telemetry (packData entry)."""
        for key, value in payload.items():
            self.entity_update(key, value)
