from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_URL = "https://api.energy-charts.info/price"
_HTTP_TIMEOUT = ClientTimeout(total=5)


class EnergyChartsProvider(Provider):
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        await super().fetch(dynamicPrice, now)   # raw wholesale forecast in buy + sell

        params = {"bzn": self.zone, "start": now.date().isoformat(), "end": (now.date() + timedelta(days=1)).isoformat()}
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            async with session.get(_URL, params=params, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Energy-Charts fetch failed for {self.zone}: {err}") from err

        buy = dynamicPrice.price_buy.data
        sell = dynamicPrice.price_sell.data
        size = buy.size
        markup, vat, fixed = self.import_markup + self.energy_tax, 1 + self.vat / 100.0, self.fixed_cost_per_day / 96.0
        us = data.get("unix_seconds") or []
        pr = data.get("price") or []
        step = (us[1] - us[0]) // 900 if len(us) > 1 else 4   # quarters per point (4 = hourly, 1 = quarter-hourly)
        base = int(now.timestamp()) // 900 * 900 - 12 * 900  # slot 12 = the quarter containing now
        tail = 12
        for ts, p in zip(us, pr):
            if p is None:
                continue
            w = float(p) / 1000.0   # EUR/MWh -> EUR/kWh
            idx = (int(ts) - base) // 900
            for q in range(step):
                slot = idx + q
                if 0 <= slot < size:
                    buy[slot] = (w * self.import_factor + markup + self.tou[slot]) * vat + fixed
                    sell[slot] = w
                    tail = max(tail, slot + 1)

        # forecast tail beyond the day-ahead window (real day's intraday shape); slots 0..11 untouched
        self.extend_tail(sell, tail, base, "sell")
        buy[tail:] = (sell[tail:] * self.import_factor + markup + self.tou[tail:]) * vat + fixed
        sell[12:] = sell[12:] * self.export_factor + self.export_markup  # the feed-in price; buy above was built on the bare spot
        self.extend_buy(buy, sell, tail, base)
