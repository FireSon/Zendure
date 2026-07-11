"""Real-time distribution simulation scenarios ported from ZenMaster to EnergyMaster's API."""

from __future__ import annotations

import asyncio
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from custom_components.energymaster.distribution import Distribution, DistributionMode
from custom_components.energymaster.device import DeviceConnection, PowerDevice, PowerProfileCharge, PowerProfileDischarge


class _Val:
    def __init__(self, v: float = 0.0) -> None:
        self.value = float(v)

    @property
    def asNumber(self) -> float:
        return self.value

    def __float__(self) -> float:
        return float(self.value)

    def __format__(self, spec: str) -> str:
        return format(self.value, spec)


class _SimDevice(PowerDevice):
    # PowerDevice with __init__ bypassed: instant-settle battery for the closed-loop sim.
    def __init__(self, device_id: str, power: int, capacity_kwh: float) -> None:
        self.device_id = device_id
        self.name = device_id
        self.profile = [PowerProfileDischarge(power=power), PowerProfileCharge(power=power)]
        self.soc = _Val(0.0)
        self.soc_min = _Val(0.0)
        self.soc_max = _Val(100.0)
        self.solar = _Val(0.0)
        self.battery_output = _Val(0.0)
        self.capacity_kwh = capacity_kwh
        self.setpoint = 0
        self.score = [0.0, 0.0]
        self.inefficient_time = 0.0
        self.connection = DeviceConnection.MQTT_CLOUD
        self._offline = 0.0

    def output_power(self, now: float) -> int:
        return self.setpoint

    async def on_setpoint(self, watts: int, p, now: float) -> int:
        if abs(watts) > abs(p.power):
            watts = p.power if watts > 0 else -p.power
        if watts > 0 and self.soc.asNumber <= self.soc_min.asNumber:
            watts = 0
        elif watts < 0 and self.soc.asNumber >= self.soc_max.asNumber:
            watts = 0
        self.setpoint = watts
        return watts


@dataclass
class AppliancePhase:
    watts: int
    duration_s: float
    cycling: bool = False
    cycle_on_s: float = 0.0
    cycle_off_s: float = 0.0


class Appliance:
    def __init__(self, name: str, phases: list[AppliancePhase]) -> None:
        self.name = name
        self.phases = phases
        self._total_s = sum(p.duration_s for p in phases)

    @property
    def total_duration_s(self) -> float:
        return self._total_s

    def watts_at(self, elapsed_s: float) -> int:
        if elapsed_s < 0 or elapsed_s >= self._total_s:
            return 0
        cumulative = 0.0
        for phase in self.phases:
            if elapsed_s < cumulative + phase.duration_s:
                phase_elapsed = elapsed_s - cumulative
                if phase.cycling and phase.cycle_on_s > 0:
                    cycle_len = phase.cycle_on_s + phase.cycle_off_s
                    pos = phase_elapsed % cycle_len
                    return phase.watts if pos < phase.cycle_on_s else 0
                return phase.watts
            cumulative += phase.duration_s
        return 0


def baseload() -> Appliance:
    return Appliance("baseload", [AppliancePhase(200, 7200)])


def kettle() -> Appliance:
    return Appliance("kettle", [AppliancePhase(2200, 210)])


def toaster() -> Appliance:
    return Appliance("toaster", [AppliancePhase(1000, 180)])


def hair_dryer() -> Appliance:
    return Appliance("hair_dryer", [AppliancePhase(1800, 480)])


def microwave() -> Appliance:
    return Appliance("microwave", [AppliancePhase(1200, 120, cycling=True, cycle_on_s=30, cycle_off_s=10)])


def oven() -> Appliance:
    return Appliance("oven", [AppliancePhase(2400, 600), AppliancePhase(800, 3000, cycling=True, cycle_on_s=40, cycle_off_s=20)])


def induction_hob_high() -> Appliance:
    return Appliance("induction_high", [AppliancePhase(3200, 480), AppliancePhase(800, 720)])


def induction_hob_low() -> Appliance:
    return Appliance("induction_low", [AppliancePhase(800, 1200)])


