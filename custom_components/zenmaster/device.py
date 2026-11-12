"""DistributionDevice: the generic contract every power device offers the distribution and the UI."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

from .const import Phase, PhaseModi
from .entity import EntityDevice
from .number import HaNumber
from .select import HaSelect
from .sensor import HaSensor

if TYPE_CHECKING:
    from .distribution import DistributionMode

_LOGGER = logging.getLogger(__name__)


FLOOR_PCT = 0.10  # device floor as a fraction of rated power
BAND_LOW_PCT = 0.30  # inverter band lower edge (the last/only active device may run below it)
BAND_HIGH_PCT = 0.95  # inverter band upper edge
REPORT_WAIT_S = 10.0  # a device awaiting the echo of its last command is left alone this long at most; one that stops reporting must not freeze the fleet


class DeviceState(IntEnum):
    """Shared meaning for every vendor; the distribution relies on these semantics."""

    CREATED = 0  # just built, the device has never reported
    CONTROLLER = 1  # regulates itself and is not steered by us: in no capacity, plan or regulation
    NOGRID = 2  # reachable, but no grid on its AC side: it can neither deliver nor charge from the grid
    OFFLINE = 3  # unreachable: the vendor machinery is probing
    EXTERNAL = 4  # own behavior, not under our control
    SENT = 5  # command sent, awaiting the transport ack
    CONFIRM = 6  # acked, awaiting the output to follow
    ACTIVE = 7  # under control at a confirmed setpoint (0 W included)


@dataclass
class PowerProfile:
    """The rated power of one direction with the floor and band that follow from it; the subclass knows the direction."""

    power: int = 800

    def __post_init__(self):
        # bands are magnitudes, the floor keeps the direction of power (negative while charging)
        limit = abs(self.power)
        self.floor = int(limit * FLOOR_PCT) * (1 if self.power >= 0 else -1)
        self.band_low = int(limit * BAND_LOW_PCT)
        self.band_high = int(limit * BAND_HIGH_PCT)

    def available_power(self, device: DistributionDevice) -> int:
        return self.power

    def available_kwh(self, device: DistributionDevice) -> float:
        return 0.0


class ProfileDischarge(PowerProfile):
    """Discharge direction profile."""

    def available_power(self, device: DistributionDevice) -> int:
        if device.soc_limit == 2 or (device.kwh and device.soc <= device.soc_min):  # empty: the device's own verdict or ours
            return 0
        # thermal derating: at 55 °C or more, delivering 25 W or more under its setpoint for 25 s, the device is asked no more than it proves to manage plus 15 W
        if device.temp >= 55 and device.power_setpoint - device.power_actual > 25 and time.time() - device.power_since > 25:
            return min(self.power, device.power_setpoint + 15)
        return self.power

    def available_kwh(self, device: DistributionDevice) -> float:
        return device.kwh * device.soc / 100.0


class ProfileCharge(PowerProfile):
    """Charge direction profile."""

    def available_power(self, device: DistributionDevice) -> int:
        if device.soc_limit == 1 or (device.kwh and device.soc >= device.soc_max):  # full: the device's own verdict or ours
            return 0
        return self.power

    def available_kwh(self, device: DistributionDevice) -> float:
        return device.kwh * (device.soc_max - device.soc) / 100.0


class DistributionDevice(EntityDevice):
    """Generic contract only; the vendor implements the behavior and keeps every field and state truthful."""

    allow_control = True  # the fleet steers it; a brand whose control is not confirmed on a device leaves that to the user
    POWER_SET = 40  # W: the measured power is this close to the setpoint, the command is in
    POWER_MAX_TIME = 10  # s: or this long after the command
    HOLD_S = 2.0  # s a device is left alone after a command
    DEADBAND_W = 10  # W: no new command while the measured power and the last setpoint are both this close to the new target

    def __init__(self, hass: HomeAssistant, device_id: str, profiles: list[PowerProfile], name: str | None = None, subentry_id: str | None = None) -> None:
        super().__init__(hass, device_id, name, subentry_id)

        # what the device is doing; CREATED until the device has ever spoken
        self.state = DeviceState.CREATED
        self.state_since = 0.0
        self.on_invalidate: Callable | None = None
        self.on_phase: Callable | None = None  # the coordinator: the LP counts the battery on its phase, so a new phase rebuilds it
        self.soc = 0.0
        self.soc_min = 10.0
        self.soc_max = 90.0
        self.soc_limit = 0  # the device's own verdict: 1 = full, 2 = empty, 0 = neither
        self.solar = 0
        self.grid_off_power = 0  # W the off-grid socket draws; sits inside gridInputPower and is invisible to P1
        self.bypass = False  # panels straight to the house: that output is not ours to count or steer
        self.temp = 25.0
        self.kwh = 0.0
        self.wear = 0.0  # equivalent full cycles over the wear window, set by the distribution

        # power settings
        self.power_profile = profiles
        self.power_phase: Phase | None = None  # None: not known yet; restored from the device's own Store, not from the config
        self.power_setpoint = 0  # what the device is going to do under our control
        self.power_actual = 0  # measured charge/discharge value now; zeroed while OFFLINE
        self.power_since = 0.0  # last update of the power_setpoint
        self.individual: int | None = None  # W in the individual mode, set by the solver; None for a device without storage, update_order drives that one

    async def update_phase(self, _entity: Any = None, _option: Any = None) -> None:
        # a changed phase regroups the fleet, so the order has to be rebuilt, and the LP that counts the battery on it
        if self.on_invalidate is not None:
            self.on_invalidate()
        if self.on_phase is not None:
            self.on_phase()

    def power(self, watts: int, mode: DistributionMode) -> int:
        """Vendor contract: the device decides what it does with the request in this fleet mode; the return is the current power_actual."""
        return self.power_actual

    def clamp(self, watts: int) -> int:
        # a request inside what the device can do now: its limits, empty or full, its thermal derating
        return max(self.power_profile[True].available_power(self), min(self.power_profile[False].available_power(self), watts))

    def power_off(self) -> None:
        """Vendor contract: end the control mode; the device returns to its own behavior."""

    def set_state(self, state: DeviceState) -> None:
        if state != self.state:
            if self.on_invalidate and (self.state <= DeviceState.EXTERNAL or state <= DeviceState.EXTERNAL):
                self.on_invalidate()
            self.state = state
            self.state_since = time.time()

    def new_setpoint(self, watts: int) -> bool:
        # False inside the dead band; else the setpoint is published, so the recorder holds the command side too, and the caller sends it
        if abs(self.power_actual - watts) < self.DEADBAND_W and abs(self.power_setpoint - watts) < self.DEADBAND_W:
            return False
        if self.power_setpoint != watts:
            self.power_since = time.time()
        self.setpoint.update_value(watts)
        return True

    def power_reached(self, now: float) -> bool:
        # the command is in: the measured power is close to the setpoint, or it has had its time
        return abs(self.power_actual - self.power_setpoint) < self.POWER_SET or now - self.state_since > self.POWER_MAX_TIME


class PolledDevice(DistributionDevice):
    """A battery on a connection of its own: one loop sends a pending command first, else polls, and wakes at once on a new command."""

    POLL_S = 2.0  # s between two polls without a command
    RESEND_S = 30  # s before an accepted setpoint goes again, in case the device lost it
    OFFLINE_POLLS = 3  # failed polls before the device counts as offline

    def __init__(self, hass: HomeAssistant, device_id: str, profiles: list[PowerProfile], name: str | None = None, subentry_id: str | None = None) -> None:
        super().__init__(hass, device_id, profiles, name, subentry_id)
        # the loop starts once the vendor's own __init__ is through: create_task only schedules it
        self.blocked = False  # the device says it cannot follow a setpoint now
        self._pending: tuple[str, int] | None = None  # ("power", W) or ("off", 0)
        self._control = False  # the device is ours since the last power()
        self._silent = 0
        self._polls = 0
        self._wake = asyncio.Event()
        self.device_task = asyncio.get_running_loop().create_task(self.device_loop())

    def unload(self) -> None:
        # the loop stops; a device under our control is handed back on its way out, then the connection closes
        self.device_task.cancel()
        self.hass.async_create_task(self.close())

    async def close(self) -> None:
        try:
            if self._control:
                await self.send("off", 0)
        finally:
            await self.disconnect()

    def entity_create(self) -> None:
        # the entities every polled battery has; a brand creates its own and then calls this
        HaNumber(self, "individual", uom="W", minimum=-10000, maximum=10000, on_changed=lambda _entity, _value: None)  # the setpoint of the individual mode, EM's own: nothing goes to the device

        HaSensor(self, "home_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "home_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_input", uom="W", device_class="power", state_class="measurement", aggr=True)
        HaSensor(self, "battery_output", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.setpoint = HaSensor(self, "power_setpoint", uom="W", device_class="power", state_class="measurement")
        HaSelect(self, "power_phase", PhaseModi, on_changed=self.update_phase, restore=True)

        self.kwhsensor = HaSensor(self, "kwh", uom="kWh", device_class="energy", restore=True)
        HaSensor(self, "rte", uom="%", state_class="measurement", precision=1)

    def soc_changed(self, entity: Any, value: Any) -> None:
        # soc_min and soc_max are ZenMaster's own limits, nothing goes to the device; the fleet order follows them
        setattr(self, entity.attr, value)
        if self.on_invalidate is not None:
            self.on_invalidate()

    def update_limits(self, values: dict[str, Any]) -> bool:
        # the capacity and the power limits the device reports; True when one changed, so the caller reorders the fleet
        changed = False
        if values.get("kwh") and round(values["kwh"], 3) != self.kwh:
            self.kwhsensor.update_value(round(values["kwh"], 3))
            changed = True
        for profile, key, sign in ((self.power_profile[0], "discharge_max", 1), (self.power_profile[1], "charge_max", -1)):
            if (limit := int(values.get(key) or 0)) and sign * limit != profile.power:
                profile.power = sign * limit
                profile.__post_init__()  # floor and bands follow the new limit
                changed = True
        return changed

    def power(self, watts: int, mode: DistributionMode) -> int:
        watts = self.clamp(watts)

        # the device is busy with its last command, unreachable, or cannot follow
        if time.time() - self.power_since < self.HOLD_S or self.state == DeviceState.OFFLINE or self.blocked:
            return self.power_actual

        # an accepted setpoint is not sent again while the power is on its way; after RESEND_S once more, in case the device lost it
        if watts == self.power_setpoint and self.state >= DeviceState.CONFIRM and time.time() - self.state_since < self.RESEND_S:
            return self.power_actual

        if not self.new_setpoint(watts):
            return self.power_actual
        self._pending = ("power", watts)
        self._wake.set()
        self.set_state(DeviceState.SENT)  # SENT, CONFIRM once the device accepts it, ACTIVE once the power follows
        return self.power_actual

    def power_off(self) -> None:
        # the device returns to the mode it had before we took control
        self._pending = ("off", 0)
        self._wake.set()
        self.setpoint.update_value(0)
        if self.state != DeviceState.OFFLINE:
            self.set_state(DeviceState.EXTERNAL)  # an offline device stays offline: it counts in no capacity until it reports again

    async def device_loop(self) -> None:
        while True:
            try:
                if (command := self._pending) is not None:
                    self._pending = None
                    await self.send(*command)
                else:
                    await self.poll()

                # a new command wakes the loop at once
                try:
                    async with asyncio.timeout(self.POLL_S):
                        await self._wake.wait()
                except TimeoutError:
                    pass
                self._wake.clear()

            except asyncio.CancelledError:
                raise
            except Exception:
                # the task must never die
                _LOGGER.exception("device task %s", self.device_id)

    def poll_failed(self, err: Exception) -> None:
        # a failed poll: after OFFLINE_POLLS in a row the device counts as offline, with no power
        self._silent += 1
        _LOGGER.debug("%s: poll failed (%s)", self.name, err)
        if self._silent >= self.OFFLINE_POLLS and self.state != DeviceState.OFFLINE:
            self.power_actual = 0
            self.set_state(DeviceState.OFFLINE)

    async def send(self, kind: str, watts: int) -> None:
        """Vendor contract: write a setpoint ("power") or hand the device back ("off")."""

    async def poll(self) -> None:
        """Vendor contract: read the device, update_status on success, poll_failed on an error."""

    async def disconnect(self) -> None:
        """Vendor contract: close the connection."""
