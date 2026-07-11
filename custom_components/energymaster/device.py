"""Distribution — power profiles, devices, and fleet manager."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import Phase
from .entity import EmDevice, EmEntity
from .sensor import EmSensor


_LOGGER = logging.getLogger(__name__)

FLOOR_PCT = 0.10  # device floor as a fraction of rated power


class DeviceConnection(IntEnum):
    DISCOVERED = 0
    OFFLINE = 1
    MQTT_CLOUD = 2
    MQTT_LOCAL = 3
    MODBUS = 4
    HTTP = 5
    BLE = 6


@dataclass
class PowerProfile:
    """Base power profile with shared timing and power fields."""

    power: int = 800
    max_solar: int = 0
    lag_cold: float = 9.0
    lag_warm: float = 4.0
    ramp_rate: float = 200.0
    redirect_window: float = 0.0
    f_up: float = 0.7
    f_down: float = 0.3

    def __post_init__(self):
        self.floor = int(self.power * FLOOR_PCT)

    def power_range(self, r: list[int]) -> None:
        pass


class PowerProfileDischarge(PowerProfile):
    """Discharge direction profile."""

    def power_range(self, r: list[int]) -> None:
        r[0] += self.floor
        r[1] += self.power


class PowerProfileCharge(PowerProfile):
    """Charge direction profile."""

    def power_range(self, r: list[int]) -> None:
        r[0] += self.power
        r[1] += self.floor


class PowerDevice(EmDevice):
    """A battery device capable of participating in power distribution."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        name: str,
        capacity_kwh: float,
        discharge: PowerProfileDischarge,
        charge: PowerProfileCharge,
        *,
        model: str = "",
        manufacturer: str = "",
        parent: str | None = None,
        phase: Phase | None = None,
    ) -> None:
        super().__init__(hass, device_id, name, model=model, manufacturer=manufacturer, parent=parent)
        self.phase = phase
        self.capacity_kwh = capacity_kwh
        self.profile = [discharge, charge]  # [False]=discharge, [True]=charge

        self.setpoint: int = 0
        self.inefficient_time: float = 0.0
        self.score: list[float] = [0.0, 0.0]  # [discharge, charge] selection score, set in Distribution
        self._prev_output: int = 0
        self._arrival: float = 0.0
        self._offline: float = time.time() + 30.0
        self._active_time: float = 0.0
        self._ramp_rate: float = discharge.ramp_rate
        self._stable_until: float = 0.0
        self._recover_task: asyncio.Task | None = None

    def device_entities(self):
        """Default every entity slot to the shared Empty sentinel.

        Driver subclasses override the slots they actually support with real
        EmSensor / EmNumber instances. Slots left as Empty stay
        out of HA's entity registry but still satisfy calculation reads
        (``device.solar.asNumber`` etc.) via class-level defaults.
        """
        self.soc = EmEntity.Empty
        self.soc_min = EmEntity.Empty
        self.soc_max = EmEntity.Empty
        self.home_input = EmEntity.Empty
        self.home_output = EmEntity.Empty
        self.battery_input = EmEntity.Empty
        self.battery_output = EmEntity.Empty
        self.solar = EmEntity.Empty
        self.temp = EmEntity.Empty
        self.offgrid = EmEntity.Empty
        self.available_kwh = EmEntity.Empty
        self.total_kwh = EmEntity.Empty
        self.remaining_time = EmEntity.Empty
        self.device_efficiency = EmEntity.Empty
        self.connection = EmSensor(id="connection")

    @property
    def online(self) -> bool:
        return self.connection.asInt > DeviceConnection.OFFLINE

    async def power_off(self) -> None:
        await self.on_setpoint(0, self.profile[False], time.time())

    def actual_power(self, now: float) -> int:
        """Estimated current output. Step if ramp_rate=0, ramp otherwise."""
        if self.connection.asInt <= DeviceConnection.OFFLINE:
            return 0

        if (offline := self._offline > 0 and now > self._offline) and not self._recover_task:
            self._recover_task = asyncio.create_task(self._recover_fallback())
        elif not offline and self._recover_task:
            self._recover_task.cancel()
            self._recover_task = None

        if (self.setpoint == 0 and self._prev_output == 0) or self.capacity_kwh == 0:
            return 0
        if now < self._arrival:
            return self._prev_output
        if self._ramp_rate == 0:
            return self.setpoint
        step = self.setpoint - self._prev_output
        ramped = min(abs(step), self._ramp_rate * (now - self._arrival))
        return int(self._prev_output + (1 if step > 0 else -1) * ramped)

    def output_power(self, now: float) -> int:
        """Power used for the P1 correction. Measured AC power (home_output - home_input)
        when the device has real telemetry, else the modelled estimate (simulation/base).
        actual_power is always called so its offline-recovery side effects still run."""
        est = self.actual_power(now)
        if self.home_output is EmEntity.Empty and self.home_input is EmEntity.Empty:
            return est
        return int(self.home_output.asNumber - self.home_input.asNumber)

    def on_telemetry(self, actual: int, now: float) -> None:
        """Update state from device telemetry."""
        if actual != self._prev_output:
            self._stable_until = now + 60
        self._prev_output = actual
        self._arrival = now
        if actual == 0 or abs(actual) > abs(self.profile[actual < 0].floor) * 1.5:
            self.inefficient_time = 0
        elif self.inefficient_time == 0:
            self.inefficient_time = now

    async def on_setpoint(self, watts: int, p: PowerProfile, now: float) -> int:
        """Apply setpoint. Override in hardware subclasses to send commands."""
        if abs(watts) > abs(p.power):
            watts = p.power
        if watts > 0 and self.soc.asNumber <= self.soc_min.asNumber:
            watts = 0
        elif watts < 0 and self.soc.asNumber >= self.soc_max.asNumber:
            watts = 0

        if p.redirect_window > 0 and self.setpoint != 0 and (self._arrival - now) > (p.lag_warm - p.redirect_window):
            self.setpoint = watts
            return self.setpoint

        if now >= self._arrival:
            self._prev_output = self.setpoint
        lag = p.lag_cold if now - self._active_time > 30 else p.lag_warm
        self._arrival = now + lag
        self._ramp_rate = p.ramp_rate
        if self.setpoint != 0:
            self._active_time = now
        else:
            self.inefficient_time = 0
        self.setpoint = watts
        return self.setpoint

    async def _recover_fallback(self) -> None:
        """Override in subclass to implement recovery logic."""
        self.connection.update_value(DeviceConnection.OFFLINE)
