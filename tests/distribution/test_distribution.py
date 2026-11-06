"""Distribution unit tests: fleet dispatch over contract fakes with simulated time."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import fake_hass
import custom_components.zenmaster.distribution as distribution_module
from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.const import Phase
from custom_components.zenmaster.device import DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge
from custom_components.zenmaster.distribution import Distribution, DistributionMode
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.solver.battery import DistributionEntry
from custom_components.zenmaster.solver.solver import LpSolver, SolverEntry
from custom_components.zenmaster.switch import HaSwitch
from custom_components.zenmaster.sensor import HaSensor

for cls in (HaSensor, HaNumber, HaBinarySensor, HaSelect, HaSwitch):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


class FakeDevice(DistributionDevice):
    # contract fake: stores the clamped command, returns the current power_actual
    def __init__(self, device_id: str, soc: float, power: int = 2400, kwh: float = 10.0) -> None:
        super().__init__(fake_hass(), device_id, [ProfileDischarge(power), ProfileCharge(-power)])
        self.soc = soc
        self.kwh = self.kwh = kwh
        self.state = DeviceState.ACTIVE  # under our control; EXTERNAL is excluded from the order

    def power(self, watts: int) -> int:
        self.power_setpoint = max(self.power_profile[1].power, min(self.power_profile[0].power, int(watts)))
        return self.power_actual

    def power_off(self) -> None:
        self.power_setpoint = 0
        self.state = DeviceState.EXTERNAL


def make_distribution(devices: list[FakeDevice], mode: DistributionMode = DistributionMode.SMART) -> Distribution:
    hass = fake_hass()
    dist = Distribution(hass, "entry")
    dist.solver_entry = DistributionEntry(dist)  # the plan side of the fleet, wired the way the coordinator does it
    dist.devices = list(devices)
    dist.wear_date = float("inf")
    dist.mode = mode.value
    for e in dist.entities.values():
        e.hass = None
    return dist


def tick(dist: Distribution, demand: int, now: float) -> None:
    # one live tick: P1 shows what the fleet does not yet cover, then every device follows its setpoint perfectly
    p1 = demand - sum(d.power_actual for d in dist.devices)
    asyncio.run(dist._process_p1(int(p1), dist.power, now))
    for d in dist.devices:
        d.power_actual = d.power_setpoint


def run(dist: Distribution, demand: int, start: float, ticks: int) -> float:
    now = start
    for _ in range(ticks):
        now += 1.0
        tick(dist, demand, now)
    return now


def test_the_restored_optimize_switch_reaches_the_flag() -> None:
    dist = make_distribution([FakeDevice("a", 50.0)])
    switch = dist.entities["optimize"]
    dist.device_load({switch._attr_unique_id: "on"})
    assert dist.optimize is True, "the switch hangs on this device, so its restored state must land on the flag entry_postrun reads"


def test_single_device_takes_the_demand() -> None:
    d1, d2 = FakeDevice("d1", 60.0), FakeDevice("d2", 30.0)
    dist = make_distribution([d1, d2])
    run(dist, 500, 1000.0, 10)
    assert 480 <= d1.power_setpoint <= 500  # highest SoC leads the discharge order
    assert d2.power_setpoint == 0


def test_a_self_regulating_device_is_not_counted_as_fleet_output() -> None:
    steered, external = FakeDevice("steered", 60.0), FakeDevice("external", 60.0)
    external.state = DeviceState.EXTERNAL  # regulates itself, so its power is already in the meter reading
    dist = make_distribution([steered, external])
    now = 1000.0
    for _ in range(15):
        now += 1.0
        external.power_actual = 500  # keeps delivering on its own decision
        asyncio.run(dist._process_p1(int(800 - steered.power_actual - external.power_actual), dist.power, now))
        steered.power_actual = steered.power_setpoint
    assert 280 <= steered.power_setpoint <= 300, "the steered device covers only what the meter still shows, not the external 500 W as well"
    assert external.power_setpoint == 0, "an external device gets no command"


def test_deadband_does_not_dispatch() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1])
    run(dist, 10, 1000.0, 5)
    assert d1.power_setpoint == 0


def test_dead_zone_drives_the_fleet_to_zero() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1])
    run(dist, 500, 1000.0, 10)
    assert d1.power_setpoint > 400
    run(dist, 30, 1010.0, 20)  # inside +/-RANGE_MIN: setpoint 0 in the current direction
    assert d1.power_setpoint == 0
    assert dist.charging is False


def test_off_mode_does_not_dispatch() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1], DistributionMode.OFF)
    run(dist, 2000, 1000.0, 5)
    assert d1.power_setpoint == 0


def test_mode_off_releases_all_devices() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1])
    run(dist, 500, 1000.0, 5)
    assert d1.power_setpoint > 0
    asyncio.run(dist.update_mode(SimpleNamespace(value=0), None))
    assert d1.power_setpoint == 0 and d1.state == DeviceState.EXTERNAL


def test_manual_mode_uses_fixed_setpoint() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1], DistributionMode.MANUAL)
    dist.manual_power = 700
    run(dist, 100, 1000.0, 10)
    assert 650 <= d1.power_setpoint <= 700


def test_mode_clamp_discharge_only() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1], DistributionMode.SMART_DISCHARGING)
    run(dist, -1500, 1000.0, 10)  # export: a discharge-only fleet must not charge
    assert d1.power_setpoint == 0


def test_mode_clamp_charge_only() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1], DistributionMode.SMART_CHARGING)
    run(dist, 1500, 1000.0, 10)  # import: a charge-only fleet must not discharge
    assert d1.power_setpoint == 0


def test_discharge_prefers_fuller() -> None:
    full, empty = FakeDevice("full", 90.0), FakeDevice("empty", 20.0)
    dist = make_distribution([empty, full])
    run(dist, 600, 1000.0, 10)  # inside the zone of one device, so the order decides alone
    assert full.power_setpoint > 0
    assert empty.power_setpoint == 0


class PhaseDevice(FakeDevice):
    # a device with the entities the detection reads: its phase select and its home in/output
    def entity_create(self) -> None:
        from custom_components.zenmaster.const import PhaseModi

        HaSelect(self, "power_phase", PhaseModi)
        HaSensor(self, "home_output", uom="W")
        HaSensor(self, "home_input", uom="W")
        for key in ("home_output", "home_input"):
            self.entities[key].entity_id = f"sensor.{self.device_id}_{key}"


def phase_fleet(monkeypatch, jumps: dict[str, list[tuple[float, float]]]) -> tuple[Distribution, PhaseDevice, PhaseDevice]:  # noqa: ANN001
    # two devices on a three-phase meter; the recorder is replaced by the given jumps
    d1, d2 = PhaseDevice("d1", 60.0), PhaseDevice("d2", 60.0)
    dist = make_distribution([d1, d2], DistributionMode.MANUAL)
    dist.p1_entities = [[], [("sensor.l1", 1.0)], [("sensor.l2", 1.0)], [("sensor.l3", 1.0)]]
    dist.solar_panels = []

    class Recorder:
        async def async_add_executor_job(self, fn, *args):
            return fn(*args)  # the matching runs inline instead of on the recorder's thread

    monkeypatch.setattr(distribution_module, "get_instance", lambda hass: Recorder())
    monkeypatch.setattr(dist, "read_jumps", lambda ids: {eid: sorted(jumps.get(eid, [])) for eid in ids})
    return dist, d1, d2


def test_phase_detection_matches_jumps_and_clears_the_second_device_in_the_next_round(monkeypatch) -> None:  # noqa: ANN001
    # d1 steps alone twelve times and L2 follows one to one; d2 only ever steps together with d1, so it is
    # blocked in the first round and read in the second, with d1's jump given back to L2
    jumps: dict[str, list[tuple[float, float]]] = {"sensor.d1_home_output": [], "sensor.d2_home_output": [], "sensor.l2": [], "sensor.l3": []}
    for k in range(1, 13):
        t = 1000.0 * k
        jumps["sensor.d1_home_output"].append((t, 500.0))
        jumps["sensor.l2"].append((t + 1, -500.0))
        t = 1000.0 * k + 500
        jumps["sensor.d1_home_output"].append((t, 400.0))
        jumps["sensor.d2_home_output"].append((t, 600.0))
        jumps["sensor.l2"].append((t + 1, -400.0))
        jumps["sensor.l3"].append((t + 1, -600.0))
    dist, d1, d2 = phase_fleet(monkeypatch, jumps)
    asyncio.run(dist.detect_phases())
    assert d1.power_phase is Phase.l2
    assert d2.power_phase is Phase.l3


def test_phase_detection_leaves_a_device_whose_step_spreads_over_the_phases(monkeypatch) -> None:  # noqa: ANN001
    # a three-phase device lowers every phase by a third: no phase stands out, so nothing is set
    jumps: dict[str, list[tuple[float, float]]] = {"sensor.d1_home_output": [], "sensor.l1": [], "sensor.l2": [], "sensor.l3": []}
    for k in range(1, 13):
        t = 1000.0 * k
        jumps["sensor.d1_home_output"].append((t, 900.0))
        for phase in ("sensor.l1", "sensor.l2", "sensor.l3"):
            jumps[phase].append((t + 1, -300.0))
    dist, d1, _ = phase_fleet(monkeypatch, jumps)
    asyncio.run(dist.detect_phases())
    assert d1.power_phase is None


def test_charge_prefers_emptier() -> None:
    full, empty = FakeDevice("full", 80.0), FakeDevice("empty", 20.0)
    dist = make_distribution([full, empty], DistributionMode.SMART_CHARGING)
    run(dist, -600, 1000.0, 15)  # inside one zone; the reversal countdown runs first
    assert empty.power_setpoint < 0
    assert full.power_setpoint == 0


def test_full_battery_not_charged() -> None:
    full = FakeDevice("full", 90.0)  # at soc_max: no charge headroom
    dist = make_distribution([full], DistributionMode.SMART_CHARGING)
    run(dist, -2000, 1000.0, 15)
    assert full.power_setpoint == 0


def test_empty_battery_not_discharged() -> None:
    empty = FakeDevice("empty", 5.0)  # below soc_min: no discharge headroom
    dist = make_distribution([empty])
    run(dist, 500, 1000.0, 10)
    assert empty.power_setpoint == 0
    run(dist, -1500, 1010.0, 15)  # solar surplus: charging is still allowed
    assert empty.power_setpoint < 0


def test_order_ranks_by_soc() -> None:
    a, b, c = FakeDevice("a", 30.0), FakeDevice("b", 60.0), FakeDevice("c", 85.0)
    dist = make_distribution([a, b, c])
    run(dist, 500, 1000.0, 1)
    assert dist.profile[False].order == [c, b, a]  # fuller first for discharge
    assert dist.profile[True].order == [a, b, c]  # emptier first for charge


def test_order_quantizes_the_soc() -> None:
    a, b = FakeDevice("a", 62.0), FakeDevice("b", 68.0)
    dist = make_distribution([a, b])
    run(dist, 500, 1000.0, 1)
    assert dist.profile[False].order == [a, b]  # same 10% bucket: fleet order wins, the 6% difference does not
    dist2 = make_distribution([FakeDevice("a", 62.0), FakeDevice("b", 78.0)])
    run(dist2, 500, 1000.0, 1)
    assert [d.device_id for d in dist2.profile[False].order] == ["b", "a"]  # a real bucket difference does rank


def test_wear_balance_prefers_less_cycled() -> None:
    fresh, worn = FakeDevice("fresh", 70.0), FakeDevice("worn", 70.0)
    fresh.wear = 2.0
    worn.wear = 20.0
    dist = make_distribution([worn, fresh])
    run(dist, 600, 1000.0, 10)  # inside the zone of one device
    assert dist.profile[False].order[0] is fresh
    assert fresh.power_setpoint > 0 and worn.power_setpoint == 0


def test_direction_change_waits_for_the_integral() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1])
    now = run(dist, 500, 1000.0, 10)
    tick(dist, -800, now + 1.0)  # opposite demand: first sample only arms the confirmation
    tick(dist, -800, now + 2.0)  # second sample: fleet to zero, countdown starts
    assert d1.power_setpoint == 0 and dist.charging is False
    run(dist, -800, now + 2.0, 6)
    assert dist.charging is True and d1.power_setpoint < 0


def test_reverse_factor_raises_on_quick_flips() -> None:
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1])
    now = run(dist, 500, 1000.0, 10)
    dist.profile[True].last_change = now  # the fleet reversed to charging moments ago
    tick(dist, -800, now + 30.0)  # the first opposite sample arms the countdown
    assert dist.reverse_delta == 5 * dist.profile[True].reverse_ws  # within a minute: 5x
    dist2 = make_distribution([FakeDevice("d1", 60.0)])
    now = run(dist2, 500, 1000.0, 10)
    tick(dist2, -800, now + 30.0)
    assert dist2.reverse_delta == dist2.profile[True].reverse_ws  # no recent flip: 1x


def test_stray_device_is_stopped_again() -> None:
    d1, d2 = FakeDevice("d1", 60.0), FakeDevice("d2", 50.0)
    d2.power_setpoint = 300  # left over from an earlier mode: it delivers, so it runs along at first
    dist = make_distribution([d1, d2])
    now = run(dist, 500, 1000.0, 5)
    assert d1.power_setpoint > 0 and d2.power_setpoint > 0
    run(dist, 500, now, 5)  # two devices are too wide a set for 500 W: the emptiest one is stopped
    assert d1.power_setpoint == 500 and d2.power_setpoint == 0


def test_no_dispatch_while_capacity_is_unknown() -> None:
    d1 = FakeDevice("d1", 60.0, kwh=0.0)  # discovered, but no packData yet
    d1.state = DeviceState.CREATED
    dist = make_distribution([d1], DistributionMode.MANUAL)
    dist.manual_power = 500
    run(dist, 0, 1000.0, 5)
    assert d1.power_setpoint == 0  # a device without capacity cannot be ranked, so the order stays empty


def test_dispatch_starts_as_soon_as_capacity_arrives() -> None:
    d1 = FakeDevice("d1", 60.0, kwh=0.0)
    d1.state = DeviceState.CREATED
    dist = make_distribution([d1], DistributionMode.MANUAL)
    d1.on_invalidate = dist.on_invalidate  # the driver wires this on every distribution device
    dist.manual_power = 500
    now = run(dist, 0, 1000.0, 3)
    assert d1.power_setpoint == 0

    d1.kwh = 10.0  # packData arrives and the device reports for the first time
    d1.set_state(DeviceState.ACTIVE)
    run(dist, 0, now, 3)
    assert d1.power_setpoint > 0  # without the invalidation the empty order would stay frozen for 10 minutes


def test_capacity_counts_the_usable_band_of_reporting_devices() -> None:
    d1, d2 = FakeDevice("d1", 50.0, kwh=10.0), FakeDevice("d2", 50.0, kwh=10.0)
    d2.state = DeviceState.OFFLINE
    dist = make_distribution([d1, d2])
    dist.on_invalidate()
    assert dist.capacity == 8.0  # 10 kWh over the 10-90% band; the offline device does not count


def test_fleet_soc_is_a_percentage_of_the_usable_band() -> None:
    d1 = FakeDevice("d1", 50.0, kwh=10.0)
    dist = make_distribution([d1], DistributionMode.MANUAL)
    dist.on_invalidate()
    run(dist, 0, 1000.0, 1)
    assert dist.entities["soc"].state == 50.0  # 4 kWh available of the 8 kWh band


def test_state_change_invalidates_the_order_on_both_sides() -> None:
    d1 = FakeDevice("d1", 60.0)
    d1.state = DeviceState.EXTERNAL  # outside our control, so the step below is a real crossing
    dist = make_distribution([d1])
    d1.on_invalidate = dist.on_invalidate

    dist.order_date = 5000.0
    d1.set_state(DeviceState.ACTIVE)  # joins the fleet
    assert dist.order_date == 0

    dist.order_date = 5000.0
    d1.set_state(DeviceState.CONFIRM)  # stays in control, no re-ranking
    assert dist.order_date == 5000.0

    d1.set_state(DeviceState.OFFLINE)  # drops out
    assert dist.order_date == 0


def test_state_change_without_a_hook_is_safe() -> None:
    d1 = FakeDevice("d1", 60.0)  # not attached to a distribution yet
    d1.set_state(DeviceState.OFFLINE)
    assert d1.state == DeviceState.OFFLINE


def test_update_mode_guards(monkeypatch) -> None:
    notes: list[str] = []
    monkeypatch.setattr(distribution_module.persistent_notification, "async_create", lambda hass, message, *a: notes.append(message))
    d1 = FakeDevice("d1", 60.0)
    d1.state = DeviceState.OFFLINE
    dist = make_distribution([d1])
    asyncio.run(dist.update_mode(SimpleNamespace(value=2), None))
    assert notes == ["No devices online, not possible to start the operation"]

    d1.state = DeviceState.EXTERNAL  # online again, but no P1 meter configured
    asyncio.run(dist.update_mode(SimpleNamespace(value=2), None))
    assert notes[1] == "No P1 meter configured, the distribution never runs without it"

    dist.p1_device = "meter"
    asyncio.run(dist.update_mode(SimpleNamespace(value=2), None))
    assert len(notes) == 2  # both guards pass: no new notification


def test_update_wear_computes_cycles_and_rte(monkeypatch) -> None:
    d1 = FakeDevice("d1", 60.0)  # 10 kWh
    dist = make_distribution([d1])
    for key, eid in (("battery_input_aggr", "sensor.d1_in"), ("battery_output_aggr", "sensor.d1_out"), ("soc", "sensor.d1_soc")):
        d1.entities[key] = SimpleNamespace(entity_id=eid)
    rte: list[float] = []
    d1.entities["rte"] = SimpleNamespace(update_value=lambda v: rte.append(v))

    # daily counter growth (kWh): 24 kWh in, 21.6 kWh out over the window; SoC dropped from 50% to 40%
    stats = {"sensor.d1_in": [{"change": 10.0}, {"change": 14.0}], "sensor.d1_out": [{"change": 21.6}], "sensor.d1_soc": [{"mean": 50}, {"mean": 40}]}

    async def executor_job(fn, *args):
        return stats

    monkeypatch.setattr(distribution_module, "get_instance", lambda hass: SimpleNamespace(async_add_executor_job=executor_job))
    monkeypatch.setattr(distribution_module.dt_util, "now", lambda: datetime.now())
    asyncio.run(dist.update_wear())

    assert d1.wear == pytest.approx((24.0 + 21.6) / (2 * 10.0))  # throughput over twice the capacity
    assert rte == [pytest.approx((21.6 - 1.0) / 24.0 * 100)]  # the 10% SoC drop corrects 1 kWh off the output


def _plan(dist: Distribution, charge: float, discharge: float, buy: float, sell: float) -> None:
    # the four plan columns the mode derives from, all reading slot 12 = now
    col = lambda v: SimpleNamespace(dest=SimpleNamespace(data=[0.0] * 12 + [v]))
    dist.solver_entry._bat_charge, dist.solver_entry._bat_discharge = col(charge), col(discharge)
    dist.solver_entry.grid_buy, dist.solver_entry.grid_sell = col(buy), col(sell)


def test_selling_to_the_grid_forces_the_plan_setpoint() -> None:
    # the evening peak: the plan exports 8.55 kW, so matching (which would cover the house only) is wrong
    dist = make_distribution([FakeDevice("d1", 60.0)])
    _plan(dist, 0.0, 8.55, 0.0, 8.55)
    assert dist.solver_entry._plan_direction() is DistributionMode.INDIVIDUAL
    assert dist.solver_entry.plan_power == pytest.approx(8550.0)


def test_charging_out_of_the_grid_forces_the_plan_setpoint() -> None:
    dist = make_distribution([FakeDevice("d1", 60.0)])
    _plan(dist, 9.75, 0.0, 9.70, 0.0)
    assert dist.solver_entry._plan_direction() is DistributionMode.INDIVIDUAL
    assert dist.solver_entry.plan_power == pytest.approx(-9750.0)


def test_without_grid_trade_the_matching_modes_stay() -> None:
    dist = make_distribution([FakeDevice("d1", 60.0)])
    _plan(dist, 2.0, 0.0, 0.0, 0.0)          # charging on solar surplus only
    assert dist.solver_entry._plan_direction() is DistributionMode.SMART_CHARGING
    _plan(dist, 0.0, 0.07, 0.0, 0.0)         # discharging to cover the house only
    assert dist.solver_entry._plan_direction() is DistributionMode.SMART_DISCHARGING
    _plan(dist, 0.0, 0.0, 0.0, 0.0)
    assert dist.solver_entry._plan_direction() is DistributionMode.OFF


def test_manual_follows_the_plan_only_while_optimize_is_on() -> None:
    # the plan buys 2 kW from the grid: with optimize on the plan takes over in the individual mode and leaves the knob alone,
    # with optimize off the knob keeps your value
    d1 = FakeDevice("d1", 60.0)
    dist = make_distribution([d1], DistributionMode.MANUAL)
    dist.manual_power = 700
    _plan(dist, 2.0, 0.0, 2.0, 0.0)
    dist.solver_entry._apply_plan_mode()
    assert int(dist.mode) == DistributionMode.INDIVIDUAL.value and dist.manual_power == 700, "met optimize aan neemt het plan per accu het over"
    d2 = FakeDevice("d2", 60.0)
    dist = make_distribution([d2], DistributionMode.MANUAL)
    dist.manual_power = 700
    _plan(dist, 2.0, 0.0, 2.0, 0.0)
    run(dist, 100, 1000.0, 10)
    assert 650 <= d2.power_setpoint <= 700, "met optimize uit blijft jouw eigen waarde staan"


def _postrun(dist: Distribution, charge: list[float], discharge: list[float], price: list[float]) -> float:
    # every list is per quarter from slot 12 (now); the grid carries the whole battery flow, buy equals sell
    pad = lambda v: [0.0] * 12 + v + [0.0] * (96 - len(v))
    bc, bd, p = pad(charge), pad(discharge), pad(price)
    b = dist.solver_entry
    b._bat_charge, b._bat_discharge = SimpleNamespace(dest=SimpleNamespace(data=bc)), SimpleNamespace(dest=SimpleNamespace(data=bd))
    b.grid_buy, b.grid_sell = SimpleNamespace(dest=SimpleNamespace(data=bc)), SimpleNamespace(dest=SimpleNamespace(data=bd))
    b.entry_postrun(SimpleNamespace(price_buy=SimpleNamespace(data=p), price_sell=SimpleNamespace(data=p)))
    return dist.entities["savings_today"].state


def test_expected_saving_books_the_margin_over_the_basis() -> None:
    # 4 kWh already in the fleet at 10 cent: selling it at 40 cent earns the 30 cent margin, not the full price
    dist = make_distribution([])
    dist.solver_entry._now = datetime(2026, 1, 1, 20, 0)
    dist.solver_entry.stock, dist.solver_entry.basis = 4.0, 0.10
    dist.solver_entry.savings = dist.solver_entry.savings_midnight = 0.0
    assert _postrun(dist, [0.0] * 4, [4.0] * 4, [0.40] * 4) == pytest.approx(1.20)


def test_expected_saving_of_a_cycle_is_the_price_difference() -> None:
    # empty fleet: 4 kWh bought at 5 cent and sold at 45 cent is 40 cent per kWh, the same the settlement books
    dist = make_distribution([])
    dist.solver_entry._now = datetime(2026, 1, 1, 20, 0)
    dist.solver_entry.stock, dist.solver_entry.basis = 0.0, 0.0
    dist.solver_entry.savings = dist.solver_entry.savings_midnight = 0.0
    assert _postrun(dist, [4.0] * 4 + [0.0] * 4, [0.0] * 4 + [4.0] * 4, [0.05] * 4 + [0.45] * 4) == pytest.approx(1.60)


def test_a_phase_over_its_limit_is_held_under_it_by_its_own_devices() -> None:
    # 18-09: four empty packs on L1 charge along with the rest of the fleet while the house already draws 2000 W on L1.
    # The guard has to cut the L1 charging to what the phase leaves under 17 kW / 3 and keep it there; the packs are
    # empty, so charging less is the only remedy. L2 and L3 have room for everything their devices can take.
    def dev(name: str, phase: int, power: int, kwh: float) -> FakeDevice:
        d = FakeDevice(name, 10.0, power, kwh)
        d.power_phase = Phase(phase)
        d.soc_limit = 2  # the pack's own verdict: empty
        return d

    devices = [dev("sf2400pro", 1, 2400, 3.84), dev("sf1600", 1, 1600, 3.84), dev("sf2400acplus", 1, 2400, 3.84), dev("sf800pro", 1, 800, 1.92),
               dev("sf2400ac", 2, 2400, 5.76), dev("sf800", 3, 800, 1.92), dev("sf800b", 3, 800, 1.92), dev("hyper", 3, 1200, 1.92)]
    dist = make_distribution(devices, DistributionMode.MANUAL)
    dist.manual_power = -9750  # the plan charges out of the grid
    dist.charging = True
    limit = dist.phase_limit_w
    house = {1: 2000, 2: 2800, 3: 2500}
    meter = lambda phase: house[phase] - sum(d.power_actual for d in devices if d.power_phase == phase)

    now, seen = 1000.0, []
    for _ in range(12):
        now += 1.0
        for phase in (1, 2, 3):
            dist.power[phase] = meter(phase)
        asyncio.run(dist._process_p1(int(sum(dist.power)), dist.power, now))
        for d in devices:
            d.power_actual = d.power_setpoint
        seen.append(meter(1))

    settled = [round(m) for m in seen[4:]]
    assert all(m <= limit for m in settled), f"L1 stays over its {limit:.0f} W limit: {settled}"
    assert all(m >= limit - 460 for m in settled), f"L1 is cut further than the 2 A under the limit that is needed: {settled}"
    assert all(meter(phase) <= limit for phase in (2, 3)), "the other phases must not be pushed over their limit by what L1 gives up"


def _house_over_the_limit(devices: list[FakeDevice], ticks: int = 10) -> tuple[Distribution, dict]:
    # the house itself draws 7000 W on L1 over the 5667 W limit while the plan wants nothing: charging less cannot help here
    dist = make_distribution(devices, DistributionMode.MANUAL)
    dist.manual_power = 0
    house = {1: 7000, 2: 1000, 3: 1000}
    now = 1000.0
    for _ in range(ticks):
        now += 1.0
        for phase in (1, 2, 3):
            dist.power[phase] = house[phase] - sum(d.power_actual for d in devices if d.power_phase == phase)
        asyncio.run(dist._process_p1(int(sum(dist.power)), dist.power, now))
        for d in devices:
            d.power_actual = d.power_setpoint
    return dist, {phase: house[phase] - sum(d.power_actual for d in devices if d.power_phase == phase) for phase in (1, 2, 3)}


def test_a_phase_over_its_limit_is_discharged_by_its_own_devices() -> None:
    def dev(name: str, phase: int) -> FakeDevice:
        d = FakeDevice(name, 60.0)  # a pack with charge left: delivering is possible
        d.power_phase = Phase(phase)
        return d

    devices = [dev("l1", 1), dev("l2", 2), dev("l3", 3)]
    dist, meters = _house_over_the_limit(devices)
    limit = dist.phase_limit_w
    assert meters[1] <= limit, f"L1 is still over its {limit:.0f} W limit at {meters[1]:.0f} W"
    assert devices[0].power_setpoint > 0, "the device on L1 has to deliver, charging less is no remedy here"
    assert all(meters[phase] <= limit for phase in (2, 3)), "what L1 gives up must not push another phase over its own limit"


def test_an_empty_pack_does_not_let_the_other_phases_charge_for_it() -> None:
    # the correction may only count on power that is really there: an empty pack delivers nothing, so the fleet setpoint stays put
    def dev(name: str, phase: int, empty: bool) -> FakeDevice:
        d = FakeDevice(name, 10.0 if empty else 60.0)
        d.power_phase = Phase(phase)
        if empty:
            d.soc_limit = 2  # the pack's own verdict: empty
        return d

    devices = [dev("l1", 1, True), dev("l2", 2, False), dev("l3", 3, False)]
    _, meters = _house_over_the_limit(devices)
    assert devices[0].power_setpoint == 0, "an empty pack cannot deliver"
    assert meters[2] == 1000 and meters[3] == 1000, f"the free phases charged for a delivery that never came: {meters}"


def _solve(dist: Distribution, buy: list[float], sell: list[float], meters: dict | None = None, load: list[float] | None = None) -> None:
    # one real solve over 96 quarters: buy per quarter from now, sell per quarter from now including what lies past the horizon,
    # the forecast of the batteries' own panels per meter, and house load on L1
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)

    class _Inputs(SolverEntry):
        def entry_config(self, lp: LpSolver) -> None:
            lp.price_buy, lp.price_sell = lp.data("price_buy", "euro", 2), lp.data("price_sell", "euro", 2)
            lp.globals["solar_meters"] = meters or {}

        async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
            lp.price_buy.data[12 : 12 + len(buy)] = buy
            lp.price_sell.data[12 : 12 + len(sell)] = sell
            if load is not None:
                lp.l1[:] += load

    lp.init([_Inputs(), dist.solver_entry])
    assert asyncio.run(lp.run(datetime(2026, 1, 1, 12, 0))) is not None and lp._last_lp_status == 0


def test_a_solve_without_prices_turns_the_fleet_off_while_optimize_is_on() -> None:
    # no buy prices: run gives no plan, the solver entry switches the fleet off and hands every battery back
    d1 = FakeDevice("d1", 50.0)
    dist = make_distribution([d1], DistributionMode.INDIVIDUAL)
    dist.optimize = True
    lp = LpSolver(track=lambda *a, **k: None, state=lambda *a, **k: None, horizon=96)

    class _NoPrices(SolverEntry):
        def entry_config(self, lp: LpSolver) -> None:
            lp.price_buy, lp.price_sell = lp.data("price_buy", "euro", 2), lp.data("price_sell", "euro", 2)

    lp.init([_NoPrices(), dist.solver_entry])
    assert asyncio.run(lp.run(datetime(2026, 1, 1, 12, 0))) is None
    assert int(dist.mode) == DistributionMode.OFF.value and d1.state is DeviceState.EXTERNAL


def _unit(dist: Distribution, device: FakeDevice):
    return next(u for u in dist.solver_entry._units if u.device is device)


def _series(col) -> list[float]:
    return list(col.dest.data[12 : 12 + 96])


def test_a_slow_battery_starts_charging_earlier() -> None:
    # same pack, a third of the inverter: the cheap window falls in price towards its end, the evening pays 60 cent.
    # The fast one fits in the last cheap quarters, the slow one needs the whole window and so starts at its first quarter.
    fast, slow = FakeDevice("fast", 10.0, power=2400, kwh=4.0), FakeDevice("slow", 10.0, power=800, kwh=4.0)
    dist = make_distribution([fast, slow])
    _solve(dist, [0.20 - 0.005 * t for t in range(16)] + [1.0] * 80, [0.0] * 16 + [0.60] * 16 + [0.0] * 160)
    first = lambda u: next(t for t, v in enumerate(_series(u.charge)) if v > 0.01)
    assert first(_unit(dist, slow)) < first(_unit(dist, fast)), "the slow battery must start before the fast one"


def test_the_fleet_columns_are_the_sum_of_the_batteries() -> None:
    fast, slow = FakeDevice("fast", 10.0, power=2400, kwh=4.0), FakeDevice("slow", 10.0, power=800, kwh=4.0)
    dist = make_distribution([fast, slow])
    _solve(dist, [0.20 - 0.005 * t for t in range(16)] + [1.0] * 80, [0.0] * 16 + [0.60] * 16 + [0.0] * 160)
    units, b = dist.solver_entry._units, dist.solver_entry
    for t in range(95):
        assert _series(b._bat_charge)[t] == pytest.approx(sum(_series(u.charge)[t] + _series(u.dc_in)[t] for u in units), abs=1e-6)
        assert _series(b._bat_discharge)[t] == pytest.approx(sum(_series(u.discharge)[t] for u in units), abs=1e-6)


def test_the_less_worn_battery_covers_the_house_first() -> None:
    # both full, the house needs 1 kW for two quarters at 50 cent: the one that has worked less delivers it
    worn, fresh = FakeDevice("worn", 90.0, kwh=4.0), FakeDevice("fresh", 90.0, kwh=4.0)
    worn.wear, fresh.wear = 2.0, 0.5
    dist = make_distribution([worn, fresh])
    _solve(dist, [0.50] * 96, [0.0] * 192, load=[0.0] * 4 + [1.0, 1.0] + [0.0] * 90)
    assert sum(_series(_unit(dist, fresh).discharge)) == pytest.approx(2.0, abs=1e-6)
    assert sum(_series(_unit(dist, worn).discharge)) == pytest.approx(0.0, abs=1e-6)


def test_own_panels_charge_their_own_battery() -> None:
    # 1 kW of its own solar for two hours: into its own pack it keeps 95 percent, through the house into the other one 80
    own, other = FakeDevice("own", 10.0, kwh=4.0), FakeDevice("other", 10.0, kwh=4.0)
    own.entities["solar"] = SimpleNamespace(entity_id="sensor.own_solar")
    dist = make_distribution([own, other])
    _solve(dist, [1.0] * 96, [0.0] * 96 + [0.30] * 96, meters={"sensor.own_solar": np.array([1.0] * 8 + [0.0] * 88)})
    assert sum(_series(_unit(dist, own).dc_in)) == pytest.approx(8.0, abs=1e-6)
    assert sum(_series(_unit(dist, other).charge)) == pytest.approx(0.0, abs=1e-6)


def test_charging_stays_under_the_limit_of_its_phase() -> None:
    # 3 kW for the house is 1 kW per phase: a battery on L1 that could take 1.8 kW charges at 1 kW
    pack = FakeDevice("pack", 10.0, power=2400, kwh=10.0)
    pack.power_phase = Phase.l1
    dist = make_distribution([pack])
    dist.max_kw = 3.0
    _solve(dist, [0.05] * 96, [0.0] * 96 + [0.50] * 96)
    assert 0.99 <= max(_series(_unit(dist, pack).charge)) <= 1.0 + 1e-6


def test_the_plan_ranks_now_before_later() -> None:
    # the slow battery is planned from the first quarter, the fast one only later: charging puts the slow one first
    fast, slow = FakeDevice("fast", 10.0, power=2400, kwh=4.0), FakeDevice("slow", 10.0, power=800, kwh=4.0)
    dist = make_distribution([fast, slow])
    _solve(dist, [0.20 - 0.005 * t for t in range(16)] + [1.0] * 80, [0.0] * 16 + [0.60] * 16 + [0.0] * 160)
    rank = dist.plan_rank[True]
    assert rank[slow][0] == 0 and rank[fast][0] == 1, f"slow is planned now, fast later: {rank}"


def _ranked(optimize: bool) -> tuple[list[FakeDevice], list[FakeDevice]]:
    # c planned now, a planned later, b not planned: discharging, with the score putting the fullest (b) first
    a, b, c = FakeDevice("a", 50.0), FakeDevice("b", 80.0), FakeDevice("c", 40.0)
    dist = make_distribution([a, b, c])
    dist.solver, dist.optimize = True, optimize
    dist.plan_rank = {False: {c: (0, -2.0), a: (1, 5.0)}, True: {}}
    dist.update_order()
    return [a, b, c], dist.profile[False].order


def test_with_optimize_the_order_follows_the_plan() -> None:
    (a, b, c), order = _ranked(True)
    assert order == [c, a, b], "planned now, then planned later, then the rest"


def test_without_optimize_the_order_keeps_its_own_score() -> None:
    (a, b, c), order = _ranked(False)
    assert order[0] is b, "the score puts the fullest first when discharging"


def test_a_standing_battery_starts_in_the_plan_order() -> None:
    # nothing runs yet and 600 W is asked: one battery starts, the planned one, not the fullest
    x, y = FakeDevice("x", 40.0), FakeDevice("y", 80.0)
    dist = make_distribution([x, y])
    dist.solver = dist.optimize = True
    dist.plan_rank = {False: {x: (0, -1.0)}, True: {}}
    dist.distribute_power(600, [x, y])
    assert x.power_setpoint == distribution_module.RANGE_MIN and y.power_setpoint == 0


def _individual(d1: FakeDevice, d2: FakeDevice) -> Distribution:
    # d1 on L1 charges 2000 W, d2 on L2 delivers 500 W
    d1.power_phase, d2.power_phase = Phase.l1, Phase.l2
    d1.individual, d2.individual = -2000, 500
    return make_distribution([d1, d2], DistributionMode.INDIVIDUAL)


def test_individual_gives_every_battery_its_own_setpoint() -> None:
    d1, d2 = FakeDevice("d1", 50.0), FakeDevice("d2", 50.0)
    dist = _individual(d1, d2)
    run(dist, 0, 1000.0, 3)
    assert (d1.power_setpoint, d2.power_setpoint) == (-2000, 500)


def test_a_solve_sets_the_individual_setpoint_of_every_battery_with_storage() -> None:
    # the AC setpoint of this quarter: discharge plus own solar out, minus charging, in W; the slow one charges from the first quarter
    fast, slow, bare = FakeDevice("fast", 10.0, power=2400, kwh=4.0), FakeDevice("slow", 10.0, power=800, kwh=4.0), FakeDevice("bare", 0.0, kwh=0.0)
    dist = make_distribution([fast, slow, bare])
    _solve(dist, [0.20 - 0.005 * t for t in range(16)] + [1.0] * 80, [0.0] * 16 + [0.60] * 16 + [0.0] * 160)
    u = _unit(dist, slow)
    assert slow.individual == int(1000 * (u.discharge.dest.data[12] + u.dc_out.dest.data[12] - u.charge.dest.data[12])) < 0
    assert bare.individual is None, "no storage, no setpoint"


def test_individual_leaves_a_device_without_storage_to_update_order() -> None:
    # without a pack it works as a plain inverter: update_order gives it its rated power, the individual mode does not touch it
    bare = FakeDevice("bare", 0.0, kwh=0.0)
    dist = make_distribution([bare], DistributionMode.INDIVIDUAL)
    run(dist, 0, 1000.0, 3)
    assert bare.power_setpoint == 2400


def test_individual_hands_a_phase_over_its_limit_to_the_phase_guard() -> None:
    # L1 reads 6000 W, over its 5667: its battery stops charging and helps, L2 keeps its plan; back at 5000 W, 2 A under, the plan returns
    d1, d2 = FakeDevice("d1", 50.0), FakeDevice("d2", 50.0)
    dist = _individual(d1, d2)
    dist.power[:] = [0, 6000, 1000, 1000]
    run(dist, 0, 1000.0, 1)
    assert d1.power_setpoint >= 0 and d2.power_setpoint == 500, f"L1 taken over, L2 on plan: {d1.power_setpoint}, {d2.power_setpoint}"
    dist.power[:] = [0, 5000, 1000, 1000]
    run(dist, 0, 1010.0, 1)
    assert d1.power_setpoint == -2000, "released 2 A under the limit, the plan is back"


def test_individual_is_not_stopped_by_update_order() -> None:
    # the fleet still points to discharging, d1 is empty and charges by its own setpoint: the new order must not stop it
    d1, d2 = FakeDevice("d1", 50.0), FakeDevice("d2", 50.0)
    d1.soc_limit = 2  # the pack's own verdict: empty, so outside the discharge order
    dist = _individual(d1, d2)
    run(dist, 0, 1000.0, 1)
    dist.update_order()
    assert d1.power_setpoint == -2000


def test_individual_stops_a_battery_that_cannot_help_its_limited_phase() -> None:
    # d1 charges on L1 by plan; the house alone draws 7000 W on L1, over its limit, so delivering is needed and d1, empty, cannot: it stops, d2 on L2 keeps its plan
    d1, d2 = FakeDevice("d1", 50.0), FakeDevice("d2", 50.0)
    d1.soc_limit = 2  # the pack's own verdict: empty
    dist = _individual(d1, d2)
    run(dist, 0, 1000.0, 1)
    dist.power[:] = [0, 9000, 1000, 1000]
    run(dist, 0, 1010.0, 1)
    assert (d1.power_setpoint, d2.power_setpoint) == (0, 500)

