"""Unit tests for the PowerDevice task state machine, driven directly via pump() with fake time."""

import asyncio

from custom_components.energymaster.device import ACK_TIMEOUT_S, SLEEP_AFTER_S, STALE_AFTER_S, DeviceState, PowerDevice, PowerProfileCharge, PowerProfileDischarge
from custom_components.energymaster.sensor import EmSensor

EmSensor.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


class _Sensor:
    def __init__(self, value: float) -> None:
        self.asNumber = value
        self._state = value


class FakeDevice(PowerDevice):
    def __init__(self, redirect_window: float = 0.0, temp: float | None = None) -> None:
        super().__init__(None, "dev", "dev", 5.0, PowerProfileDischarge(power=2400, redirect_window=redirect_window), PowerProfileCharge(power=-2400, redirect_window=redirect_window))
        for e in self.entities.values():
            e.hass = None
        self.soc = _Sensor(50.0)
        self.soc_min = _Sensor(10.0)
        self.soc_max = _Sensor(95.0)
        if temp is not None:
            self.temp = _Sensor(temp)
        self.sent: list[int] = []
        self.sleeps = 0
        self.recoveries = 0
        self.acks = False  # when True the fake transport expects a reply for each command

    def set_power(self, watts: int, sleep: bool = False) -> None:
        if sleep:
            self.sleeps += 1
        else:
            self.sent.append(watts)
        if self.acks:
            self._acked = False

    async def recover_connection(self) -> None:
        self.recoveries += 1


async def pump_device(dev: PowerDevice, now: float) -> None:
    """Test-side mirror of _device_loop's processing: run the state machine on simulated time until it settles."""
    while True:
        match dev.state:
            case DeviceState.OFFLINE:
                await dev._state_offline(now)
            case DeviceState.IDLE:
                dev._state_idle(now)
            case DeviceState.ACTIVE:
                dev._state_active(now)
            case DeviceState.PREPARE:
                dev._state_prepare(now)
            case DeviceState.BUSY:
                dev._state_busy(now)
        if not dev._task_event.is_set():
            return
        dev._task_event.clear()


def pump(dev: PowerDevice, now: float) -> None:
    asyncio.run(pump_device(dev, now))


def test_command_cycle_learns_lag() -> None:
    d = FakeDevice()
    d.on_telemetry(0, 100.0)
    assert d.request_power(1000) == 1000
    pump(d, 100.0)
    assert d.sent == [1000] and d.state == DeviceState.BUSY
    d.on_telemetry(950, 104.0)  # within max(20W, 10%) of the target
    pump(d, 104.0)
    assert d.state == DeviceState.ACTIVE
    assert d._lag_history == [4.0]


def test_confirm_to_idle_when_off() -> None:
    d = FakeDevice()
    d.on_telemetry(0, 100.0)
    d.request_power(1000)
    pump(d, 100.0)
    d.on_telemetry(1000, 103.0)
    pump(d, 103.0)
    d.request_power(0)
    pump(d, 110.0)
    d.on_telemetry(0, 112.0)
    pump(d, 112.0)
    assert d.state == DeviceState.IDLE and d.setpoint == 0


def test_request_clamps_and_deadband() -> None:
    d = FakeDevice()
    assert d.request_power(5000) == 2400  # profile clamp
    d._requested = None
    d.setpoint = 1000
    assert d.request_power(1010) == 1000  # within deadband: keep the current setpoint
    assert d._requested is None
    d.soc = _Sensor(10.0)
    assert d.request_power(500) == 0  # empty battery cannot discharge
    d2 = FakeDevice()
    d2.soc = _Sensor(95.0)
    assert d2.request_power(-500) == 0  # full battery cannot charge


def test_zero_passes_through_busy() -> None:
    d = FakeDevice()
    d.on_telemetry(0, 100.0)
    d.request_power(1000)
    pump(d, 100.0)
    assert d.state == DeviceState.BUSY
    d.request_power(0)
    pump(d, 101.0)
    assert d.sent == [1000, 0]  # off went straight through the pending command


def test_plateau_derates_at_temperature() -> None:
    d = FakeDevice(temp=45.0)
    d.on_telemetry(0, 100.0)
    d.request_power(2400)
    pump(d, 100.0)
    d.on_telemetry(2000, 101.0)
    pump(d, 101.0)
    assert d.state == DeviceState.BUSY  # below target, not yet concluded
    d.on_telemetry(2000, 116.0)  # unchanged since 101: plateau after 1x bootstrap lag
    pump(d, 116.0)
    assert d.state == DeviceState.ACTIVE
    assert d._derate_curve[False]["45"] == 2000
    assert d.request_power(2400) == 2000  # the promise is capped at this temperature; the command itself stays at max
    assert d._requested is None  # setpoint already at max: no new command needed
    d.temp = _Sensor(40.0)
    assert d.request_power(2400) == 2400  # cooler bin has no entry: full promise again


