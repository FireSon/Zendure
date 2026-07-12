"""Real-time distribution simulation scenarios ported from ZenMaster to EnergyMaster's API."""

from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from custom_components.energymaster.binary_sensor import EmBinarySensor
from custom_components.energymaster.device import DeviceConnection
from custom_components.energymaster.distribution import Distribution, DistributionMode
from custom_components.energymaster.number import EmNumber
from custom_components.energymaster.select import EmSelect
from custom_components.energymaster.sensor import EmSensor
from custom_components.energymaster.switch import EmSwitch
from custom_components.energymaster.zendure.device import ZendureDevice
from tests.distribution.test_device_task import pump_device

_DISCHARGE_MAX = {"SF2400AC": 2400, "SF2400AC_PLUS": 2400, "SF2400PRO": 2400, "SF1600AC": 1600, "SF1600AC_PLUS": 1600, "HYPER2000": 1200, "SF800": 800, "SF800PRO": 800}
_SIM_PHYSICS = {"HYPER2000": (12.0, 5.0, 200.0)}  # lag_cold, lag_warm, ramp W/s (0 = step); default = SF-gedrag


class _Driver:
    @staticmethod
    def on_update(device) -> None:
        pass


class SimZendureDevice(ZendureDevice):
    # Echt ZendureDevice; alleen de MQTT-write is afgevangen. Fysica + 1s-reports gesimuleerd.
    def __init__(self, prodkey: str, config: dict, start_soc: float) -> None:
        super().__init__(None, prodkey, config, _Driver())
        for e in self.entities.values():
            e.hass = None  # geen HA-loop in de sim
        self.connection.set_state(DeviceConnection.MQTT_LOCAL)
        p = self.profile[0]
        p.power = _DISCHARGE_MAX.get(self.zendure_model.name, 800)  # live vullen reports dit; sim zet de echte ontlaadmax
        p.__post_init__()  # floor en band volgen het aangepaste vermogen
        self.commands: list[dict] = []
        self._pending: int | None = None
        self._target = 0  # W uit laatste commando, +ontladen / -laden
        self._actual = 0.0
        self._soc_pct = start_soc
        self._pending_at = 0.0  # moment waarop het openstaande commando effectief wordt
        self._report_at = 0.0
        if self.capacity_kwh:  # accu-loze devices rapporteren geen SoC-properties; span 0 filtert ze uit de dispatch
            self.update_properties({"properties": {"minSoc": 100, "socSet": 950, "electricLevel": int(start_soc)}}, 0.0)

    async def recover_connection(self) -> None:
        self.commands.append({"recover": True})  # no real BLE/HTTP attempts from the sim

    def mqtt_publish(self, topic: str, command: dict) -> None:
        self.commands.append(command)
        if "function" not in command:
            return  # properties/read etc. is no power command
        args = command["arguments"]
        if command["function"] == "deviceAutomation":  # Hyper
            v = args[0]["autoModelValue"]
            self._pending = -v["chargingPower"] if args[0]["autoModelProgram"] == 1 else v["outPower"]
        elif command["function"] == "hemsEP":  # SF-modellen
            self._pending = -args["chargePower"] if args["chargeState"] else args["outputPower"]
        self.command_reply()  # the real device acks receipt of a function invoke

    def tick(self, now: float, dt: float) -> None:
        # pijplijn: een commando wordt ~lag na ontvangst effectief; nieuwere commando's overschrijven de waarde, niet de timer
        lag_cold, lag_warm, ramp = _SIM_PHYSICS.get(self.zendure_model.name, (10.0, 4.0, 0.0))
        if self._pending is not None and self._pending_at == 0.0:
            self._pending_at = now + (lag_cold if self._actual == 0 else lag_warm)
        if self._pending is not None and now >= self._pending_at:
            self._target, self._pending, self._pending_at = self._pending, None, 0.0
        p = self.profile[self._target < 0]
        target = max(-abs(p.power), min(abs(p.power), self._target))
        if self.capacity_kwh == 0 or target > 0 and self._soc_pct <= self.soc_min.asNumber or target < 0 and self._soc_pct >= self.soc_max.asNumber:
            target = 0
        self._actual = float(target) if ramp == 0 else self._actual + max(-ramp * dt, min(ramp * dt, target - self._actual))
        if self.capacity_kwh:
            self._soc_pct = max(0.0, min(100.0, self._soc_pct - self._actual * dt / 3600.0 / (self.capacity_kwh * 10)))
            aggr = self.battery_output_aggr if self._actual > 0 else self.battery_input_aggr  # kWh-tellers zoals live (wear-bron)
            aggr._state = (aggr._state or 0.0) + abs(self._actual) * dt / 3600.0 / 1000.0
        if now >= self._report_at:
            self._report_at = now + 1.0
            w = int(self._actual)
            props = {"outputHomePower": max(0, w), "gridInputPower": max(0, -w), "packInputPower": max(0, w), "outputPackPower": max(0, -w), "electricLevel": int(self._soc_pct)}
            self.update_properties({"properties": props}, now)


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


