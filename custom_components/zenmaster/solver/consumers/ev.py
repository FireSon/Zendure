from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from ..solver import LpSolver, SolverEntry
from ...config import ConfigField
from ...consumer import ConsumerDevice
from ...const import Phase

_CHARGER_MIN_KW = 4.2          # the wallbox cannot charge below this power
_SOLAR_PRICE_FRACTION = 0.33   # solar charges while the blended price stays under this fraction of the compensation


class ChargeMode(IntEnum):
    off = 0     # not controlled
    solar = 1   # opportunistic up to ev_max while it pays, no deadline
    plan = 2    # reach ev_max_soc before departure, then opportunistic
    fast = 3    # straight to max, ignores price and solar


class ElectricVehicle(SolverEntry, ConsumerDevice):
    # charge-only EV, planned deterministically while the cable is connected.
    # Fields without a cast are settings: entities on the device, kept by HA (see ConsumerDevice).
    CONFIG = [
        ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True, hidden=True),
        ConfigField("name", {"text": {}}),  # the subentry title: HA names the device and its entities after it
        ConfigField("charge_power_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("soc_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("connected_entity", {"entity": {}}, cast=str, section_key="link"),
        ConfigField("power_entity", {"entity": {}}, cast=str, required=False, section_key="link"),
        ConfigField("manage", {"boolean": {}}, section_key="behaviour"),
        ConfigField("departure_time", {"time": {}}, section_key="behaviour"),
        ConfigField("ev_max_soc", {"number": {"mode": "box", "step": 0.1}}, section_key="behaviour"),
        ConfigField("charge_compensation_eur_kwh", {"number": {"mode": "box", "step": 0.01}}, section_key="behaviour"),
        ConfigField("capacity_kwh", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kWh"}}, section_key="expert"),
        ConfigField("max_power_kw", {"number": {"mode": "box", "step": 0.1, "unit_of_measurement": "kW"}}, section_key="expert"),
        ConfigField("phase", {"select": {"options": [{"value": "0", "label": "Alle fasen"}, {"value": "1", "label": "L1"}, {"value": "2", "label": "L2"}, {"value": "3", "label": "L3"}]}}, section_key="expert"),
        ConfigField("state", {"entity": {}}, readonly=True),
    ]

    def __init__(self):
        super().__init__()
        self.id = ""
        self.charge_power_entity = ""
        self.soc_entity = ""
        self.connected_entity = ""
        self.connected = False
        self.current_soc = 0.0
        self.capacity_kwh = 0.0
        self.max_power_kw = 0.0
        self.ev_max_soc = 0.0
        self.charge_compensation_eur_kwh = 0.0
        self.departure_time = ""
        self.phase = Phase.all
        self.mode = ChargeMode.solar

    def _write(self, kw: float):
        # the charge power on the charger: a number and an input_number have their own set_value
        return self.hass.services.async_call(self.charge_power_entity.split(".")[0], "set_value", {"entity_id": self.charge_power_entity, "value": round(kw, 2)}, blocking=False)

    def release(self) -> None:
        # hand the charger back: without a plan there is no limit on it, so it may charge at full power
        if self.charge_power_entity and self.hass is not None:
            self.hass.async_create_task(self._write(self.max_power_kw))

    def entry_config(self, lp: LpSolver):
        self.mode = ChargeMode(self.mode)  # accept the plain int value coming from the config
        self.ev_power = lp.data(self.id + "_power", "kW")
        lp.track(self.soc_entity, lambda v: setattr(self, "current_soc", v / 100.0))  # HA reports %, we hold a 0-1 fraction
        lp.state(self.connected_entity, lambda s: setattr(self, "connected", s == "on"))

    async def apply_power(self) -> None:
        # the planned charge power for this quarter
        if not self.charge_power_entity or self.hass is None:
            return
        await self._write(self.ev_power.data[12])

    async def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        self.ev_power.data[12:] = 0.0
        if self.mode is ChargeMode.off or not self.connected:
            return

        max_kwh = (self.ev_max_soc - self.current_soc) * self.capacity_kwh
        if max_kwh <= 0.0:
            return

        now_slot = (now.hour * 60 + now.minute) // 15
        try:
            h, m = self.departure_time.split(":")[:2]  # "HH:MM", or "HH:MM:SS" the way HA's time entity keeps it
            slots_until_departure = (int(h) * 4 + int(m) // 15 - now_slot) % 96 or 96
        except (ValueError, AttributeError):
            slots_until_departure = 96

        if self.mode is ChargeMode.fast:
            for c in range(12, 12 + slots_until_departure):
                if max_kwh > 0:
                    kwh = lp.phase_room(self.phase, c - 12, min(max_kwh, self.max_power_kw * 0.25))
                    max_kwh -= kwh
                    self.ev_power.data[c] = kwh * 4
        else:
            limit = self.charge_compensation_eur_kwh * (1 if self.mode is ChargeMode.plan else _SOLAR_PRICE_FRACTION)
            max_kw = self.max_power_kw * 0.25

            def affordable(i, sell, buy):
                # kW -> kWh for this quarter
                solar = lp.solar.data[i] * 0.25  
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
                if max_kwh > 0:
                    kwh = min(max_kwh, kwh)
                    self.ev_power.data[i] = kwh * 4
                    max_kwh -= kwh

        lp.phase_add(self.phase, self.ev_power.data[12:108], 0, slots_until_departure)

        await self.apply_power()