def test_derate_floor_rejects_implausible_observation() -> None:
    d = FakeDevice(temp=45.0)
    d.on_telemetry(0, 100.0)
    d.request_power(2400)
    pump(d, 100.0)
    d.on_telemetry(300, 101.0)  # implausibly low: a real derating never cuts more than 25%
    d.on_telemetry(300, 116.0)
    pump(d, 116.0)
    assert d._derate_curve[False]["45"] == 1800  # floored at 75% of rated power


def test_no_response_does_not_derate() -> None:
    d = FakeDevice(temp=45.0)
    d.on_telemetry(800, 100.0)
    d.request_power(2400)
    pump(d, 100.0)
    pump(d, 131.0)  # 2x bootstrap lag passed, output never changed after the send
    assert d.state == DeviceState.ACTIVE
    assert d._derate_curve[False] == {}  # mid-processing look-alike must not teach a limit


def test_unset_temp_sensor_does_not_derate() -> None:
    d = FakeDevice()  # temp stays the Empty sentinel (never reported)
    d.on_telemetry(0, 100.0)
    d.request_power(2400)
    pump(d, 100.0)
    d.on_telemetry(1500, 101.0)
    d.on_telemetry(1500, 116.0)
    pump(d, 116.0)
    assert d._derate_curve == [{}, {}]
    d._requested = None
    assert d.request_power(2400) == 2400


def test_telemetry_evidence_raises_curve() -> None:
    d = FakeDevice(temp=45.0)
    d._derate_curve[False]["45"] = 1500
    d.on_telemetry(1800, 100.0)
    assert d._derate_curve[False]["45"] == 1800


def test_redirect_within_window() -> None:
    d = FakeDevice(redirect_window=3.0)
    d.on_telemetry(0, 100.0)
    d.request_power(1000)
    pump(d, 100.0)
    assert d.state == DeviceState.PREPARE
    d.request_power(1500)
    pump(d, 101.0)
    assert d.sent == [1000, 1500] and d.state == DeviceState.PREPARE  # replaced in flight
    pump(d, 103.5)
    assert d.state == DeviceState.BUSY
    d.on_telemetry(1500, 105.0)
    pump(d, 105.0)
    assert d.state == DeviceState.ACTIVE
    assert d._lag_history == [5.0]  # measured from the first send


def test_missing_ack_resends_then_offline() -> None:
    d = FakeDevice()
    d.acks = True
    d.on_telemetry(0, 100.0)
    d.request_power(1000)
    pump(d, 100.0)
    pump(d, 100.0 + ACK_TIMEOUT_S + 1)
    assert d.sent == [1000, 1000]  # one resend
    pump(d, 100.0 + 2 * ACK_TIMEOUT_S + 2)
    assert d.state == DeviceState.OFFLINE
    assert d.recoveries == 1  # entering OFFLINE immediately attempts recovery
    d.on_telemetry(0, 120.0)  # contact restored
    pump(d, 120.0)
    assert d.state == DeviceState.BUSY and d.sent[-1] == 1000  # setpoint resent


def test_ack_received_prevents_resend() -> None:
    d = FakeDevice()
    d.acks = True
    d.on_telemetry(0, 100.0)
    d.request_power(1000)
    pump(d, 100.0)
    d.command_reply()
    pump(d, 100.0 + ACK_TIMEOUT_S + 1)
    assert d.sent == [1000] and d.state == DeviceState.BUSY  # acked: waiting on telemetry, no resend


def test_sleep_after_idle_and_wake() -> None:
    d = FakeDevice()
    d.on_telemetry(0, 100.0)
    d._last_activity = 100.0
    pump(d, 100.0)
    assert d.sleeps == 0
    d.on_telemetry(0, 100.0 + SLEEP_AFTER_S + 1)
    pump(d, 100.0 + SLEEP_AFTER_S + 1)
    assert d.sleeps == 1 and d.asleep
    d.request_power(500)
    pump(d, 200.0)
    assert d.sent == [500] and not d.asleep and d.state == DeviceState.BUSY


def test_stale_goes_offline_and_resends_setpoint() -> None:
    d = FakeDevice()
    d.on_telemetry(0, 100.0)
    d.request_power(500)
    pump(d, 100.0)
    d.on_telemetry(500, 103.0)
    pump(d, 103.0)
    assert d.state == DeviceState.ACTIVE
    pump(d, 103.0 + STALE_AFTER_S + 1)
    assert d.state == DeviceState.OFFLINE and d.recoveries == 1
    d.on_telemetry(500, 300.0)
    pump(d, 300.0)
    assert d.sent == [500, 500]  # setpoint resent after reconnect
    assert d.state == DeviceState.ACTIVE  # still delivering 500: the resend confirms instantly
