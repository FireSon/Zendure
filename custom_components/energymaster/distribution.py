"""Distribution: fleet manager."""

from __future__ import annotations

from datetime import datetime
import logging
from math import ceil
import time
from enum import Enum
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

import numpy as np

from .device import PowerDevice
from .entity import EmDevice
from .number import EmNumber
from .solver.solver import LpSolver, SolverData, SolverEntry
from .select import EmSelect
from .sensor import EmSensor
from .switch import EmSwitch


_LOGGER = logging.getLogger(__name__)

START_W = 50
DEADBAND_W = 20
FLOOR_PCT = 0.10
REDIRECT_WINDOW = 3.0
STORAGE_COST_PER_KWH = 0.03
WEAR_BIAS = 0.6  # wear-balance: scales each unit's cycle cost by its prior usage vs the fleet mean
INERTIA_KEEP = 0.7  # anti-flap: relative score kept for units not currently running
EPS_KW = 0.01  # below this an LP charge/discharge/grid flow counts as zero


class DistributionMode(Enum):
    OFF = 0
    MANUAL = 1
    MATCHING = 2
    MATCHING_DISCHARGE = 3
    MATCHING_CHARGE = 4
    STORE_SOLAR = 5
    PEAK_SHAVING = 6
    GRID_CHARGE = 7
    GRID_DISCHARGE = 8
    IDLE = 9


