from __future__ import annotations

from datetime import datetime

from ..dynamicprice import DynamicPrice, Provider


class NextEnergyProvider(Provider):
    # not reverse-engineered: no real day-ahead, indicative wholesale forecast + tariffs overlay only
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        buy, sell = await super().fetch(dynamicPrice, now)  # raw wholesale forecast in working copies
        markup, vat = self.import_markup + self.energy_tax, 1 + self.vat / 100.0
        buy[12:] = (sell[12:] * self.import_factor + markup + self.tou[12:]) * vat   # no day-ahead; slots 0..11 untouched
        sell[12:] = sell[12:] * self.export_factor + self.export_markup  # the feed-in price; buy above was built on the bare spot
        return buy, sell
