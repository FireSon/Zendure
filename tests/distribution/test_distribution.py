import asyncio

from custom_components.energymaster.distribution import START_W, Distribution, DistributionMode
from custom_components.energymaster.device import PowerDevice, PowerProfileCharge, PowerProfileDischarge


class _Sensor:
    def __init__(self, value: float) -> None:
        self.asNumber = value


class StubDevice(PowerDevice):
    # Real PowerDevice with __init__ bypassed so the test needs no HA. Only the attributes
    # the dispatch reads are set; on_setpoint records what was commanded.
    def __init__(self, device_id: str, soc: float, power: int = 2300, capacity_kwh: float = 10.0, throughput_kwh: float = 0.0) -> None:
        self.device_id = device_id
        self.profile = [PowerProfileDischarge(power=power), PowerProfileCharge(power=power)]
        self.soc = _Sensor(soc)
        self.soc_min = _Sensor(10.0)
        self.soc_max = _Sensor(100.0)
        self.solar = _Sensor(0.0)
        self.battery_input_aggr = _Sensor(0.0)
        self.battery_output_aggr = _Sensor(throughput_kwh)
        self.capacity_kwh = capacity_kwh
        self.setpoint = 0
        self.score = [0.0, 0.0]
        self._wear_snapshots: list = []
        self.commanded: int | None = None
        self.output = 0

    def output_power(self, now: float) -> int:
        return self.output

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


# --- dispatch (no solver run: _process_p1 builds order + gewichten lazily) ---

