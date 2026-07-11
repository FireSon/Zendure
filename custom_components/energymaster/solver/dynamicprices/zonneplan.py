from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import ClientSession, ClientTimeout

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_LOGGER = logging.getLogger(__name__)
_ZP_BASE = "https://app-api.zonneplan.nl"
_HTTP_TIMEOUT = ClientTimeout(total=5)


class ZonneplanProvider(Provider):
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        await super().fetch(dynamicPrice, now)   # raw wholesale forecast in buy + sell
        buy = dynamicPrice.price_buy.data
        sell = dynamicPrice.price_sell.data
        size = buy.size
        base = int(now.timestamp()) // 900 * 900 - 12 * 900  # slot 12 = the quarter containing now
        tail, delta_sum, n = 12, 0.0, 0
        for day in (now.date(), now.date() + timedelta(days=1)):
            rows = await self._day(dynamicPrice, day)
            if not rows:
                break
            day_start = int(datetime.combine(day, time(0), ZoneInfo("Europe/Amsterdam")).timestamp())  # rows are hourly from local midnight
            for h, row in enumerate(rows):
                try:
                    b = float(row["buy_price"])
                    s = float(row.get("sell_price") or row["buy_price"])
                except (KeyError, TypeError, ValueError) as err:
                    raise ProviderError(f"Zonneplan row malformed: {err}") from err
                idx = (day_start + h * 3600 - base) // 900
                for q in range(4):
                    slot = idx + q
                    if 0 <= slot < size:
                        buy[slot], sell[slot] = b, s
                        delta_sum += b - s
                        n += 1
                        tail = max(tail, slot + 1)

        # forecast tail: real day's intraday shape, then extend with the average buy-sell delta
        self.extend_tail(sell, tail, base)
        buy[tail:] = sell[tail:] + (delta_sum / n if n else 0.0)

    async def _day(self, dynamicPrice, target_date):
        if not dynamicPrice.access_token:
            raise ProviderError("Zonneplan: not authorized; complete OAuth via setup")
        headers = {"Authorization": f"Bearer {dynamicPrice.access_token}"}
        try:
            async with ClientSession() as session:
                async with session.get(f"{_ZP_BASE}/v1/forecast", params={"date": target_date.isoformat()}, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                    if resp.status in (401, 403):
                        raise ProviderError("Zonneplan auth expired, re-run OAuth")
                    resp.raise_for_status()
                    payload = await resp.json()
        except ProviderError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Zonneplan fetch failed: {err}") from err
        return payload.get("hourly")

    async def request_auth(self, dynamicPrice: DynamicPrice) -> str:
        if not dynamicPrice.email:
            raise ProviderError("Zonneplan: missing 'email' in config")
        try:
            async with ClientSession() as session:
                async with session.post(f"{_ZP_BASE}/auth/email", json={"email": dynamicPrice.email}, timeout=_HTTP_TIMEOUT) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Zonneplan request_auth failed: {err}") from err
        try:
            dynamicPrice.auth_uuid = str(payload["uuid"])
        except (KeyError, TypeError) as err:
            raise ProviderError(f"Zonneplan request_auth payload malformed: {err}") from err
        return dynamicPrice.auth_uuid

    async def complete_auth(self, dynamicPrice: DynamicPrice, token: str) -> bool:
        if not dynamicPrice.auth_uuid:
            return False
        try:
            async with ClientSession() as session:
                async with session.post(f"{_ZP_BASE}/auth/email/{dynamicPrice.auth_uuid}/authorise", json={"token": token}, timeout=_HTTP_TIMEOUT) as resp:
                    if resp.status >= 400:
                        return False
                    payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        access_token = payload.get("access_token") if isinstance(payload, dict) else None
        if not access_token:
            return False
        dynamicPrice.access_token = str(access_token)
        return True
