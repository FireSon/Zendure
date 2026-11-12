from __future__ import annotations

import time
from datetime import datetime
from enum import IntEnum

from ..solver import EntryStatus, LpSolver, SolverEntry
from ...config import ConfigField
from ...consumer import HEAD, MANAGE, TAIL, ConsumerDevice
from ...const import Phase, time_to_slot, watt_factor

_CHARGER_MIN_KW = 4.2  # the wallbox cannot charge below this power
_STEP_S = 30  # Solar follows the surplus at most once per this many seconds


class ChargeMode(IntEnum):
    maximum = 1  # straight to max, ignores price and solar
    ready_by = 2  # reach soc_target before departure, whatever the price
    price = 3  # opportunistic up to ev_max while it pays, no deadline
    solar = 4  # the solar surplus into the car, at least 4.2 kW
    v2h = 5  # the car as house battery; not built yet, plans like price


class ElectricVehicle(SolverEntry, ConsumerDevice):
    # charge-only EV, planned deterministically while the cable is connected.
    # Fields without a cast are settings: entities on the device, kept by HA (see ConsumerDevice).
    CONFIG = [
        *HEAD,
        ConfigField("charge_power_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("soc_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("connected_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("power_entity", {"entity": {}}, cast=str, required=False, section_key="link"),
        MANAGE,
        ConfigField("departure_time", {"time": {}}, section_key="behaviour"),
        ConfigField("ev_charge_mode", {"select": {"options": [{"value": "1", "label": "Maximum"}, {"value": "2", "label": "Ready by"}, {"value": "3", "label": "Price"}, {"value": "4", "label": "Solar"}, {"value": "5", "label": "V2H"}]}}, section_key="behaviour"),
        ConfigField("ev_max_soc", {"number": {"mode": "box", "step": 0.1}}, section_key="behaviour"),
        ConfigField("soc_target", {"number": {"mode": "box", "step": 0.1}}, section_key="behaviour"),
        ConfigField("charge_compensation_eur_kwh", {"number": {"mode": "box", "step": 0.01}}, section_key="behaviour"),
        ConfigField("capacity_kwh", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kWh"}}, section_key="expert"),
        ConfigField("max_power_kw", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kW"}}, section_key="expert"),
        *TAIL,
    ]

    def __init__(self):
        super().__init__()
        self.id = ""
        self.charge_power_entity = ""
        self.soc_entity = ""
        self.connected_entity = ""
        self.current_soc = 0.0
        self.capacity_kwh = 0.0
        self.max_power_kw = 0.0
        self.ev_max_soc = 0.0
        self.soc_target = 0.0
        self.charge_compensation_eur_kwh = 0.0
        self.departure_time = ""
        self.phase = Phase.all
        self.ev_charge_mode = ChargeMode.price
        self._stepped = 0.0  # when on_surplus last wrote the charger
        self._written_kw = 0.0  # the last power written: the car's draw, since the cloud power lags
        self._surplus_sum, self._surplus_count = 0.0, 0  # the surplus readings since the last step

    @property
    def charge_target(self) -> float:
        # Ready by charges to its own departure target, every mode stays under the upper bound
        return min(self.soc_target, self.ev_max_soc) if self.ev_charge_mode is ChargeMode.ready_by else self.ev_max_soc

    def _write(self, kw: float):
        # the charge power in the charger's own unit (W, kW or A per phase), from the entity's unit and the car's phase
        self._written_kw = kw
        factor = watt_factor(self.hass.states.get(self.charge_power_entity), self.phase)
        return self.hass.services.async_call(self.charge_power_entity.split(".")[0], "set_value", {"entity_id": self.charge_power_entity, "value": round(1000 * kw / factor, 2)}, blocking=False)

    def release(self) -> None:
        # hand the charger back: without a plan there is no limit on it, so it may charge at full power
        if self.charge_power_entity and self.hass is not None:
            self.hass.async_create_task(self._write(self.max_power_kw))

    def entry_config(self, lp: LpSolver):
        self.ev_charge_mode = ChargeMode(self.ev_charge_mode)  # accept the plain int value coming from the config
        self.ev_power = lp.data(self.id + "_power", "kW")
        lp.track(self.soc_entity, lambda v: setattr(self, "current_soc", v / 100.0))  # HA reports %, we hold a 0-1 fraction
        # Solar follows the live surplus of the distribution within the quarter
        if (distribution := lp.globals.get("price_device")) is not None:
            lp.track(distribution.entities["surplus"].entity_id, self.on_surplus)
        # the charger back after its restart gets the plan of this quarter at once
        lp.track(self.charge_power_entity, lambda _state: self.hass.async_create_task(self._write(self.ev_power.data[12])), back=True)

    def on_surplus(self, watts: float) -> None:
        # Solar in a planned quarter: every 30 s the car takes its draw plus the mean surplus of those 30 s, at least 4.2 kW
        if self.ev_charge_mode is not ChargeMode.solar or self.ev_power.data[12] <= 0 or not self.charge_power_entity:
            return
        self._surplus_sum, self._surplus_count = self._surplus_sum + watts, self._surplus_count + 1
        if time.time() - self._stepped < _STEP_S:
            return
        mean = self._surplus_sum / self._surplus_count
        self._stepped, self._surplus_sum, self._surplus_count = time.time(), 0.0, 0
        self.hass.async_create_task(self._write(max(_CHARGER_MIN_KW, min(self.max_power_kw, self._written_kw + mean / 1000))))

    async def apply_power(self) -> None:
        # the planned charge power for this quarter
        if not self.charge_power_entity or self.hass is None:
            return
        await self._write(self.ev_power.data[12])

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        # every quarter the charger gets the plan of this quarter, also 0 when there is nothing to charge
        self.ev_power.data[12:] = 0.0
        # the cable as the entity shows it now: no copy that can miss a change
        connected = (s := lp.hass.states.get(self.connected_entity)) is not None and s.state == "on"
        if connected:
            self.plan(lp, now)
        else:
            self.entity_update("ev_charge_mode", ChargeMode.price)  # a mode holds for one session: back to the default once the cable is out
        # the status from the plan: charging this quarter, later, at its target, or nothing to do
        self.status = (EntryStatus.running if self.ev_power.data[12] > 0 else EntryStatus.planned if self.ev_power.data[12:].any()
            else EntryStatus.ready if connected and self.current_soc >= self.charge_target / 100 else EntryStatus.none)
        await self.apply_power()

    def plan(self, lp: LpSolver, now: datetime) -> None:
        # the charge power per quarter until departure, as far as the car still takes it
        max_kwh = (self.charge_target / 100 - self.current_soc) * self.capacity_kwh  # the target in %, current_soc a 0-1 fraction
        if max_kwh <= 0.0:
            return

        now_slot = (now.hour * 60 + now.minute) // 15
        # the quarters until departure; a full day when the time is not readable
        slots_until_departure = 96 if (slot := time_to_slot(self.departure_time)) is None else (slot - now_slot) % 96 or 96

        # the solar left after the planned house load per quarter (kW), index 0 = slot 12
        surplus = [max(0.0, lp.solar.data[c] - sum(load[c - 12] for load in lp.loads[Phase.all])) for c in range(12, 12 + slots_until_departure)]

        if self.ev_charge_mode in (ChargeMode.maximum, ChargeMode.solar):
            for c in range(12, 12 + slots_until_departure):
                # Maximum takes the full charger power, Solar only the solar surplus of the quarter
                cap = self.max_power_kw if self.ev_charge_mode is ChargeMode.maximum else min(self.max_power_kw, surplus[c - 12])
                # wallbox cannot modulate below 4.2 kW: a quarter or remainder under it is left out
                if max_kwh > 0 and (kwh := lp.phase_room(self.phase, c - 12, min(max_kwh, cap * 0.25))) >= _CHARGER_MIN_KW * 0.25:
                    max_kwh -= kwh
                    self.ev_power.data[c] = kwh * 4
        else:
            limit = float("inf") if self.ev_charge_mode is ChargeMode.ready_by else self.charge_compensation_eur_kwh
            max_kw = self.max_power_kw * 0.25

            def affordable(i, sell, buy):
                # the solar surplus, kW -> kWh for this quarter
                solar = surplus[i - 12] * 0.25
                if sell >= limit:
                    return 0.0, sell
                kwh = lp.phase_room(self.phase, i - 12, max_kw if buy <= limit else min(max_kw, solar * (buy - sell) / (buy - limit)))

                # wallbox cannot modulate below 4.2 kW
                if kwh < _CHARGER_MIN_KW * 0.25:
                    return 0.0, buy
                grid = max(0.0, kwh - solar)
                price = ((kwh - grid) * sell + grid * buy) / kwh if kwh > 0 else buy
                return kwh, price

            costs = [(i, affordable(i, lp.price_sell.data[i], lp.price_buy.data[i])) for i in range(12, 12 + slots_until_departure)]
            for i, (kwh, price) in sorted(costs, key=lambda t: t[1][1]):
                # wallbox cannot modulate below 4.2 kW: a remainder under it is left out
                if max_kwh > 0 and (kwh := min(max_kwh, kwh)) >= _CHARGER_MIN_KW * 0.25:
                    self.ev_power.data[i] = kwh * 4
                    max_kwh -= kwh

        lp.phase_add(self.phase, self.ev_power.data[12:108], 0, slots_until_departure)