def dishwasher() -> Appliance:
    return Appliance(
        "dishwasher",
        [
            AppliancePhase(70, 300),
            AppliancePhase(1800, 600),
            AppliancePhase(70, 300),
            AppliancePhase(1800, 600),
            AppliancePhase(70, 300),
            AppliancePhase(600, 1800),
            AppliancePhase(70, 300),
            AppliancePhase(0, 300),
        ],
    )


def washing_machine() -> Appliance:
    return Appliance(
        "washing_machine",
        [
            AppliancePhase(200, 300),
            AppliancePhase(2000, 600),
            AppliancePhase(200, 600),
            AppliancePhase(100, 300),
            AppliancePhase(2000, 300),
            AppliancePhase(100, 300),
            AppliancePhase(500, 600),
            AppliancePhase(50, 300),
        ],
    )


def tumble_dryer() -> Appliance:
    return Appliance("tumble_dryer", [AppliancePhase(2500, 2700, cycling=True, cycle_on_s=45, cycle_off_s=15), AppliancePhase(200, 600)])


def ev_charger() -> Appliance:
    return Appliance("ev_charger", [AppliancePhase(3600, 7200)])


def heat_pump() -> Appliance:
    phases = []
    for _ in range(6):
        phases.append(AppliancePhase(1200, 1140))
        phases.append(AppliancePhase(2400, 60))
    return Appliance("heat_pump", phases)