def test_discharge_prefers_fuller() -> None:
    full = StubDevice("full", soc=90.0)
    empty = StubDevice("empty", soc=20.0)
    dist = _dist([empty, full], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert full.commanded is not None and full.commanded > 0  # fuller unit discharges
    assert empty.commanded is None  # idle en nooit gecommandeerd -> geen 0-spam


def test_charge_prefers_emptier() -> None:
    full = StubDevice("full", soc=90.0)
    empty = StubDevice("empty", soc=20.0)
    dist = _dist([full, empty], DistributionMode.MATCHING_CHARGE)
    asyncio.run(dist._process_p1(-2000, now=1000.0))
    assert empty.commanded is not None and empty.commanded < 0  # emptier unit charges
    assert full.commanded is None


def test_full_battery_not_charged() -> None:
    full = StubDevice("full", soc=100.0)
    dist = _dist([full], DistributionMode.MATCHING_CHARGE)
    asyncio.run(dist._process_p1(-2000, now=1000.0))
    assert full.commanded is None  # no charge headroom -> score 0 -> not in the order


def test_prebuilt_order_is_used() -> None:
    a = StubDevice("a", soc=80.0)
    b = StubDevice("b", soc=80.0)
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist.discharge_order = [a]  # solver plan present -> no rebuild, b is not a candidate
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert a.commanded is not None and a.commanded > 0
    assert b.commanded is None


def test_off_mode_does_not_dispatch() -> None:
    dev = StubDevice("d", soc=50.0)
    dist = _dist([dev], DistributionMode.OFF)
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert dev.commanded is None


def test_peak_shaving_clamps_to_max_grid() -> None:
    dev = StubDevice("d", soc=80.0)
    dist = _dist([dev], DistributionMode.PEAK_SHAVING, max_grid=1000)
    asyncio.run(dist._process_p1(3000, now=1000.0))
    assert dist.setpoint == 2000  # only the excess above max_grid (3000 - 1000)


def test_deadband_does_not_dispatch() -> None:
    dev = StubDevice("d", soc=50.0)
    dist = _dist([dev], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(10, now=1000.0))
    assert dev.commanded is None


# --- order en gewichten per window ---

def test_discharge_order_ranks_by_soc() -> None:
    a = StubDevice("a", soc=30.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=90.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(1500, now=1000.0))
    assert c.score[False] > b.score[False] > a.score[False]  # fuller ranks higher for discharge


def test_charge_order_ranks_by_soc() -> None:
    a = StubDevice("a", soc=30.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=90.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_CHARGE)
    asyncio.run(dist._process_p1(-1500, now=1000.0))
    assert a.score[True] > b.score[True] > c.score[True]  # emptier ranks higher for charge


def test_second_unit_joins_via_make_before_break() -> None:
    a = StubDevice("a", soc=20.0)
    b = StubDevice("b", soc=60.0)
    c = StubDevice("c", soc=95.0)
    dist = _dist([a, b, c], DistributionMode.MATCHING_DISCHARGE)
    asyncio.run(dist._process_p1(4000, now=1000.0))  # koude start: c direct actief
    asyncio.run(dist._process_p1(4000, now=1001.0))  # tekort 1815 W -> add-integraal 1815 Ws
    asyncio.run(dist._process_p1(4000, now=1002.0))  # integraal 3630 Ws >= 2000 -> b stapt in op START_W
    assert c.commanded is not None and c.commanded > START_W  # fullest carries the load
    assert b.commanded == START_W  # make-before-break instap
    assert a.commanded is None  # emptiest stays off


def test_wear_balance_prefers_less_cycled() -> None:
    fresh = StubDevice("fresh", soc=70.0, throughput_kwh=2.0)
    worn = StubDevice("worn", soc=70.0, throughput_kwh=20.0)
    dist = _dist([worn, fresh], DistributionMode.MATCHING_DISCHARGE)
    fresh._wear_snapshots = [[0.0, 0.0]]  # 7-dagen-baseline op 0 kWh
    worn._wear_snapshots = [[0.0, 0.0]]
    asyncio.run(dist._process_p1(2000, now=1000.0))
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
    # fase 1: integraal loopt vol (12 ticks x 1080 Ws tekort onder cap_low 1380) maar de cooldown blokkeert de drop
    dist._last_add = 999.0
    for i in range(12):
        asyncio.run(dist._process_p1(300, now=1000.0 + i))
    assert len(dist._active) == 2
    # fase 2: cooldown verlopen -> zelfde onderschrijding dropt de laagst gerankte
    dist._last_add = 0.0
    dist._drop_ws = 0.0
    for i in range(12):
        asyncio.run(dist._process_p1(300, now=2000.0 + i))
    assert dist._active == [a]
    assert b.commanded == 0


def test_direction_change_needs_integral() -> None:
    a = StubDevice("a", soc=60.0)
    dist = _dist([a], DistributionMode.MATCHING)
    dist.discharge_order = [a]
    dist.charge_order = [a]
    dist._active = [a]
    a.setpoint = 800  # ontlaadt
    asyncio.run(dist._process_p1(-2000, now=1000.0))  # richtingwissel-integraal 2000 < 3000: alleen afbouwen
    assert a.commanded == 560  # ramp naar 0 met f_down (800 - 800*0.3)
    assert dist._active == [a] and dist._charging is False
    asyncio.run(dist._process_p1(-2000, now=1001.0))  # integraal 4000 >= 3000: wissel naar laden
    assert dist._charging is True
    assert a.commanded is not None and a.commanded < 0  # zelfde device laadt nu op


def test_shares_follow_weights_within_band() -> None:
    a = StubDevice("a", soc=80.0)
    b = StubDevice("b", soc=80.0)
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist.discharge_order = [a, b]
    dist._active = [a, b]
    dist._weights[False] = {"a": 6.0, "b": 2.0}
    asyncio.run(dist._process_p1(2000, now=1000.0))
    assert a.commanded == 1050  # naar rato 6/8 van 2000 = 1500, geramp't met f_up 0.7
    assert b.commanded == 350  # restant 500, geramp't
    # zware weging: a wordt geclampt op band_high (2185), b vangt het restant
    dist2 = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    a.setpoint = b.setpoint = 0
    dist2.discharge_order = [a, b]
    dist2._active = [a, b]
    dist2._weights[False] = {"a": 100.0, "b": 1.0}
    asyncio.run(dist2._process_p1(3000, now=1000.0))
    assert a.commanded == 1529  # band_high 2185 geramp't
    # kleine weging: eerste device wordt opgetild naar de harde 10%-vloer (230)
    dist3 = _dist([b, a], DistributionMode.MATCHING_DISCHARGE)
    a.setpoint = b.setpoint = 0
    dist3.discharge_order = [b, a]
    dist3._active = [b, a]
    dist3._weights[False] = {"b": 0.1, "a": 10.0}
    asyncio.run(dist3._process_p1(2000, now=1000.0))
    assert b.commanded == 161  # floor 230 geramp't


def test_window_rotation_swaps_via_make_before_break() -> None:
    a = StubDevice("a", soc=90.0)
    b = StubDevice("b", soc=60.0)
    dist = _dist([a, b], DistributionMode.MATCHING_DISCHARGE)
    dist._active = [b]
    b.setpoint = 500  # b draagt de last van het vorige window
    dist._build_orders(1000.0)  # nieuwe kop is a -> rotatie via make-before-break
    assert dist._incoming is a and dist._swap_out is b
    asyncio.run(dist._process_p1(500, now=1000.0))
    assert a.commanded == START_W  # instap-probe
    a.output = START_W  # de instapper levert echt
    asyncio.run(dist._process_p1(500, now=1001.0))
    assert dist._active == [a] and dist._incoming is None and dist._swap_out is None
    assert b.commanded == 0