class Distribution(SolverEntry, EmDevice):
    """Per-meter fleet manager + battery LP entry (active when self.devices is non-empty)."""

    def __init__(self, hass: HomeAssistant, all_devices: list[PowerDevice]) -> None:
        EmDevice.__init__(self, hass, "EnergyMaster", "EnergyMaster")
        SolverEntry.__init__(self)
        self.hass = hass
        self.name: str = ""
        self.p1_device: str = ""
        self.max_kw: float = 17.0
        self.phase_limit_kw = 17.0 / 3
        self.solar: list[float] = []
        self.power: list[float] = [0, 0, 0]

        self.devices = all_devices
        self.setpoint: int = 0
        self.manual_setpoint: int = 0
        self._mode = DistributionMode.OFF
        self.auto: bool = False  # master: True = EnergyMaster drives the mode per slice, False = manual (mode select / HA)
        self.max_grid: int = 0

        self._power_reselect: float = 0.0  # next time the device selection may be re-scored (120s throttle)
        self._commanded: dict[str, float] = {}
        self._bat_charge = self._bat_discharge = self._bat_soc = None  # aggregate SolverData, set in init_run
        self.charge_order: list = []  # postrun: participating devices sorted for charging
        self.discharge_order: list = []  # postrun: participating devices sorted for discharging

    def device_entities(self):
        self.optimize = EmSwitch("optimize", on_changed=self._set_auto, restore=True, default=False)  # master auto/manual switch (UI + HA)
        self.mode = EmSelect("Mode", {0: "off", 1: "manual", 2: "smart", 3: "smart_discharging", 4: "smart_charging", 5: "store_solar"}, self.update_mode, restore=True)
        self.manualpower = EmNumber("manual_power", uom="W", minimum=-10000, maximum=10000, on_write=self.entity_write, restore=True)

        # Batterij-fleet aggregate (som / cap-gewogen over alle units, elke cyclus in post_run)
        self.soc = EmSensor("soc", uom="%", device_class="battery", state_class="measurement", precision=1)
        self.capacity_kwh = EmSensor("capacity_kwh", uom="kWh", device_class="energy_storage", state_class="measurement", precision=1)
        self.available_kwh = EmSensor("available_kwh", uom="kWh", device_class="energy_storage", state_class="measurement", precision=1)
        self.charge_power = EmSensor("charge_power", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.discharge_power = EmSensor("discharge_power", uom="W", device_class="power", state_class="measurement", aggr=True)

        # Systeemtotalen uit de laatste solve (post_run)
        self.solar_power = EmSensor("solar_power", uom="W", device_class="power", state_class="measurement", icon="mdi:solar-power", aggr=True)
        self.house_load = EmSensor("house_load", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.grid_import = EmSensor("grid_import", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.grid_export = EmSensor("grid_export", uom="W", device_class="power", state_class="measurement", aggr=True)
        self.cost_today = EmSensor("cost_today", uom="EUR", device_class="monetary", state_class="total", precision=2)
        self.savings_today = EmSensor("savings_today", uom="EUR", device_class="monetary", state_class="total", precision=2)
        self.autarky = EmSensor("autarky", uom="%", state_class="measurement", precision=0)

    def device_update(self, dev: PowerDevice) -> None:
        self.capacity_kwh.update_value(sum(d.capacity_kwh for d in self.devices))
        self.available_kwh.update_value(sum(d.available_kwh.asNumber for d in self.devices))

    def entity_write(self, entity, value) -> None:
        # the manual_power number drives the fleet setpoint (negative = charge, positive = discharge)
        if entity is self.manualpower:
            self.manual_setpoint = int(value)

    async def _set_auto(self, entity, on) -> None:
        # master switch: ON -> the mode follows the solver plan; OFF -> back to off (manual/HA)
        self.auto = bool(on)
        if self.auto:
            self._apply_plan_mode()
        else:
            self._mode = DistributionMode.OFF
            self.mode.update_value(DistributionMode.OFF.value)
            for d in self.devices:
                await d.power_off()

    def _plan_direction(self) -> DistributionMode:
        # the mode implied by the solver plan for the current slot, mapped to a selectable mode
        if self._bat_charge is None or self._bat_charge.dest is None:
            return DistributionMode.OFF
        net = self._bat_charge.dest.data[12] - self._bat_discharge.dest.data[12]  # >0 charge, <0 discharge
        if net > EPS_KW:
            return DistributionMode.MATCHING_CHARGE
        if net < -EPS_KW:
            return DistributionMode.MATCHING_DISCHARGE
        return DistributionMode.OFF

    def _apply_plan_mode(self) -> None:
        m = self._plan_direction()
        self._mode = m
        self.mode.update_value(m.value)  # reflect the plan-chosen mode in the UI select
        if self.auto:  # auto aan: zet de mode op off; het plan bepaalt de mode per slice
            self._mode = DistributionMode.OFF
            self.mode.update_value(DistributionMode.OFF.value)

    async def load_state(self) -> None:
        await super().load_state()
        self._mode = DistributionMode(self.mode.value or 0)
        self.manual_setpoint = int(self.manualpower.asNumber)  # restore-persisted manual power
        self.auto = bool(self.optimize.asNumber)  # restore-persisted master switch

    def entry_config(self, lp: LpSolver):
        # publish house-wide values to the shared globals so other entries (e.g. BaseLoad) can read them
        self.phase_limit_kw = self.max_kw / 3
        lp.globals["phase_limit_kw"] = self.phase_limit_kw

        # Solver columns
        self.grid_buy = lp.column(tags=("supply",), dest=lp.data("grid_buy", "kW"))
        self.grid_sell = lp.column(tags=("draw",), dest=lp.data("grid_sell", "kW"))

        self.gain = 0.0
        self._bat_charge = charge = lp.column(hi=0.0, tags=("draw",), dest=lp.data("bat_charge", "kW"))
        self._bat_discharge = discharge = lp.column(hi=0.0, tags=("supply",), dest=lp.data("bat_discharge", "kW"))
        self._bat_soc = soc = lp.column(lo=0.0, hi=1.0, dest=lp.data("bat_soc", "0-1"))
        self._rebuild_soc = lp.rowDelta(lambda t1, t2: soc[t1] - soc[t2] - self.gain * charge[t2] + self.gain * discharge[t2], const=0.0)

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        h = lp.horizon
        limit = self.max_kw
        for t in range(h):
            self.grid_buy.costs[t] = lp.price_buy.data[12 + t] * 0.25
            self.grid_sell.costs[t] = -lp.price_sell.data[12 + t] * 0.25
            self.grid_buy.upper[t] = limit
            self.grid_sell.upper[t] = limit

        # aggregaat-batterij: SoC slot 0 = live fleet-stand, eind-energie waarderen op het prijsniveau net na de horizon
        if self.capacity_kwh.asNumber:
            cap = self.capacity_kwh.asNumber
            if self.gain != 0.25 / cap:
                self.gain = 0.25 / cap
                self._rebuild_soc()
            self._bat_charge.upper[0:h] = 0.75 * sum(abs(dev.profile[True].power) for dev in self.devices) / 1000.0
            self._bat_discharge.upper[0:h] = 0.75 * sum(abs(dev.profile[False].power) for dev in self.devices) / 1000.0
            self._bat_charge.upper[h - 1] = self._bat_discharge.upper[h - 1] = 0.0  # last flow slot is not SoC-coupled -> pin to 0 so it cannot free-dump
            self._bat_soc.lower[0] = self._bat_soc.upper[0] = self.available_kwh.asNumber / cap
            self._bat_soc.costs[h - 1] = -cap * float(np.mean(lp.price_sell.data[12 + h : 12 + h + 16]))  # realiseerbare restwaarde = verkoopprijs (voorkomt kopen-om-te-hamsteren)
            self._bat_discharge.costs[0:h] = 0.03 * 0.25

    def entry_postrun(self, lp: LpSolver):
        # projected 24h net cost + savings vs buying everything (the instantaneous sensors are updated live in _process_p1)
        pb, ps = lp.price_buy.data[12:108], lp.price_sell.data[12:108]
        cost = float(np.sum(self.grid_buy.dest.data[12:108] * pb - self.grid_sell.dest.data[12:108] * ps)) * 0.25
        load24 = lp.l1[:96] + lp.l2[:96] + lp.l3[:96]
        baseline = float(np.sum(np.maximum(0.0, load24 - lp.solar.data[12:108]) * pb)) * 0.25
        self.cost_today.update_value(cost)
        self.savings_today.update_value(max(0.0, baseline - cost))

        if self.auto:  # master ON: each solve, the mode follows the fresh plan
            self._apply_plan_mode()

        if not self.devices:
            return

        charge = self._bat_charge.dest.data
        discharge = self._bat_discharge.dest.data
        grid_import = self.grid_buy.dest.data
        grid_export = self.grid_sell.dest.data

        def planned_mode(slot: int) -> DistributionMode:
            net = charge[slot] - discharge[slot]
            if net > EPS_KW:
                return DistributionMode.GRID_CHARGE if grid_import[slot] > EPS_KW else DistributionMode.MATCHING_CHARGE
            if net < -EPS_KW:
                return DistributionMode.GRID_DISCHARGE if grid_export[slot] > EPS_KW else DistributionMode.MATCHING_DISCHARGE
            return DistributionMode.IDLE

        mode = planned_mode(12)
        self.charge_order = []
        self.discharge_order = []
        if mode in (DistributionMode.GRID_CHARGE, DistributionMode.GRID_DISCHARGE):
            # Find the next mode change
            cnt = 12
            while cnt < 12 + lp.horizon and planned_mode(cnt) == mode:
                cnt += 1

            charging = bool(charge[12] > 0)  # python bool: numpy.bool cannot index profile/score lists (py3.14)
            price = lp.price_buy.data[12:cnt] if charging else lp.price_sell.data[12:cnt]
            better = sum(t < price[0] for t in price) if charging else sum(t > price[0] for t in price)

            order = []
            for d in self.devices:
                d.score = [0.0, 0.0]
                kwh15m = 0.25 * abs(d.profile[charging].power) / 1000
                if kwh15m <= 0:
                    continue
                slots = ceil(d.available_kwh.asNumber / kwh15m if not charging else (d.total_kwh.asNumber - d.available_kwh.asNumber) / kwh15m)
                if better < slots:
                    d.score[charging] = 100.0
                    order.append(d)
            self.charge_order = order if charging else []
            self.discharge_order = [] if charging else order
        # matching / idle: leave the orders empty, _process_p1 builds the live order on demand
        self._power_reselect = 0.0  # next P1 tick reselects against the fresh order

    async def _process_p1(self, p1: int, now: float) -> None:
        now = now or time.time()

        # get all solar from solar panels
        solar: int = sum(self.solar)

        active = 0
        output = 0
        pwr_range = [0, 0]
        for d in self.devices:
            if (power := d.output_power(now)) != 0 and (p := d.profile[power < 0]):
                active += 1
                output += power
                p.power_range(pwr_range)
            solar += d.solar.asNumber
        setpoint = p1 + output

        # live weergavesensoren uit de actuele telemetrie (elke P1-tick, ongeacht mode)
        cap = self.capacity_kwh.asNumber
        house = p1 + solar + output  # verbruik = net-import + zon + batterij-levering
        self.grid_import.update_value(max(0, p1))
        self.grid_export.update_value(max(0, -p1))
        self.solar_power.update_value(solar)
        self.discharge_power.update_value(max(0, output))
        self.charge_power.update_value(max(0, -output))
        self.house_load.update_value(max(0, house))
        self.soc.update_value(self.available_kwh.asNumber / cap * 100.0 if cap else 0.0)
        self.autarky.update_value(max(0.0, min(100.0, (house - max(0, p1)) / house * 100.0)) if house > 0 else 0.0)

        if self._mode == DistributionMode.MANUAL:
            setpoint = self.manual_setpoint
        elif self._mode == DistributionMode.MATCHING_DISCHARGE:
            setpoint = max(setpoint, 0)
        elif self._mode == DistributionMode.MATCHING_CHARGE:
            setpoint = min(setpoint, 0)
        elif self._mode == DistributionMode.STORE_SOLAR:
            setpoint = min(setpoint, 0) if solar > 0 else 0
        elif self._mode == DistributionMode.PEAK_SHAVING:
            setpoint = max(0, setpoint - self.max_grid)

        if self.max_grid > 0:
            setpoint = max(setpoint, p1 + output - self.max_grid)

        if self._mode == DistributionMode.OFF or abs(output - setpoint) < DEADBAND_W:
            return
        self.setpoint = setpoint
        charging = setpoint < 0
        order = self.charge_order if charging else self.discharge_order
        if not order:  # no solver plan yet -> build the live matching order for this direction
            cycles = [d.battery_output.asNumber / d.capacity_kwh for d in self.devices if d.capacity_kwh]
            mean = (sum(cycles) / len(cycles) or 1.0) if cycles else 1.0
            for d in self.devices:
                span = d.soc_max.asNumber - d.soc_min.asNumber
                head = (d.soc_max.asNumber - d.soc.asNumber if charging else d.soc.asNumber - d.soc_min.asNumber) / span if span else 0.0
                fit = 1.0 if abs(setpoint) >= d.profile[charging].floor else abs(setpoint) / d.profile[charging].floor
                inertia = 1.0 if d.setpoint != 0 else INERTIA_KEEP
                wear = (d.battery_output.asNumber / d.capacity_kwh) / mean if d.capacity_kwh else 1.0
                d.score[charging] = max(0.0, head) ** 2 * fit * inertia / (1.0 + WEAR_BIAS * wear)
            order = sorted((d for d in self.devices if d.score[charging] > 0), key=lambda d: d.score[charging], reverse=True)

        # Pick how many of the order to engage when the running set can't cover the setpoint or the throttle expired.
        if not pwr_range[0] <= setpoint <= pwr_range[1] or now >= self._power_reselect:
            active = 0
            pwr_range = [0, 0]
            while active < len(order) and abs(0.92 * pwr_range[not charging]) < abs(setpoint):
                order[active].profile[charging].power_range(pwr_range)
                active += 1
            self._power_reselect = now + 120

        total_score = sum(d.score[charging] for d in order[:active])
        low_demand = abs(setpoint) < abs(pwr_range[charging])
        for i, d in enumerate(order):
            if i < active and (d.inefficient_time == 0 or now - d.inefficient_time < 300):
                p = d.profile[charging]
                if low_demand and active > i + 1 and pwr_range[not charging] > 0:
                    share = int(setpoint * p.power / pwr_range[not charging])
                elif active == i + 1 or total_score == 0 or p.floor >= setpoint:
                    share = setpoint
                else:
                    share = p.floor + int((setpoint - pwr_range[charging]) * d.score[charging] / total_score)
                gap = share - d.setpoint
                if abs(gap) > DEADBAND_W:
                    share = d.setpoint + int(gap * (p.f_up if abs(share) > abs(d.setpoint) else p.f_down))
                setpoint -= await d.on_setpoint(share, p, now)
                pwr_range[charging] -= p.floor
                total_score -= d.score[charging]
            else:
                await d.on_setpoint(0, d.profile[charging], now)
        for d in self.devices:
            if d not in order:
                await d.on_setpoint(0, d.profile[charging], now)

    async def update_mode(self, entity: EmSelect, _operation: Any) -> None:
        mode = DistributionMode(entity.value)
        _LOGGER.info("Update operation: %s from: %s", mode, self._mode)

        self._mode = mode
        if mode != DistributionMode.OFF and (len(self.devices) == 0 or all(not d.online for d in self.devices)):
            _LOGGER.warning("No devices online, not possible to start the operation")
            persistent_notification.async_create(self.hass, "No devices online, not possible to start the operation", "EnergyMaster", "EnergyMaster")
            return

        match self._mode:
            case DistributionMode.OFF:
                if len(self.devices) > 0:
                    for d in self.devices:
                        await d.power_off()