def solar_generation(peak_w: int = 3000, duration_s: int = 7200) -> Appliance:
    q = duration_s / 4
    return Appliance("solar", [AppliancePhase(-peak_w // 2, q), AppliancePhase(-peak_w, q * 2), AppliancePhase(-peak_w // 2, q)])


@dataclass
class ScheduledAppliance:
    appliance: Appliance
    start_offset_s: float


@dataclass
class HourBucket:
    load_wh: float = 0.0
    solar_wh: float = 0.0
    fleet_wh: float = 0.0
    grid_wh: float = 0.0
    dev_setpoint: list = field(default_factory=list)
    dev_soc: list = field(default_factory=list)
    _dev_setpoint_sum: list = field(default_factory=list)
    _ticks: int = 0


@dataclass
class Metrics:
    name: str
    duration_s: float = 0.0
    grid_import_wh: float = 0.0
    grid_export_wh: float = 0.0
    fleet_discharge_wh: float = 0.0
    fleet_charge_wh: float = 0.0
    device_switches: int = 0
    peak_overshoot_w: int = 0
    peak_undershoot_w: int = 0
    below_floor_ticks: int = 0
    total_ticks: int = 0
    _prev_active: set = field(default_factory=set)
    hourly: list = field(default_factory=list)
    _dev_names: list = field(default_factory=list)
    _dev_kwh: list = field(default_factory=list)

    def report(self) -> str:
        return (
            f"\n  {'-' * 60}\n"
            f"  Scenario: {self.name}\n"
            f"  Duration: {self.duration_s:.0f}s ({self.duration_s / 60:.1f} min)\n"
            f"  {'-' * 60}\n"
            f"    Grid import (uncovered):  {self.grid_import_wh:8.1f} Wh\n"
            f"    Grid export (overshoot):  {self.grid_export_wh:8.1f} Wh\n"
            f"    Fleet discharged:         {self.fleet_discharge_wh:8.1f} Wh\n"
            f"    Fleet charged:            {self.fleet_charge_wh:8.1f} Wh\n"
            f"    Device switches:          {self.device_switches:8d}\n"
            f"    Peak overshoot:           {self.peak_overshoot_w:8d} W\n"
            f"    Peak undershoot:          {self.peak_undershoot_w:8d} W\n"
            f"    Below-floor ticks:        {self.below_floor_ticks:8d} / {self.total_ticks}\n"
            f"  {'-' * 60}"
        )

    def hourly_report(self) -> str:
        if not self.hourly:
            return ""
        n_dev = len(self._dev_names)
        hdr = f"  {'Hour':>4s} | {'Load':>6s} | {'Solar':>6s} | {'Fleet':>6s} | {'Grid':>6s}"
        for i in range(n_dev):
            hdr += f" | {self._dev_names[i]}({self._dev_kwh[i]:.0f}kWh)"
        lines = [f"\n  {'-' * len(hdr)}", hdr, f"  {'-' * len(hdr)}"]
        for h, b in enumerate(self.hourly):
            row = f"  {h:3d}h | {b.load_wh:5.0f}W | {b.solar_wh:5.0f}W | {b.fleet_wh:+6.0f}W | {b.grid_wh:+6.0f}W"
            for i in range(n_dev):
                sp = b.dev_setpoint[i] if i < len(b.dev_setpoint) else 0
                soc = b.dev_soc[i] if i < len(b.dev_soc) else 0
                row += f" | {sp:+5.0f}W {soc:5.1f}%"
            lines.append(row)
        lines.append(f"  {'-' * len(hdr)}")
        return "\n".join(lines)


class SimHarness:
    FLEET = [
        ("bat0", 800, 1.0),
        ("bat1", 1600, 2.0),
        ("bat2", 1600, 5.0),
        ("bat3", 2400, 8.0),
        ("bat4", 2400, 12.0),
    ]
    FLEET_LEVELS = [95.0, 90.0, 85.0, 80.0, 75.0]
    P1_INTERVAL = 1.5
    NOISE_W = 50
    SOLAR_AVG_S = 60.0

    def __init__(self, name: str) -> None:
        self.dist = Distribution.__new__(Distribution)
        self.dist.devices = []
        self.dist._mode = DistributionMode.MATCHING
        self.dist.manual_setpoint = 0
        self.dist.max_grid = 0
        self.dist.setpoint = 0
        self.dist.solar = []
        self.dist._power_reselect = 0.0
        self.dist.charge_order = []
        self.dist.discharge_order = []
        class _Stat:
            asNumber = 0.0
            def update_value(self, value) -> bool:
                return True
        for a in ("capacity_kwh", "available_kwh", "grid_import", "grid_export", "solar_power", "discharge_power", "charge_power", "house_load", "soc", "autarky"):
            setattr(self.dist, a, _Stat())
        self.devs: list[_SimDevice] = []
        for (dname, power, kwh), soc in zip(self.FLEET, self.FLEET_LEVELS):
            d = _SimDevice(dname, power, kwh)
            d.soc.value = soc
            self.devs.append(d)
            self.dist.devices.append(d)
        self.metrics = Metrics(name=name, _dev_names=[n for n, _, _ in self.FLEET], _dev_kwh=[k for _, _, k in self.FLEET])

    async def run(self, schedule: list[ScheduledAppliance], duration_s: Optional[float] = None, seed: int = 42) -> Metrics:
        if duration_s is None:
            duration_s = max(sa.start_offset_s + sa.appliance.total_duration_s for sa in schedule)
        m = self.metrics
        m.duration_s = duration_s
        hours = self.P1_INTERVAL / 3600.0

        rng = random.Random(seed)
        solar_avg_len = max(1, int(self.SOLAR_AVG_S / self.P1_INTERVAL))
        solar_buf: deque[int] = deque(maxlen=solar_avg_len)

        sim_time = 1000000.0
        elapsed = 0.0

        while elapsed < duration_s:
            sim_time += self.P1_INTERVAL
            elapsed += self.P1_INTERVAL

            loads_w = 0
            solar_instant = 0
            for sa in schedule:
                w = sa.appliance.watts_at(elapsed - sa.start_offset_s)
                if w < 0:
                    solar_instant += abs(w)
                else:
                    loads_w += w

            solar_buf.append(solar_instant)
            solar_w = sum(solar_buf) // len(solar_buf)
            noise = rng.randint(-self.NOISE_W, self.NOISE_W)

            fleet_output = sum(d.output_power(sim_time) for d in self.devs)  # discharge positive
            p1 = loads_w - solar_instant - fleet_output + noise

            self.dist.solar = [solar_w]
            await self.dist._process_p1(p1, now=sim_time)

            for d in self.devs:
                if d.setpoint != 0 and d.capacity_kwh > 0:
                    d.soc.value -= d.setpoint * self.P1_INTERVAL / 3600.0 / (d.capacity_kwh * 10)
                    d.soc.value = max(0.0, min(100.0, d.soc.value))

            m.total_ticks += 1
            if p1 > 0:
                m.grid_import_wh += p1 * hours
                if fleet_output > 0:
                    m.peak_undershoot_w = max(m.peak_undershoot_w, p1)
            if p1 < 0 and fleet_output > 0:
                m.grid_export_wh += abs(p1) * hours
                m.peak_overshoot_w = max(m.peak_overshoot_w, abs(p1))
            if fleet_output > 0:
                m.fleet_discharge_wh += fleet_output * hours
            elif fleet_output < 0:
                m.fleet_charge_wh += abs(fleet_output) * hours
            for d in self.devs:
                if d.setpoint != 0 and abs(d.setpoint) < abs(d.profile[d.setpoint < 0].floor):
                    m.below_floor_ticks += 1
            curr = {d.name for d in self.devs if d.setpoint != 0}
            m.device_switches += len(curr - m._prev_active) + len(m._prev_active - curr)
            m._prev_active = curr
            hour = int(elapsed / 3600)
            while len(m.hourly) <= hour:
                m.hourly.append(HourBucket(_dev_setpoint_sum=[0.0] * len(self.devs)))
            b = m.hourly[hour]
            b._ticks += 1
            b.load_wh += loads_w * hours
            b.solar_wh += solar_instant * hours
            b.fleet_wh += fleet_output * hours
            b.grid_wh += p1 * hours
            for i, d in enumerate(self.devs):
                b._dev_setpoint_sum[i] += d.setpoint
            b.dev_soc = [d.soc.asNumber for d in self.devs]

        for b in m.hourly:
            if b._ticks > 0 and not b.dev_setpoint:
                b.dev_setpoint = [s / b._ticks for s in b._dev_setpoint_sum]
        return m


def test_scenario_morning_rush() -> None:
    m = asyncio.run(
        SimHarness("morning_rush").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(kettle(), 30),
                ScheduledAppliance(toaster(), 30),
                ScheduledAppliance(hair_dryer(), 90),
            ],
            600,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_cooking_dinner() -> None:
    m = asyncio.run(
        SimHarness("cooking_dinner").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(oven(), 0),
                ScheduledAppliance(induction_hob_high(), 60),
            ],
            1200,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_solar_midday() -> None:
    m = asyncio.run(
        SimHarness("solar_midday").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(solar_generation(3000, 7200), 0),
                ScheduledAppliance(dishwasher(), 300),
                ScheduledAppliance(washing_machine(), 600),
            ],
            4800,
        )
    )
    print(m.report())
    assert m.fleet_charge_wh > 0


def test_scenario_evening_peak() -> None:
    m = asyncio.run(
        SimHarness("evening_peak").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(ev_charger(), 0),
                ScheduledAppliance(oven(), 300),
            ],
            3600,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_rapid_cycling() -> None:
    m = asyncio.run(
        SimHarness("rapid_cycling").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(microwave(), 0),
                ScheduledAppliance(kettle(), 60),
            ],
            300,
        )
    )
    print(m.report())


