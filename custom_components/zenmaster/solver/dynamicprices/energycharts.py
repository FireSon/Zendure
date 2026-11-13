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
    async def fetch(self, dynamic_price: DynamicPrice, now: datetime):
        buy, sell = await super().fetch(dynamic_price, now)  # raw wholesale forecast in working copies

        params = {"bzn": self.zone, "start": now.date().isoformat(), "end": (now.date() + timedelta(days=1)).isoformat()}
        try:
            session = async_get_clientsession(dynamic_price.hass)  # HA's shared session, never closed here
            async with session.get(_URL, params=params, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Energy-Charts fetch failed for {self.zone}: {err}") from err

        # every Energy-Charts point as (start, quarters, EUR/MWh)
        us, pr = data.get("unix_seconds") or [], data.get("price") or []
        step = (us[1] - us[0]) // 900 if len(us) > 1 else 4  # quarters per point (4 = hourly, 1 = quarter-hourly)
        return self.fill_spot(buy, sell, now, [(ts, step, p) for ts, p in zip(us, pr)])