_REAL_PROFILES = json.loads((Path(__file__).parent / "real_profiles.json").read_text())


class RealAppliance(Appliance):
    # per-seconde profiel uit productie-data (real_profiles.json), geextraheerd 2026-07-10/11
    def __init__(self, name: str) -> None:
        self.name = name
        self._watts = _REAL_PROFILES[name]["watts"]
        self._total_s = float(len(self._watts))

    def watts_at(self, elapsed_s: float) -> int:
        return self._watts[int(elapsed_s)] if 0 <= elapsed_s < self._total_s else 0


def baseload() -> Appliance:
    return Appliance("baseload", [AppliancePhase(200, 7200)])


def kettle() -> Appliance:
    return RealAppliance("koffie")


def toaster() -> Appliance:
    return Appliance("toaster", [AppliancePhase(1000, 180)])


def hair_dryer() -> Appliance:
    return Appliance("hair_dryer", [AppliancePhase(1800, 480)])


def microwave() -> Appliance:
    return Appliance("microwave", [AppliancePhase(1200, 120, cycling=True, cycle_on_s=30, cycle_off_s=10)])


def oven() -> Appliance:
    return RealAppliance("koken")


def induction_hob_high() -> Appliance:
    return RealAppliance("inductie")


def induction_hob_low() -> Appliance:
    return Appliance("induction_low", [AppliancePhase(800, 1200)])


def dishwasher() -> Appliance:
    return RealAppliance("vaatwasser")


def washing_machine() -> Appliance:
    return RealAppliance("wasmachine")


def tumble_dryer() -> Appliance:
    return Appliance("tumble_dryer", [AppliancePhase(2500, 2700, cycling=True, cycle_on_s=45, cycle_off_s=15), AppliancePhase(200, 600)])


def ev_charger() -> Appliance:
    return RealAppliance("ev")


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
    START_SOC = {"p0S70q2x": 95.0, "MPEt6K1P": 90.0, "Cd3wbW9V": 85.0, "Vw6mE0cP": 80.0, "BVng8146": 75.0, "9RUB1mW7": 70.0}
    P1_INTERVAL = 1.0  # live krijgt de distributie elke seconde een P1-waarde
    NOISE_W = 50
    SOLAR_AVG_S = 60.0
    ORDER_INTERVAL_S = 900.0  # order per 15-min window vast

    def __init__(self, name: str) -> None:
        for cls in (EmSensor, EmNumber, EmBinarySensor, EmSelect, EmSwitch):
            cls.add = staticmethod(lambda *a, **k: None)
        self.dist = Distribution(None, [])
        for e in self.dist.entities.values():
            e.hass = None  # geen HA-loop in de sim
        self.dist._mode = DistributionMode.MATCHING
        cfg = json.loads((Path(__file__).resolve().parents[2] / "config" / ".storage" / "energymaster").read_text())
        self.devs: list[SimZendureDevice] = []
        for dev_id, dev_cfg in cfg["data"]["zendure"]["devices"].items():
            d = SimZendureDevice(dev_cfg["prodkey"], {**dev_cfg, "deviceid": dev_id}, self.START_SOC.get(dev_id, 0.0))
            self.devs.append(d)
            self.dist.devices.append(d)
        self.metrics = Metrics(name=name, _dev_names=[d.name for d in self.devs], _dev_kwh=[d.capacity_kwh for d in self.devs])
        self._order_at = 0.0

    def _window_tick(self, sim_time: float) -> None:
        # wat entry_postrun live doet: de 15-min orderbouw (wear-snapshots doen de devices zelf via on_telemetry)
        if sim_time >= self._order_at:
            self._order_at = sim_time + self.ORDER_INTERVAL_S
            self.dist._build_orders(sim_time)

    async def run(self, schedule: list[ScheduledAppliance], duration_s: Optional[float] = None, seed: int = 42, p1_filter=None) -> Metrics:
        # p1_filter: optionele bewerking (bijv. middeling) op de P1 die de REGELAAR ziet; metrics rekenen op de rauwe P1
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

            self._window_tick(sim_time)

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

            for d in self.devs:
                d.tick(sim_time, self.P1_INTERVAL)  # fysica + telemetrie-report door de echte verwerkingsketen

            fleet_output = sum(d.output_power() for d in self.devs)  # discharge positive
            p1 = loads_w - solar_instant - fleet_output + noise

            self.dist.solar = [solar_w]
            await self.dist._process_p1(p1 if p1_filter is None else int(p1_filter(p1)), now=sim_time)
            for d in self.devs:
                await pump_device(d, sim_time)  # the real device task logic runs on simulated time

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