def test_scenario_winter_evening() -> None:
    m = asyncio.run(
        SimHarness("winter_evening").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(heat_pump(), 0),
                ScheduledAppliance(oven(), 300),
                ScheduledAppliance(induction_hob_low(), 600),
            ],
            3600,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_laundry_day() -> None:
    m = asyncio.run(
        SimHarness("laundry_day").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(washing_machine(), 0),
                ScheduledAppliance(tumble_dryer(), 1800),
            ],
            5400,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_solar_to_cooking() -> None:
    solar_short = Appliance("solar_short", [AppliancePhase(-2500, 600), AppliancePhase(-500, 120), AppliancePhase(0, 3000)])
    m = asyncio.run(
        SimHarness("solar_to_cooking").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(solar_short, 0),
                ScheduledAppliance(induction_hob_high(), 720),
                ScheduledAppliance(oven(), 780),
            ],
            2400,
        )
    )
    print(m.report())
    assert m.fleet_charge_wh > 0
    assert m.fleet_discharge_wh > 0


def test_scenario_overnight_baseload() -> None:
    fridge = Appliance("fridge", [AppliancePhase(150, 7200, cycling=True, cycle_on_s=900, cycle_off_s=900)])
    m = asyncio.run(
        SimHarness("overnight_baseload").run(
            [
                ScheduledAppliance(Appliance("standby", [AppliancePhase(180, 7200)]), 0),
                ScheduledAppliance(fridge, 0),
            ],
            7200,
        )
    )
    print(m.report())


