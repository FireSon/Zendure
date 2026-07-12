"""Distribution — power profiles, devices, and fleet manager."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum

from homeassistant.core import HomeAssistant

from .const import Phase
from .entity import EmDevice, EmEntity
from .sensor import EmSensor


_LOGGER = logging.getLogger(__name__)

FLOOR_PCT = 0.10  # device floor as a fraction of rated power
BAND_LOW_PCT = 0.30  # inverter-band ondergrens (laatste/enige actieve device mag eronder)
BAND_HIGH_PCT = 0.95  # inverter-band bovengrens
DEADBAND_W = 20  # kleinste vermogensverschil dat een nieuw commando waard is
SNAPSHOT_INTERVAL_S = 3600.0  # wear: uurlijkse snapshot van de eigen doorzet-tellers
WEAR_WINDOW_S = 7 * 86400.0  # wear = delta van de doorzet-tellers over 7 dagen
SLEEP_AFTER_S = 60.0  # device task: after 1 minute of nothing to do, enter power-save (if the driver supports it)
TIMEOUT_FACTOR = 2.0  # busy timeout = expected processing time x this factor
STALE_AFTER_S = 90.0  # no telemetry or activity for this long (while awake) -> OFFLINE
RECOVER_RETRY_S = 30.0  # first recovery retry interval, doubles per attempt
RECOVER_BACKOFF_MAX_S = 600.0  # recovery retry interval ceiling
LAG_BOOTSTRAP_S = 15.0  # expected processing time before any measurement exists
LAG_HISTORY_SIZE = 10  # expected processing time = average of the last N measured cycles
ACK_TIMEOUT_S = 5.0  # no command reply within this -> resend once, then OFFLINE
PLATEAU_STABLE_S = 3.0  # output unchanged this long (after 1x expected lag) below target -> conclude derating early
DERATE_FLOOR_PCT = 0.75  # a real thermal derating never cuts more than 25% off the rated power; lower observations are noise


class DeviceConnection(IntEnum):
    DISCOVERED = 0
    OFFLINE = 1
    MQTT_CLOUD = 2
    MQTT_LOCAL = 3
    MODBUS = 4
    HTTP = 5
    BLE = 6


class DeviceState(IntEnum):
    OFFLINE = 0  # no telemetry within threshold: recovery needed
    IDLE = 1  # not active (power-save is an internal flag within IDLE)
    ACTIVE = 2  # running at a confirmed setpoint
    PREPARE = 3  # command sent and being processed; a new setpoint may still replace it
    BUSY = 4  # command locked in, awaiting the telemetry reply


@dataclass
class PowerProfile:
    """Base power profile with shared timing and power fields."""

    power: int = 800
    max_solar: int = 0
    redirect_window: float = 0.0
    f_up: float = 0.7
    f_down: float = 0.3

    def __post_init__(self):
        self.floor = int(self.power * FLOOR_PCT)
        self.band_low = int(abs(self.power) * BAND_LOW_PCT)
        self.band_high = int(abs(self.power) * BAND_HIGH_PCT)


class PowerProfileDischarge(PowerProfile):
    """Discharge direction profile."""


class PowerProfileCharge(PowerProfile):
    """Charge direction profile."""


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
        self.score: list[float] = [0.0, 0.0]  # [discharge, charge] selection score, set in Distribution
        self._prev_output: int = 0
        self._wear_snapshots: list = []  # [timestamp, doorzet-kWh] per uur, 7 dagen; restored via de device-Store
        self._wear_snapshot_at: float = 0.0
        self._restore.append("_wear_snapshots")

        # device task: one long-running task per device owns the full command cycle
        self._task: asyncio.Task | None = None
        self._task_event = asyncio.Event()
        self.state = DeviceState.IDLE
        self._requested: int | None = None  # target requested by the dispatcher; None = no outstanding request
        self._pending_target: int | None = None  # sent command awaiting the telemetry reply
        self._pending_since = 0.0
        self._acked = True  # an acking driver lowers this in set_power and raises it via command_reply()
        self._resent = False  # pending command was resent once after a missing reply
        self._last_activity = 0.0
        self._last_telemetry = 0.0
        self._last_output_change = 0.0
        self._offline_since = 0.0
        self._recover_backoff = RECOVER_RETRY_S
        self.asleep = False
        self._lag_history: list[float] = []  # last measured processing times, persisted via the device Store
        self._derate_curve: list[dict[str, int]] = [{}, {}]  # [discharge, charge] temp °C -> max W the device proved to handle there
        self._restore.append("_lag_history")
        self._restore.append("_derate_curve")

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

    def output_power(self) -> int:
        """Power used for the P1 correction: last telemetry, 0 while the device is unreachable."""
        return 0 if self.state == DeviceState.OFFLINE else self._prev_output

    def on_telemetry(self, actual: int, now: float) -> None:
        """Update state from device telemetry."""
        if actual != self._prev_output:
            self._last_output_change = now
        self._prev_output = actual
        self._last_telemetry = now
        if actual and self.temp._state is not None:
            curve = self._derate_curve[actual < 0]
            key = str(int(self.temp.asNumber))
            if key in curve and abs(actual) > curve[key]:
                curve[key] = abs(actual)  # live evidence: the device proves it can do more at this temperature
        self._task_event.set()  # wake the device task: fresh telemetry may confirm a pending command
        if now >= self._wear_snapshot_at:
            self._wear_snapshot_at = now + SNAPSHOT_INTERVAL_S
            self._wear_snapshots.append([now, getattr(self, "battery_input_aggr", EmEntity.Empty).asNumber + getattr(self, "battery_output_aggr", EmEntity.Empty).asNumber])
            while self._wear_snapshots and self._wear_snapshots[0][0] < now - WEAR_WINDOW_S:
                self._wear_snapshots.pop(0)
            if self.hass and self.hass.loop.is_running():
                self.hass.async_create_task(self.save_state())

    def ramp_to(self, watts: float, charging: bool) -> int:
        """Damped step toward the target: gap x f_up/f_down from the profile, then request_power()."""
        p = self.profile[charging]
        signed = -int(watts) if charging else int(watts)
        gap = signed - self.setpoint
        if abs(gap) > DEADBAND_W:
            signed = self.setpoint + int(gap * (p.f_up if abs(signed) > abs(self.setpoint) else p.f_down))
        return self.request_power(signed)

    def wear_cycles(self) -> float:
        """Equivalente volle cycli over de wear-window: delta van de eigen doorzet-tellers; teller-reset -> 0."""
        current = getattr(self, "battery_input_aggr", EmEntity.Empty).asNumber + getattr(self, "battery_output_aggr", EmEntity.Empty).asNumber
        baseline = min(self._wear_snapshots[0][1], current) if self._wear_snapshots else current
        return (current - baseline) / self.capacity_kwh if self.capacity_kwh else 0.0

    def set_power(self, watts: int, sleep: bool = False) -> None:
        """Driver contract: fire-and-forget power command; sleep=True is the power-save hint; an acking driver lowers _acked here and calls command_reply() on the ack."""

    async def recover_connection(self) -> None:
        """Driver contract: try to re-establish contact; success shows up as fresh telemetry."""

    def command_reply(self) -> None:
        """Driver contract: call when the device confirms receipt of the last command."""
        self._acked = True
        self._task_event.set()

    def request_power(self, watts: int) -> int:
        """Dispatcher contract: desired power; returns what the device will actually deliver."""
        # hardware clamp and SoC gate on the command itself
        charging = watts < 0
        limit = abs(self.profile[charging].power)
        if abs(watts) > limit:
            watts = -limit if charging else limit
        if watts > 0 and self.soc.asNumber <= self.soc_min.asNumber:
            watts = 0
        elif watts < 0 and self.soc.asNumber >= self.soc_max.asNumber:
            watts = 0

        # only a real change is worth a command; 0 always passes while something is running
        if abs(watts - self.setpoint) <= DEADBAND_W and (watts != 0 or self.setpoint == 0):
            watts = self.setpoint
        else:
            self._requested = int(watts)
            self._task_event.set()

        # promise the distribution what the device can actually deliver; the derate curve never caps the command
        deliverable = abs(self.profile[charging].power)
        if self.temp._state is not None:
            deliverable = min(deliverable, self._derate_curve[charging].get(str(int(self.temp.asNumber)), deliverable))
        return max(-deliverable, min(deliverable, int(watts)))

    def start_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._device_loop())

    def stop_task(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _set_state(self, state: DeviceState, now: float) -> None:
        if state == DeviceState.OFFLINE:
            self._recover_backoff = RECOVER_RETRY_S
            self._offline_since = now
        self.state = state
        self._task_event.set()  # rerun the loop so the new state's handler computes its own wake_at

    def _stale(self, now: float) -> bool:
        base = max(self._last_telemetry, self._last_activity)
        return bool(base) and now - base > STALE_AFTER_S

    async def _device_loop(self) -> None:
        wake_at: float | None = None
        while True:
            try:
                # wait for an event (telemetry/request/ack) or until the current state expires
                try:
                    await asyncio.wait_for(self._task_event.wait(), None if wake_at is None else max(0.0, wake_at - time.time()))
                except asyncio.TimeoutError:
                    pass
                self._task_event.clear()

                # run the state machine until it settles; a state switch sets the event so the new state's handler runs immediately
                now = time.time()
                while True:
                    match self.state:
                        case DeviceState.OFFLINE:
                            wake_at = await self._state_offline(now)
                        case DeviceState.IDLE:
                            wake_at = self._state_idle(now)
                        case DeviceState.ACTIVE:
                            wake_at = self._state_active(now)
                        case DeviceState.PREPARE:
                            wake_at = self._state_prepare(now)
                        case DeviceState.BUSY:
                            wake_at = self._state_busy(now)
                    if not self._task_event.is_set():
                        break
                    self._task_event.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("device task %s", self.name)  # the task itself must never die

    async def _state_offline(self, now: float) -> float | None:
        # fresh telemetry after the offline moment proves the device is back; resend what it should be doing
        if self._last_telemetry > self._offline_since:
            if self.setpoint != 0 and self._requested is None:
                self._requested = self.setpoint  # device may have reset while away
            self._set_state(DeviceState.IDLE, now)
            return None

        # still unreachable: fire one recovery attempt and back off
        await self.recover_connection()
        wake_at = now + self._recover_backoff
        self._recover_backoff = min(2 * self._recover_backoff, RECOVER_BACKOFF_MAX_S)
        return wake_at

    def _state_idle(self, now: float) -> float | None:
        # an awake device that stopped reporting is lost
        if not self.asleep and self._stale(now):
            self._set_state(DeviceState.OFFLINE, now)
            return None

        # a request goes out immediately; this is also what wakes a sleeping device
        if self._requested is not None:
            self._send(self._requested, now)
            return None
        if self.asleep:
            return None  # silent by definition; nothing to watch until the next request

        # power-save after a minute of nothing to do; the driver decides what to do with the sleep flag
        if now - self._last_activity > SLEEP_AFTER_S:
            self.set_power(0, sleep=True)
            self.asleep = True
            return None
        return self._last_activity + SLEEP_AFTER_S

    def _state_active(self, now: float) -> float | None:
        # a running device that stopped reporting is lost
        if self._stale(now):
            self._set_state(DeviceState.OFFLINE, now)
            return None

        # a request goes out immediately
        if self._requested is not None:
            self._send(self._requested, now)
            return None
        return max(self._last_telemetry, self._last_activity) + STALE_AFTER_S

    def _state_prepare(self, now: float) -> float | None:
        # within the redirect window a new setpoint replaces the command in flight; the processing time does not restart
        if self._requested is not None:
            self._pending_target = self._requested
            self._requested = None
            self._acked = True  # the driver lowers this again when its transport acks
            self.setpoint = self._pending_target
            self.set_power(self._pending_target)

        # window over: the command is locked in
        window_end = self._pending_since + self.profile[self._pending_target < 0].redirect_window
        if now >= window_end:
            self._set_state(DeviceState.BUSY, now)
            return None
        return window_end

    def _state_busy(self, now: float) -> float | None:
        target = self._pending_target

        # off is always allowed, straight through a pending command
        if self._requested == 0 and target != 0:
            self._pending_target = None
            self._set_state(DeviceState.IDLE, now)
            return None

        # a device that stopped reporting is lost
        if self._stale(now):
            self._set_state(DeviceState.OFFLINE, now)
            return None

        # output reached the target: confirmed, and the measured processing time feeds the lag average
        actual = self.output_power()
        tolerance = max(DEADBAND_W, abs(target) // 10)
        if abs(actual - target) <= tolerance:
            self._lag_history.append(now - self._pending_since)
            del self._lag_history[:-LAG_HISTORY_SIZE]
            self._pending_target = None
            self._set_state(DeviceState.ACTIVE if self.setpoint != 0 else DeviceState.IDLE, now)
            return None

        # no receipt confirmation: resend once, then declare the transport dead
        if not self._acked:
            if now < self._pending_since + ACK_TIMEOUT_S:
                return self._pending_since + ACK_TIMEOUT_S
            if self._resent:
                self._set_state(DeviceState.OFFLINE, now)
                return None
            self._resent = True
            self._pending_since = now  # the resend restarts the cycle
            self.set_power(target)
            return now + ACK_TIMEOUT_S

        # timeout, or the output visibly responded and then flattened below target: learn the max at this temperature
        expected = sum(self._lag_history) / len(self._lag_history) if self._lag_history else LAG_BOOTSTRAP_S
        timeout_at = self._pending_since + TIMEOUT_FACTOR * expected
        responded = self._last_output_change > self._pending_since
        plateau = responded and now >= self._pending_since + expected and now - self._last_output_change >= PLATEAU_STABLE_S
        if now >= timeout_at or plateau:
            if responded and actual != 0 and abs(actual) < abs(target) - tolerance and self.temp._state is not None:
                floor = int(DERATE_FLOOR_PCT * abs(self.profile[target < 0].power))
                self._derate_curve[target < 0][str(int(self.temp.asNumber))] = max(abs(actual), floor)
            self._pending_target = None
            self._set_state(DeviceState.ACTIVE if self.setpoint != 0 else DeviceState.IDLE, now)
            return None
        return timeout_at

    def _send(self, target: int, now: float) -> None:
        # full bookkeeping for one outgoing command; drivers only implement the pure set_power
        self._requested = None
        self.asleep = False
        self._pending_target = target
        self._pending_since = now
        self._last_activity = now
        self._acked = True  # the driver lowers this again when its transport acks
        self._resent = False
        self.setpoint = target
        self.set_power(target)
        self._set_state(DeviceState.PREPARE if self.profile[target < 0].redirect_window > 0 else DeviceState.BUSY, now)
