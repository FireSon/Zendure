import asyncio

from custom_components.energymaster.distribution import START_W, Distribution, DistributionMode
from custom_components.energymaster.device import PowerDevice, PowerProfileCharge, PowerProfileDischarge
from custom_components.energymaster.sensor import EmSensor
from tests.distribution.test_device_task import pump_device

EmSensor.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


class _Sensor:
    def __init__(self, value: float) -> None:
        self.asNumber = value


class StubDevice(PowerDevice):
    # Real PowerDevice (hass=None); zero lag seeded so every command settles within the same pump.
    # set_power records what was commanded; `output` is fed as telemetry each tick.
    def __init__(self, device_id: str, soc: float, power: int = 2300, capacity_kwh: float = 10.0, throughput_kwh: float = 0.0) -> None:
        super().__init__(None, device_id, device_id, capacity_kwh, PowerProfileDischarge(power=power), PowerProfileCharge(power=power))
        for e in self.entities.values():
            e.hass = None
        self.soc = _Sensor(soc)
        self.soc_min = _Sensor(10.0)
        self.soc_max = _Sensor(100.0)
        self.solar = _Sensor(0.0)
        self.battery_input_aggr = _Sensor(0.0)
        self.battery_output_aggr = _Sensor(throughput_kwh)
        self._lag_history = [0.0]  # zero lag: the busy timeout settles instantly, the device is always free again
        self.commanded: int | None = None
        self.output = 0

    def set_power(self, watts: int, sleep: bool = False) -> None:
        if not sleep:  # a sleep hint is not a power command
            self.commanded = watts


def tick(dist: Distribution, p1: int, now: float) -> None:
    # one live tick: telemetry in, dispatch, then every device task pumps with the same clock
    async def run() -> None:
        for d in dist.devices:
            d.on_telemetry(d.output, now)
        await dist._process_p1(p1, now=now)
        for d in dist.devices:
            await pump_device(d, now)
    asyncio.run(run())


class _ModeStub:
    # stand-in for the mode EmSelect: _process_p1 only reads .asInt
    def __init__(self, value: int) -> None:
        self._state = int(value)

    @property
    def asInt(self) -> int:
        return self._state


def _dist(devices: list[StubDevice], mode: DistributionMode, max_grid: int = 0) -> Distribution:
    d = Distribution.__new__(Distribution)
    d.devices = list(devices)
    d.mode = _ModeStub(mode.value)
    d.manual_setpoint = 0
    d.max_grid = max_grid
    d.setpoint = 0
    d.solar = []
    d.charge_order = []
    d.discharge_order = []
    d._weights = {False: {}, True: {}}
    d._active = []
    d._incoming = d._swap_out = None
    d._charging = False
    d._add_ws = d._drop_ws = d._direction_ws = 0.0
    d._last_add = 0.0
    d._last_tick = 0.0
    _attach_stats(d)
    return d


class _Stat:
    asNumber = 0.0
    def update_value(self, value) -> bool:
        return True


def _attach_stats(d: Distribution) -> None:
    # display sensors _process_p1 reads/writes; the dispatch logic under test ignores them
    for a in ("capacity_kwh", "available_kwh", "grid_import", "grid_export", "solar_power", "discharge_power", "charge_power", "house_load", "soc", "autarky"):
        setattr(d, a, _Stat())


# --- dispatch (no solver run: _process_p1 builds order + weights lazily) ---

def test_discharge_prefers_fuller() -> None:
    full = StubDevice("full", soc=90.0)
    empty = StubDevice("empty", soc=20.0)
    dist = _dist([empty, full], DistributionMode.MATCHING_DISCHARGE)
    tick(dist, 2000, 1000.0)
    assert full.commanded is not None and full.commanded > 0  # fuller unit discharges
    assert empty.commanded is None  # idle and never commanded -> no 0-spam


def test_charge_prefers_emptier() -> None:
    full = StubDevice("full", soc=90.0)
    empty = StubDevice("empty", soc=20.0)
    dist = _dist([full, empty], DistributionMode.MATCHING_CHARGE)
    tick(dist, -2000, 1000.0)
    assert empty.commanded is not None and empty.commanded < 0  # emptier unit charges
    assert full.commanded is None


def test_full_battery_not_charged() -> None:
    full = StubDevice("full", soc=100.0)
    dist = _dist([full], DistributionMode.MATCHING_CHARGE)
    tick(dist, -2000, 1000.0)
    assert full.commanded is None  # no charge headroom -> score 0 -> not in the order


def test_prebuilt_order_is_used() -> None:
    a = StubDevice("a", soc=80.0)
    b = StubDevice("b", soc=80.0)
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist.discharge_order = [a]  # solver plan present -> no rebuild, b is not a candidate
    tick(dist, 2000, 1000.0)
    assert a.commanded is not None and a.commanded > 0
    assert b.commanded is None


def test_off_mode_does_not_dispatch() -> None:
    dev = StubDevice("d", soc=50.0)
    dist = _dist([dev], DistributionMode.OFF)
    tick(dist, 2000, 1000.0)
    assert dev.commanded is None


def test_peak_shaving_clamps_to_max_grid() -> None:
    dev = StubDevice("d", soc=80.0)
    dist = _dist([dev], DistributionMode.PEAK_SHAVING, max_grid=1000)
    tick(dist, 3000, 1000.0)
    assert dist.setpoint == 2000  # only the excess above max_grid (3000 - 1000)


def test_deadband_does_not_dispatch() -> None:
    dev = StubDevice("d", soc=50.0)
    dist = _dist([dev], DistributionMode.MATCHING_DISCHARGE)
    tick(dist, 10, 1000.0)
    assert dev.commanded is None