def test_scenario_everything_at_once() -> None:
    m = asyncio.run(
        SimHarness("everything_at_once").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(ev_charger(), 0),
                ScheduledAppliance(oven(), 0),
                ScheduledAppliance(dishwasher(), 0),
                ScheduledAppliance(washing_machine(), 60),
            ],
            1800,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_kettle_during_discharge() -> None:
    m = asyncio.run(
        SimHarness("kettle_during_discharge").run(
            [
                ScheduledAppliance(Appliance("steady", [AppliancePhase(500, 600)]), 0),
                ScheduledAppliance(kettle(), 120),
            ],
            600,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_solar_clouds() -> None:
    cloud_phases = []
    for _ in range(8):
        cloud_phases.append(AppliancePhase(-2500, 300))
        cloud_phases.append(AppliancePhase(-400, 120))
    m = asyncio.run(
        SimHarness("solar_clouds").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(Appliance("solar_cloudy", cloud_phases), 0),
            ],
            3360,
        )
    )
    print(m.report())


def test_scenario_full_day() -> None:
    fridge = Appliance("fridge", [AppliancePhase(150, 86400, cycling=True, cycle_on_s=900, cycle_off_s=900)])
    standby = Appliance("standby", [AppliancePhase(180, 86400)])
    ev_short = Appliance("ev_short", [AppliancePhase(3600, 1800)])
    solar_day = Appliance(
        "solar_day",
        [
            AppliancePhase(-1000, 7200),
            AppliancePhase(-3000, 14400),
            AppliancePhase(-1000, 7200),
        ],
    )

    m = asyncio.run(
        SimHarness("full_day").run(
            [
                ScheduledAppliance(standby, 0),
                ScheduledAppliance(fridge, 0),
                ScheduledAppliance(kettle(), 6 * 3600),
                ScheduledAppliance(toaster(), 6 * 3600),
                ScheduledAppliance(hair_dryer(), 6.5 * 3600),
                ScheduledAppliance(solar_day, 10 * 3600),
                ScheduledAppliance(dishwasher(), 11 * 3600),
                ScheduledAppliance(washing_machine(), 12 * 3600),
                ScheduledAppliance(induction_hob_high(), 16 * 3600),
                ScheduledAppliance(oven(), 17 * 3600),
                ScheduledAppliance(ev_short, 17.5 * 3600),
                ScheduledAppliance(heat_pump(), 19 * 3600),
                ScheduledAppliance(tumble_dryer(), 20 * 3600),
            ],
            86400,
        )
    )
    print(m.report())
    print(m.hourly_report())
    assert m.fleet_discharge_wh > 0
    assert m.fleet_charge_wh > 0


def test_summary_report() -> None:
    scenarios = [
        ("morning_rush", test_scenario_morning_rush),
        ("cooking_dinner", test_scenario_cooking_dinner),
        ("solar_midday", test_scenario_solar_midday),
        ("evening_peak", test_scenario_evening_peak),
        ("rapid_cycling", test_scenario_rapid_cycling),
        ("winter_evening", test_scenario_winter_evening),
        ("laundry_day", test_scenario_laundry_day),
        ("solar_to_cooking", test_scenario_solar_to_cooking),
        ("overnight_baseload", test_scenario_overnight_baseload),
        ("everything_at_once", test_scenario_everything_at_once),
        ("kettle_discharge", test_scenario_kettle_during_discharge),
        ("solar_clouds", test_scenario_solar_clouds),
        ("full_day", test_scenario_full_day),
    ]

    print(f"\n{'=' * 72}")
    print("  SIMULATION SUMMARY")
    print(f"{'=' * 72}")

    results = []
    for name, fn in scenarios:
        try:
            fn()
            results.append((name, "PASS"))
        except AssertionError as e:
            results.append((name, f"FAIL: {e}"))

    print(f"\n{'=' * 72}")
    print("  RESULTS")
    print(f"{'=' * 72}")
    for name, status in results:
        marker = "PASS" if status == "PASS" else "FAIL"
        print(f"  [{marker:4s}] {name}")
    print(f"{'=' * 72}\n")
    for name, status in results:
        assert status == "PASS", f"{name} -> {status}"
