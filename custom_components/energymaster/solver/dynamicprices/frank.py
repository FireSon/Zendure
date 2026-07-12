from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientSession, ClientTimeout

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_LOGGER = logging.getLogger(__name__)
_FE_URL = "https://frank-graphql-prod.graphcdn.app/"
_HTTP_TIMEOUT = ClientTimeout(total=5)
_QUERY = """
query MarketPrices($startDate: Date!, $endDate: Date!) {
  marketPricesElectricity(startDate: $startDate, endDate: $endDate) {
    from till marketPrice marketPriceTax sourcingMarkupPrice energyTaxPrice
  }
}
"""


class FrankProvider(Provider):
    market_timezone = "Europe/Amsterdam"

    def init(self, dynamicprice: DynamicPrice):
        pass

    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        await super().fetch(dynamicPrice, now)

        # Load payload from api
        variables = {"startDate": now.date().isoformat(), "endDate": (now.date() + timedelta(days=2)).isoformat()}
        body = {"query": _QUERY, "variables": variables}
        try:
            async with ClientSession() as session:
                async with session.post(_FE_URL, json=body, timeout=_HTTP_TIMEOUT) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Frank Energie fetch failed: {err}") from err
        try:
            rows = payload["data"]["marketPricesElectricity"]
        except (KeyError, TypeError) as err:
            raise ProviderError(f"Frank Energie payload malformed: {err}") from err

        # real day-ahead aligned by each row's 'from' timestamp (slot 12 = the quarter containing now),
        # mirroring the base fetch; keep the last fixed components for the forecast tail
        buy = dynamicPrice.price_buy.data
        sell = dynamicPrice.price_sell.data
        size = buy.size
        base = int(now.timestamp()) // 900 * 900 - 12 * 900
        sourcing = etx = 0.0
        tail = 12
        for row in rows:
            try:
                start = int(datetime.fromisoformat(row["from"]).timestamp())
            except (KeyError, TypeError, ValueError):
                continue  # a row without a valid timestamp cannot be placed

            # row duration from the data itself: hourly rows fill 4 quarters, 15-min rows 1
            try:
                span = max(1, (int(datetime.fromisoformat(row["till"]).timestamp()) - start) // 900)
            except (KeyError, TypeError, ValueError):
                span = 4  # no usable till: assume the historical hourly format
            try:
                buy_excl = float(row["marketPrice"])
                tax = float(row.get("marketPriceTax") or 0.0)
                sourcing = float(row.get("sourcingMarkupPrice") or 0.0)
                etx = float(row.get("energyTaxPrice") or 0.0)
            except (KeyError, TypeError, ValueError) as err:
                raise ProviderError(f"Frank Energie row malformed: {err}") from err
            all_in = buy_excl + tax + sourcing + etx
            idx = (start - base) // 900  # quarter index of this row
            for q in range(span):
                slot = idx + q
                if 0 <= slot < size:
                    buy[slot] = all_in
                    sell[slot] = buy_excl
                    tail = max(tail, slot + 1)

        # remaining forecast slots: give the tail the real day's intraday shape, then all-in buy (last fixed components + VAT%)
        self.extend_tail(sell, tail, base)
        vat = 1 + self.vat / 100.0
        buy[tail:] = (sell[tail:] + etx + sourcing) * vat

