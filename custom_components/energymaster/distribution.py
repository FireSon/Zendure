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

from .device import DEADBAND_W, PowerDevice
from .entity import EmDevice
from .number import EmNumber
from .solver.solver import LpSolver, SolverData, SolverEntry
from .select import EmSelect
from .sensor import EmSensor
from .switch import EmSwitch


_LOGGER = logging.getLogger(__name__)

STORAGE_COST_PER_KWH = 0.03
WEAR_BIAS = 0.6  # window-order: 7-dagen-cyclusdelta weegt mee in de rangorde
EPS_KW = 0.01  # below this an LP charge/discharge/grid flow counts as zero
ADD_WS = 2000.0  # bijschakelen: integraal van tekort boven de 95%-band (sweep-optimum 2026-07-11)
DROP_WS = 12000.0  # afschakelen: integraal van overschot onder de 30%-band
DIRECTION_WS = 3000.0  # richtingwissel-integraal laden<->ontladen
DROP_COOLDOWN_S = 600.0  # na bijschakelen 10 min geen drop (cyclende lasten)
START_W = 60  # make-before-break instapvermogen
ACTIVE_W = 40  # inkomend device telt mee zodra het dit levert


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

        self._commanded: dict[str, float] = {}
        self._bat_charge = self._bat_discharge = self._bat_soc = None  # aggregate SolverData, set in init_run
        self.charge_order: list = []  # postrun: volledige ranking per richting, 15 min vast
        self.discharge_order: list = []
        self._weights: dict[bool, dict[str, float]] = {False: {}, True: {}}  # bevroren kWh-gewichten per window
        self._active: list[PowerDevice] = []  # actieve set, in order-volgorde
        self._incoming: PowerDevice | None = None  # make-before-break instapper
        self._swap_out: PowerDevice | None = None  # valt af zodra de instapper levert (window-rotatie)
        self._charging = False
        self._add_ws = self._drop_ws = self._direction_ws = 0.0
        self._last_add = 0.0
        self._last_tick = 0.0

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
            self._reset_dispatch()
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

    def _build_orders(self, now: float) -> None:
        cycles = [d.wear_cycles() for d in self.devices if d.capacity_kwh]
        mean = (sum(cycles) / len(cycles) or 1.0) if cycles else 1.0
        for charging in (False, True):
            weights = {}
            for d in self.devices:
                span = d.soc_max.asNumber - d.soc_min.asNumber
                head = ((d.soc_max.asNumber - d.soc.asNumber) if charging else (d.soc.asNumber - d.soc_min.asNumber)) / span if span else 0.0
                wear = d.wear_cycles() / mean
                d.score[charging] = max(0.0, head) ** 2 / (1.0 + WEAR_BIAS * wear)
                weights[d.device_id] = max(0.0, head) * d.capacity_kwh  # ontladen: beschikbare kWh, laden: vrije kWh
            order = sorted((d for d in self.devices if d.score[charging] > 0), key=lambda d: d.score[charging], reverse=True)
            self._weights[charging] = weights
            if charging:
                self.charge_order = order
            else:
                self.discharge_order = order
        # rotatie op de windowgrens: nieuwe kop wisselt in via make-before-break
        order = self.charge_order if self._charging else self.discharge_order
        if self._active and order and order[0] not in self._active and self._incoming is None:
            self._incoming = order[0]
            ranked = sorted(self._active, key=lambda d: order.index(d) if d in order else 99)
            self._swap_out = ranked[-1]

    def _reset_dispatch(self) -> None:
        self._active = []
        self._incoming = self._swap_out = None
        self._add_ws = self._drop_ws = self._direction_ws = 0.0

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

        self._build_orders(time.time())

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
        if mode in (DistributionMode.GRID_CHARGE, DistributionMode.GRID_DISCHARGE):
            # Find the next mode change
            cnt = 12
            while cnt < 12 + lp.horizon and planned_mode(cnt) == mode:
                cnt += 1

            charging = bool(charge[12] > 0)  # python bool: numpy.bool cannot index profile/score lists (py3.14)
            price = lp.price_buy.data[12:cnt] if charging else lp.price_sell.data[12:cnt]
            better = sum(t < price[0] for t in price) if charging else sum(t > price[0] for t in price)

            participants = []
            for d in (self.charge_order if charging else self.discharge_order):  # GRID: order beperken tot prijsvenster-deelnemers
                kwh15m = 0.25 * abs(d.profile[charging].power) / 1000
                if kwh15m <= 0:
                    continue
                slots = ceil(d.available_kwh.asNumber / kwh15m if not charging else (d.total_kwh.asNumber - d.available_kwh.asNumber) / kwh15m)
                if better < slots:
                    participants.append(d)
            if charging:
                self.charge_order = participants
            else:
                self.discharge_order = participants

    async def _process_p1(self, p1: int, now: float) -> None:
        now = now or time.time()

        # get all solar from solar panels
        solar: int = sum(self.solar)

        output = 0
        for d in self.devices:
            output += d.output_power(now)
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
        dt = min(5.0, now - self._last_tick) if self._last_tick else 1.0
        self._last_tick = now
        charging_wanted = setpoint < 0
        sp = abs(setpoint)

        # richtingwissel pas na de integraal; tot die tijd afbouwen naar 0
        if self._active and charging_wanted != self._charging:
            self._direction_ws += sp * dt
            if self._direction_ws < DIRECTION_WS:
                for d in self._active:
                    await d.ramp_to(0.0, self._charging, now)
                return
            for d in self._active:
                await d.on_setpoint(0, d.profile[self._charging], now)
            self._active = []
            self._incoming = self._swap_out = None
            self._add_ws = self._drop_ws = 0.0
        self._direction_ws = 0.0
        self._charging = charging_wanted
        order = self.charge_order if charging_wanted else self.discharge_order
        if not order:  # nog geen solve gedraaid
            self._build_orders(now)
            order = self.charge_order if charging_wanted else self.discharge_order

        # make-before-break: inkomend device wordt volwaardig lid zodra het echt levert
        if self._incoming is not None and abs(self._incoming.output_power(now)) >= ACTIVE_W:
            self._active.append(self._incoming)
            self._active.sort(key=lambda d: order.index(d) if d in order else 99)
            self._incoming = None
            if self._swap_out is not None:
                if self._swap_out in self._active:
                    self._active.remove(self._swap_out)
                    await self._swap_out.on_setpoint(0, self._swap_out.profile[self._charging], now)
                self._swap_out = None

        # bijschakelen: integraal van het tekort boven de 95%-band; lege set schakelt direct
        cap_high = sum(d.profile[self._charging].band_high for d in self._active)
        if sp > cap_high:
            self._add_ws += (sp - cap_high) * dt
            if (self._add_ws >= ADD_WS or not self._active) and self._incoming is None:
                candidates = [d for d in order if d not in self._active]
                if candidates:
                    if self._active:
                        self._incoming = candidates[0]
                    else:
                        self._active.append(candidates[0])
                    self._last_add = now
                self._add_ws = 0.0
        else:
            self._add_ws = 0.0

        # afschakelen: integraal van het overschot onder de 30%-band, met cooldown en haalbaarheids-gate
        if len(self._active) > 1:
            cap_low = sum(d.profile[self._charging].band_low for d in self._active)
            remaining_high = cap_high - self._active[-1].profile[self._charging].band_high
            if sp < cap_low and remaining_high >= sp:
                self._drop_ws += (cap_low - sp) * dt
                if self._drop_ws >= DROP_WS and now - self._last_add > DROP_COOLDOWN_S:
                    dropped = self._active.pop()
                    await dropped.on_setpoint(0, dropped.profile[self._charging], now)
                    self._drop_ws = 0.0
            else:
                self._drop_ws = 0.0

        # verdeling: naar rato van de bevroren window-gewichten, geclampt op de band; laatste neemt het restant
        weights = self._weights[self._charging]
        total_weight = sum(weights.get(d.device_id, 0.0) for d in self._active) or 1.0
        remainder = float(sp)
        for i, d in enumerate(self._active):
            p = d.profile[self._charging]
            if i == len(self._active) - 1:
                share = remainder  # laatste/enige device mag onder de 30%-band
            else:
                share = min(sp * weights.get(d.device_id, 0.0) / total_weight, p.band_high)
                share = max(share, abs(p.floor))  # harde 10%-vloer overrult naar-rato
            share = max(0.0, min(share, p.band_high, remainder))
            remainder -= share
            await d.ramp_to(share, self._charging, now)
        if self._incoming is not None:
            await self._incoming.on_setpoint(-START_W if self._charging else START_W, self._incoming.profile[self._charging], now)  # vaste instap-probe, geen ramp
        for d in self.devices:
            if d not in self._active and d is not self._incoming and d.setpoint != 0:
                await d.on_setpoint(0, d.profile[self._charging], now)

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
                self._reset_dispatch()
                if len(self.devices) > 0:
                    for d in self.devices:
                        await d.power_off()
