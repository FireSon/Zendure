import asyncio

from custom_components.energymaster.distribution import Distribution, DistributionMode
from custom_components.energymaster.device import PowerDevice, PowerProfileCharge, PowerProfileDischarge


class _Sensor:
    def __init__(self, value: float) -> None:
        self.asNumber = value


class StubDevice(PowerDevice):
    # Real PowerDevice with __init__ bypassed so the test needs no HA. Only the attributes
    # _process_p1 reads are set; on_setpoint records what was commanded.
    def __init__(self, device_id: str, soc: float, power: int = 2300, capacity_kwh: float = 10.0, output: float = 5.0) -> None:
        self.device_id = device_id
        self.profile = [PowerProfileDischarge(power=power), PowerProfileCharge(power=power)]
        self.soc = _Sensor(soc)
        self.soc_min = _Sensor(10.0)
        self.soc_max = _Sensor(100.0)
        self.solar = _Sensor(0.0)
        self.battery_output = _Sensor(output)
        self.capacity_kwh = capacity_kwh
        self.setpoint = 0
        self.score = [0.0, 0.0]
        self.inefficient_time = 0.0
        self.commanded: int | None = None

    def output_power(self, now: float) -> int:
        return 0

    async def on_setpoint(self, watts: int, p, now: float) -> int:
        self.commanded = watts
        self.setpoint = watts
        return watts


def _dist(devices: list[StubDevice], mode: DistributionMode, max_grid: int = 0) -> Distribution:
    d = Distribution.__new__(Distribution)
    d.devices = list(devices)
    d._mode = mode
    d.manual_setpoint = 0
    d.max_grid = max_grid
    d.setpoint = 0
    d.solar = []
    d._power_reselect = 0.0
    d.charge_order = []
    d.discharge_order = []
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


# --- live build + dispatch (no solver run: _process_p1 builds the order from SoC/wear) ---

def test_live_discharge_prefers_fuller() -> None:
    full = StubDevice("full", soc=90.0)
    empty = StubDevice("empty", soc=20.0)
    dist = _dist([empty, full], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert full.commanded is not None and full.commanded > 0   # fuller unit discharges
    assert empty.commanded == 0


def test_live_charge_prefers_emptier() -> None:
    full = StubDevice("full", soc=90.0)
    empty = StubDevice("empty", soc=20.0)
    dist = _dist([full, empty], DistributionMode.MATCHING_CHARGE)
    asyncio.run(dist._process_p1(-2000, now=1000.0))
    assert empty.commanded is not None and empty.commanded < 0  # emptier unit charges
    assert full.commanded == 0


def test_full_battery_not_charged() -> None:
    full = StubDevice("full", soc=100.0)
    dist = _dist([full], DistributionMode.MATCHING_CHARGE)
    asyncio.run(dist._process_p1(-2000, now=1000.0))
    assert full.commanded == 0   # no charge headroom -> score 0 -> not in the order


def test_prebuilt_order_is_used() -> None:
    a = StubDevice("a", soc=80.0)
    b = StubDevice("b", soc=80.0)
    a.score = [2.0, 0.0]
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist.discharge_order = [a]   # solver plan present -> no live rebuild, b is not a candidate
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert a.commanded is not None and a.commanded > 0
    assert b.commanded == 0


def test_off_mode_does_not_dispatch() -> None:
    dev = StubDevice("d", soc=50.0)
    dist = _dist([dev], DistributionMode.OFF)
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert dev.commanded is None


def test_peak_shaving_clamps_to_max_grid() -> None:
    dev = StubDevice("d", soc=80.0)
    dist = _dist([dev], DistributionMode.PEAK_SHAVING, max_grid=1000)
    asyncio.run(dist._process_p1(3000, now=1000.0))
    assert dist.setpoint == 2000   # only the excess above max_grid (3000 - 1000)


def test_deadband_does_not_dispatch() -> None:
    dev = StubDevice("d", soc=50.0)
    dist = _dist([dev], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(10, now=1000.0))
    assert dev.commanded is None


# --- sort order of the live-built fleet ---

def test_discharge_order_ranks_by_soc() -> None:
    a = StubDevice("a", soc=30.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=90.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(1500, now=1000.0))
    assert c.score[False] > b.score[False] > a.score[False]   # fuller ranks higher for discharge


def test_charge_order_ranks_by_soc() -> None:
    a = StubDevice("a", soc=30.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=90.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_CHARGE)
    asyncio.run(dist._process_p1(-1500, now=1000.0))
    assert a.score[True] > b.score[True] > c.score[True]      # emptier ranks higher for charge


def test_engages_top_scored_first() -> None:
    a = StubDevice("a", soc=20.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=95.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(4000, now=1000.0))   # needs two units
    assert c.commanded and c.commanded > 0            # two fullest engage
    assert b.commanded and b.commanded > 0
    assert a.commanded == 0                            # emptiest stays off


def test_wear_balance_prefers_less_cycled() -> None:
    fresh = StubDevice("fresh", soc=70.0, output=2.0)
    worn = StubDevice("worn", soc=70.0, output=20.0)
    dist = _dist([worn, fresh], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert fresh.score[False] > worn.score[False]      # equal SoC -> less-cycled ranks higher
    assert fresh.commanded and fresh.commanded > 0
    assert worn.commanded == 0
