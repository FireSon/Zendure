from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_FE_URL = "https://graphql.frankenergie.nl/"
_HTTP_TIMEOUT = ClientTimeout(total=5)
_QUERY = """
query MarketPrices($date: String!, $resolution: PriceResolution!) {
  marketPrices(date: $date, resolution: $resolution) {
    electricityPrices { from till marketPrice marketPriceTax sourcingMarkupPrice energyTaxPrice }
  }
}
"""


class FrankProvider(Provider):
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        await super().fetch(dynamicPrice, now)

        # the new API serves one day per call at 15-minute resolution; fetch today and the next two days
        rows = []
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            for days in range(3):
                body = {"query": _QUERY, "operationName": "MarketPrices", "variables": {"date": (now.date() + timedelta(days=days)).isoformat(), "resolution": "PT15M"}}
                async with session.post(_FE_URL, json=body, timeout=_HTTP_TIMEOUT) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
                if prices := (((payload or {}).get("data") or {}).get("marketPrices") or {}).get("electricityPrices"):
                    rows.extend(prices)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Frank Energie fetch failed: {err}") from err
        if not rows:
            raise ProviderError("Frank Energie returned no prices")

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
        self.extend_tail(sell, tail, base, "sell")
        vat = 1 + self.vat / 100.0
        buy[tail:] = (sell[tail:] + etx + sourcing) * vat
        self.extend_buy(buy, sell, tail, base)

