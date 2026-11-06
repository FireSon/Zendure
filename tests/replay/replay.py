"""Replay a recorded series against the live Distribution and report what the meter would have done."""

# usage: python tests/replay/replay.py replay-0813.json [mode] ; mode is one of the DistributionModi keys, default smart

import asyncio
import importlib.util
import json
import sys
import time

# fabricates the homeassistant modules; under pytest the suite conftest already did it, and a second set of dummies clashes on metaclass
if "homeassistant" not in sys.modules:
    spec = importlib.util.spec_from_file_location("_conftest", "tests/conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["conftest"] = module

from conftest import fake_hass  # noqa: E402
from custom_components.zenmaster.binary_sensor import HaBinarySensor  # noqa: E402
from custom_components.zenmaster.const import Phase  # noqa: E402
from custom_components.zenmaster.device import REPORT_WAIT_S, DeviceState, DistributionDevice, ProfileCharge, ProfileDischarge  # noqa: E402
from custom_components.zenmaster.distribution import Distribution, DistributionMode, DistributionModi  # noqa: E402
from custom_components.zenmaster.number import HaNumber  # noqa: E402
from custom_components.zenmaster.select import HaSelect  # noqa: E402
from custom_components.zenmaster.sensor import HaSensor  # noqa: E402

for cls in (HaSensor, HaNumber, HaBinarySensor, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform outside HA

# measured 2026-09-21/22 on the fleet with EM unloaded, direct commands, the device polled at 15 Hz (scratchpad rhythm_campaign): the
# setpoint is in the device 1.8 s after the command (1.1 to 2.4 s per device, ZenSDK ~0.4 s quicker than hemsEP), the power then follows
# at ~200 W/s (150 to 210 on the 2400s, the 1600/800 not resolved); from 0 W the first power flows only 6 to 15 s after the command,
# already after 15 s idle; a stop lands after 2.3 s; commands are handled in order, one per ~1.3 s, a quicker follower is dropped
SETPOINT_S = 1.8  # command to setpoint in the device
RAMP_W_S = 200.0  # the power moves toward the applied setpoint at this slope
START_S = 8.4  # from standstill the first power flows this long after the command, median of five SolarFlows
STOP_S = 2.3  # a command to 0 W lands after this, in one step
PROCESS_S = 1.3  # one command per this; a command sent sooner after the previous one is dropped
LAG_ENABLED = True  # False reproduces the old optimistic bound, where a device followed its setpoint within one tick


class ReplayDevice(DistributionDevice):
    # the vendor contract, reduced to what a replay needs: remember the clamped command, follow it, drain the pack
    def __init__(self, hass, spec: dict) -> None:
        super().__init__(hass, spec["key"], [ProfileDischarge(spec["power_discharge"]), ProfileCharge(spec["power_charge"])])
        self.kwh = spec["kwh"]
        self.soc_min = spec.get("soc_min", self.soc_min)
        self.soc_max = spec.get("soc_max", self.soc_max)
        self.power_phase = Phase(spec.get("phase", 0))
        self.state = DeviceState.ACTIVE
        self.now = 0.0  # the replay clock, set by the loop; the state stamp carries it instead of the wall clock
        self.queue: list[tuple[float, int]] = []  # commands on their way into the device: (accepted at, target)
        self.applied = 0  # the setpoint the device works with
        self.flow_at = 0.0  # from standstill: when the inverter is up and the power starts to move
        self.last_accept = -1e18
        self.power_since = -1e18

    def power(self, watts: int) -> int:
        watts = max(self.power_profile[1].power, min(self.power_profile[0].power, int(watts)))

        # bypass: a full pack cannot take more, an empty one passes through no more than the panels make
        level = (self.soc - self.soc_min) / (self.soc_max - self.soc_min) * 100 if self.soc_max > self.soc_min else 0.0
        if watts < 0 and level >= 99:
            watts = 0
        elif level <= 1:
            watts = min(self.solar, watts)

        # the echo: until the queued command has been applied the device is busy with it, REPORT_WAIT_S at most
        if self.applied != self.power_setpoint and self.now - self.power_since < REPORT_WAIT_S:
            return self.power_actual

        # the driver's rule: no command while both the measured power and the last setpoint sit within 10 W of the target; otherwise it
        # goes out (again), SENT until the power follows. The device takes one command per PROCESS_S, in order, with one waiting
        # place: a command that would wait longer than a period is dropped (measured 25 percent lost at 1 Hz, none at 2 s)
        if abs(self.power_actual - watts) < 10 and abs(self.power_setpoint - watts) < 10:
            return self.power_actual
        if self.power_setpoint != watts:
            self.power_since = self.now
        self.power_setpoint = watts
        self.state = DeviceState.SENT
        self.state_since = self.now
        accept = max(self.now, self.last_accept + PROCESS_S)
        if accept - self.now < PROCESS_S:
            self.queue.append((accept, watts))
            self.last_accept = accept
        return self.power_actual

    def power_off(self) -> None:
        self.power_setpoint = 0


async def replay(data: dict, mode: int) -> dict:
    hass = fake_hass()
    dist = Distribution(hass, "replay")
    dist.wear_date = float("inf")
    dist.mode = mode
    dist.p1_device = "replay"
    dist.devices = [ReplayDevice(hass, spec) for spec in data["devices"]]
    for entity in dist.entities.values():
        if entity:
            entity.hass = None

    # the soc has to start where the recording starts, otherwise the ranking is wrong from the first tick
    keys = [spec["key"] for spec in data["devices"]]
    for device, key in zip(dist.devices, keys):
        device.soc = data["soc"][key][0]

    times, p1s, pvs = data["t"], data["p1"], data["pv"]
    segments, import_wh, export_wh, switches, previous = [], 0.0, 0.0, 0, set()
    recorded_wh = 0.0  # the meter as it actually ran that day, over the same steps: what the other control achieved
    ticks = []  # wall time of every _process_p1 call in ms: the control's own cost per meter reading, without HA's state writes and MQTT
    limit = dist.phase_limit_w
    over_s, recorded_over_s, phase_peak = 0.0, 0.0, 0.0  # seconds a phase sat over its limit, ours and the recording's, and our worst phase
    for i in range(len(times) - 1):
        step = min(times[i + 1] - times[i], 60.0)

        # what the house drew net of its own PV, from the recording; the fleet part is replaced by ours
        measured = sum(data["power"][key][i] for key in keys)
        house = p1s[i] + pvs[i] + measured
        fleet = sum(d.power_actual for d in dist.devices)
        grid = house - pvs[i] - fleet
        for device, key in zip(dist.devices, keys):
            device.now = times[i]
            device.solar = data["solar"][key][i]  # the panels on the device itself, what a bypass can pass through
            device.bypass = not device.kwh  # no pack means the panels always run straight through; an empty pack still charges

        # the meter per phase, the same way: the recorded fleet part out, ours in; a device without a known phase counts a third on each
        if "l1" in data:
            for phase in (1, 2, 3):
                swap = sum((data["power"][key][i] - device.power_actual) * (1.0 if device.power_phase == phase else 1 / 3 if device.power_phase == Phase.all else 0.0) for device, key in zip(dist.devices, keys))
                dist.power[phase] = data[f"l{phase}"][i] + swap
            over_s += step if max(dist.power[1:]) > limit else 0.0
            recorded_over_s += step if max(data[f"l{phase}"][i] for phase in (1, 2, 3)) > limit else 0.0
            phase_peak = max(phase_peak, max(dist.power[1:]))

        # follow the mode the recording was in; manual needs its recorded setpoint, without it that stretch is mirrored and skipped
        recorded = data.get("mode", [None] * len(times))[i]
        manual = data.get("manual")
        replayable = recorded != "off" and (recorded != "manual" or manual is not None)
        if recorded:
            dist.mode = next((k for k, v in DistributionModi.items() if v == recorded), dist.mode)
        if manual is not None:
            dist.manual_power = manual[i]

        dist.solar = pvs[i]  # the panels as the stabilizer sees them live
        started = time.perf_counter()
        await dist._process_p1(int(grid), dist.power, times[i])
        ticks.append((time.perf_counter() - started) * 1000)

        # a real device answers after its measured dead time and then slews; LAG_ENABLED False is the old optimistic bound
        now = times[i]
        for device, key in zip(dist.devices, keys):
            if not replayable:
                device.power_actual = data["power"][key][i]
                continue
            if not LAG_ENABLED:
                device.power_actual = device.power_setpoint
                continue
            if not device.kwh:  # no pack: the panels run straight through, the recording is what it delivered
                device.power_actual = data["power"][key][i]
                continue
            # the device applies its commands in order: a stop after STOP_S, a setpoint after SETPOINT_S; from standstill the inverter first has to start
            while device.queue and now >= device.queue[0][0] + (STOP_S if device.queue[0][1] == 0 else SETPOINT_S):
                accepted, target = device.queue.pop(0)
                if device.applied == 0 and target != 0:
                    device.flow_at = accepted + START_S
                device.applied = target
            # the power: a stop lands at once, otherwise it moves toward the setpoint at RAMP_W_S once the inverter runs
            if device.applied == 0:
                device.power_actual = 0
            elif now >= device.flow_at:
                move = RAMP_W_S * step
                delta = device.applied - device.power_actual
                device.power_actual = device.applied if abs(delta) <= move else device.power_actual + (move if delta > 0 else -move)
            # the state follows the report like the driver: ACTIVE once the power is within 40 W of the setpoint
            if device.state == DeviceState.SENT and abs(device.power_actual - device.power_setpoint) <= 40:
                device.state = DeviceState.ACTIVE
            if device.kwh:
                device.soc = max(0.0, min(100.0, device.soc - device.power_actual * step / 36000.0 / device.kwh))
                level = (device.soc - device.soc_min) / (device.soc_max - device.soc_min) * 100 if device.soc_max > device.soc_min else 0.0
                device.soc_limit = 2 if level <= 1 else 1 if level >= 99 else 0  # the pack's own verdict, as the real device reports it

        if not replayable:
            continue
        segments.append((abs(grid), step))
        import_wh += max(0, grid) * step / 3600
        export_wh += max(0, -grid) * step / 3600
        recorded_wh += abs(p1s[i]) * step / 3600
        active = {d.device_id for d in dist.devices if d.power_setpoint}
        switches += len(active ^ previous)
        previous = active

    total = sum(step for _, step in segments)
    segments.sort()
    ticks.sort()

    def percentile(share: float) -> float:
        seen = 0.0
        for value, step in segments:
            seen += step
            if seen >= total * share:
                return value
        return segments[-1][0]

    within = lambda watts: sum(step for value, step in segments if value <= watts) / total * 100
    return {
        "mode": mode,
        "hours": total / 3600,
        "import_wh": import_wh,
        "export_wh": export_wh,
        "total_wh": import_wh + export_wh,
        "recorded_wh": recorded_wh,
        "median": percentile(0.5),
        "p90": percentile(0.9),
        "p99": percentile(0.99),
        "within": {watts: within(watts) for watts in (100, 250, 500)},
        "switches": switches,
        "tick_ms": {"median": ticks[len(ticks) // 2], "p99": ticks[int(len(ticks) * 0.99)], "max": ticks[-1]},
        "soc": {device.device_id: device.soc for device in dist.devices},
        "over_s": over_s,
        "recorded_over_s": recorded_over_s,
        "phase_peak": phase_peak,
    }


async def main() -> None:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    mode = next((k for k, v in DistributionModi.items() if v == (sys.argv[2] if len(sys.argv) > 2 else "smart")), 2)
    result = await replay(data, mode)

    print(f"{sys.argv[1]}  mode {DistributionModi[result['mode']]}  {result['hours']:.1f} h  {len(result['soc'])} devices")
    print(f"  import {result['import_wh']:8.0f} Wh   export {result['export_wh']:8.0f} Wh   totaal {result['total_wh']:8.0f} Wh   opgenomen {result['recorded_wh']:8.0f} Wh")
    print(f"  mediaan {result['median']:6.0f} W   p90 {result['p90']:6.0f} W   p99 {result['p99']:6.0f} W")
    print(f"  binnen 100 W {result['within'][100]:5.1f}%   250 W {result['within'][250]:5.1f}%   500 W {result['within'][500]:5.1f}%")
    print(f"  schakelingen {result['switches']}")
    print(f"  tick mediaan {result['tick_ms']['median']:.3f} ms   p99 {result['tick_ms']['p99']:.3f} ms   max {result['tick_ms']['max']:.2f} ms")
    if "l1" in data:
        print(f"  fase boven de grens {result['over_s']:.0f} s (opgenomen {result['recorded_over_s']:.0f} s)   hoogste fase {result['phase_peak']:.0f} W")
    for key, soc in result["soc"].items():
        print(f"    {key:<24} soc {data['soc'][key][0]:5.1f} -> {soc:5.1f}")


if __name__ == "__main__":
    asyncio.run(main())