# --- order and weights per window ---

def test_discharge_order_ranks_by_soc() -> None:
    a = StubDevice("a", soc=30.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=90.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_DISCHARGE)
    tick(dist, 1500, 1000.0)
    assert c.score[False] > b.score[False] > a.score[False]  # fuller ranks higher for discharge


def test_charge_order_ranks_by_soc() -> None:
    a = StubDevice("a", soc=30.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=90.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_CHARGE)
    tick(dist, -1500, 1000.0)
    assert a.score[True] > b.score[True] > c.score[True]  # emptier ranks higher for charge


def test_second_unit_joins_via_make_before_break() -> None:
    a = StubDevice("a", soc=20.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=95.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_DISCHARGE)
    tick(dist, 4000, 1000.0)  # cold start: c active immediately
    tick(dist, 4000, 1001.0)  # shortfall 1815 W -> add integral 1815 Ws
    tick(dist, 4000, 1002.0)  # integral 3630 Ws >= 2000 -> b enters at START_W
    assert c.commanded is not None and c.commanded > START_W  # fullest carries the load
    assert b.commanded == START_W  # make-before-break instap
    assert a.commanded is None  # emptiest stays off


def test_wear_balance_prefers_less_cycled() -> None:
    fresh = StubDevice("fresh", soc=70.0, throughput_kwh=2.0)
    worn = StubDevice("worn", soc=70.0, throughput_kwh=20.0)
    dist = _dist([worn, fresh], DistributionMode.MATCHING_DISCHARGE)
    fresh._wear_snapshots = [[0.0, 0.0]]  # 7-dagen-baseline op 0 kWh
    worn._wear_snapshots = [[0.0, 0.0]]
    tick(dist, 2000, 1000.0)
    assert fresh.score[False] > worn.score[False]  # equal SoC -> less-cycled ranks higher
    assert fresh.commanded is not None and fresh.commanded > 0
    assert worn.commanded is None


# --- integralen, verdeling en rotatie ---

def test_drop_needs_integral_and_cooldown() -> None:
    a = StubDevice("a", soc=90.0)
    b = StubDevice("b", soc=60.0)
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist.discharge_order = [a, b]
    dist._active = [a, b]
    # phase 1: the integral fills (12 ticks x 1080 Ws below cap_low 1380) but the cooldown blocks the drop
    dist._last_add = 999.0
    for i in range(12):
        tick(dist, 300, 1000.0 + i)
    assert len(dist._active) == 2
    # phase 2: cooldown expired -> the same shortfall drops the lowest-ranked device
    dist._last_add = 0.0
    dist._drop_ws = 0.0
    for i in range(12):
        tick(dist, 300, 2000.0 + i)
    assert dist._active == [a]
    assert b.commanded == 0


def test_direction_change_needs_integral() -> None:
    a = StubDevice("a", soc=60.0)
    dist = _dist([a], DistributionMode.MATCHING)
    dist.discharge_order = [a]
    dist.charge_order = [a]
    dist._active = [a]
    a.setpoint = 800  # ontlaadt
    tick(dist, -2000, 1000.0)  # direction integral 2000 < 3000: only ramp down
    assert a.commanded == 560  # ramp toward 0 with f_down (800 - 800*0.3)
    assert dist._active == [a] and dist._charging is False
    tick(dist, -2000, 1001.0)  # integral 4000 >= 3000: switch to charging
    assert dist._charging is True
    assert a.commanded is not None and a.commanded < 0  # zelfde device laadt nu op


def test_shares_follow_weights_within_band() -> None:
    a = StubDevice("a", soc=80.0)
    b = StubDevice("b", soc=80.0)
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist.discharge_order = [a, b]
    dist._active = [a, b]
    dist._weights[False] = {"a": 6.0, "b": 2.0}
    tick(dist, 2000, 1000.0)
    assert a.commanded == 1050  # pro rata 6/8 of 2000 = 1500, ramped with f_up 0.7
    assert b.commanded == 350  # restant 500, geramp't
    # heavy weight: a is clamped at band_high (2185), b catches the remainder
    dist2 = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    a.setpoint = b.setpoint = 0
    dist2.discharge_order = [a, b]
    dist2._active = [a, b]
    dist2._weights[False] = {"a": 100.0, "b": 1.0}
    tick(dist2, 3000, 1000.0)
    assert a.commanded == 1529  # band_high 2185 geramp't
    # tiny weight: the first device is lifted to the hard 10% floor (230)
    dist3 = _dist([b, a], DistributionMode.MATCHING_DISCHARGE)
    a.setpoint = b.setpoint = 0
    dist3.discharge_order = [b, a]
    dist3._active = [b, a]
    dist3._weights[False] = {"b": 0.1, "a": 10.0}
    tick(dist3, 2000, 1000.0)
    assert b.commanded == 161  # floor 230 geramp't


def test_window_rotation_swaps_via_make_before_break() -> None:
    a = StubDevice("a", soc=90.0)
    b = StubDevice("b", soc=60.0)
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist._active = [b]
    b.setpoint = 500  # b carries the load from the previous window
    dist._build_orders(1000.0)  # nieuwe kop is a -> rotatie via make-before-break
    assert dist._incoming is a and dist._swap_out is b
    tick(dist, 500, 1000.0)
    assert a.commanded == START_W  # entry probe
    a.output = START_W  # the entrant actually delivers
    tick(dist, 500, 1001.0)
    assert dist._active == [a] and dist._incoming is None and dist._swap_out is None
    assert b.commanded == 0
