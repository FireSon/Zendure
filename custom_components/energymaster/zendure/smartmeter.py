"""ZendureSmartMeter — Zendure CT clamp / smart meter via MQTT.

Reports per-phase power, current, voltage. Can serve as P1 source
for Distribution — faster than HA state listener (direct MQTT, ~1s).

MQTT telemetry format:
    properties: {totalPower: watts}
    circuits: [{phase: 1, p: watts, i: amps*100, u: volts*100}, ...]
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from ..entity import EmDevice
from ..sensor import EmSensor

_LOGGER = logging.getLogger(__name__)


class ZendureSmartMeter(EmDevice):
    """Zendure smart meter / CT clamp with per-phase monitoring."""

    def __init__(self, hass: HomeAssistant, device_id: str, name: str, model: str = "SmartMeter", prodkey: str = "") -> None:
        super().__init__(hass, device_id, name, model=model, manufacturer="Zendure")
        self.model = model
        self.prodkey = prodkey

    def device_entities(self):
        # Total power
        self.totalPower = EmSensor("totalPower", uom="W", device_class="power", state_class="measurement")

        # Per-phase power
        self.phase1_power = EmSensor("phase1_power", uom="W", device_class="power", state_class="measurement")
        self.phase2_power = EmSensor("phase2_power", uom="W", device_class="power", state_class="measurement")
        self.phase3_power = EmSensor("phase3_power", uom="W", device_class="power", state_class="measurement")

        # Per-phase current (raw value / 100 = Amps)
        self.phase1_current = EmSensor("phase1_current", uom="A", device_class="current", state_class="measurement", convert=lambda v: v / 100.0)
        self.phase2_current = EmSensor("phase2_current", uom="A", device_class="current", state_class="measurement", convert=lambda v: v / 100.0)
        self.phase3_current = EmSensor("phase3_current", uom="A", device_class="current", state_class="measurement", convert=lambda v: v / 100.0)

        # Per-phase voltage (raw value / 100 = Volts)
        self.phase1_voltage = EmSensor("phase1_voltage", uom="V", device_class="voltage", state_class="measurement", convert=lambda v: v / 100.0)
        self.phase2_voltage = EmSensor("phase2_voltage", uom="V", device_class="voltage", state_class="measurement", convert=lambda v: v / 100.0)
        self.phase3_voltage = EmSensor("phase3_voltage", uom="V", device_class="voltage", state_class="measurement", convert=lambda v: v / 100.0)

    def update_properties(self, payload: dict, now: float) -> None:
        """Handle incoming MQTT telemetry: flat properties and the per-phase circuits array."""
        # Flat properties: {totalPower: watts, ...}
        if properties := payload.get("properties"):
            for key, value in properties.items():
                self.entity_update(key, value)

        # Per-phase circuits: [{phase: 1, p: watts, i: amps*100, u: volts*100}, ...]
        if circuits := payload.get("circuits"):
            for phase_data in circuits:
                phase_num = phase_data.get("phase")
                if phase_num is None:
                    continue
                prefix = f"phase{phase_num}"
                if (p := phase_data.get("p")) is not None:
                    self.entity_update(f"{prefix}_power", p)
                if (i := phase_data.get("i")) is not None:
                    self.entity_update(f"{prefix}_current", i)
                if (u := phase_data.get("u")) is not None:
                    self.entity_update(f"{prefix}_voltage", u)
