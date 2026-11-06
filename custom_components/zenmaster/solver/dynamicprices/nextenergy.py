from __future__ import annotations

from datetime import datetime

from ..dynamicprice import DynamicPrice, Provider


class NextEnergyProvider(Provider):
    # not reverse-engineered: no real day-ahead, indicative wholesale forecast + tariffs overlay only
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        await super().fetch(dynamicPrice, now)   # raw wholesale forecast in buy + sell
        buy = dynamicPrice.price_buy.data
        sell = dynamicPrice.price_sell.data
        markup, vat, fixed = self.import_markup + self.energy_tax, 1 + self.vat / 100.0, self.fixed_cost_per_day / 96.0
        buy[12:] = (sell[12:] * self.import_factor + markup + self.tou[12:]) * vat + fixed   # no day-ahead; slots 0..11 untouched
        sell[12:] = sell[12:] * self.export_factor + self.export_markup  # the feed-in price; buy above was built on the bare spot
