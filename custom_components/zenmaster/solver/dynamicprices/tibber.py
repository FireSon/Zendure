from __future__ import annotations

import asyncio
from datetime import datetime

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_TIBBER_URL = "https://api.tibber.com/v1-beta/gql"
_HTTP_TIMEOUT = ClientTimeout(total=5)
_QUERY = """
query {
  viewer {
    homes {
      currentSubscription {
        priceInfo {
          today    { startsAt total energy tax level }
          tomorrow { startsAt total energy tax level }
        }
      }
    }
  }
}
"""


class TibberProvider(Provider):
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        await super().fetch(dynamicPrice, now)   # raw wholesale forecast in buy + sell

        headers = {"Authorization": f"Bearer {dynamicPrice.access_token}", "Content-Type": "application/json"}
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            async with session.post(_TIBBER_URL, json={"query": _QUERY}, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Tibber fetch failed: {err}") from err
        try:
            info = payload["data"]["viewer"]["homes"][0]["currentSubscription"]["priceInfo"]
        except (KeyError, IndexError, TypeError) as err:
            raise ProviderError(f"Tibber payload malformed: {err}") from err

        rows = (info.get("today") or []) + (info.get("tomorrow") or [])
        buy = dynamicPrice.price_buy.data
        sell = dynamicPrice.price_sell.data
        size = buy.size
        step = int((datetime.fromisoformat(rows[1]["startsAt"]) - datetime.fromisoformat(rows[0]["startsAt"])).total_seconds()) // 900 if len(rows) > 1 else 4
        base = int(now.timestamp()) // 900 * 900 - 12 * 900  # slot 12 = the quarter containing now
        tail, delta_sum, n = 12, 0.0, 0
        for entry in rows:
            try:
                total, energy = float(entry["total"]), float(entry["energy"])
                start = int(datetime.fromisoformat(entry["startsAt"]).timestamp())
            except (KeyError, TypeError, ValueError) as err:
                raise ProviderError(f"Tibber entry malformed: {err}") from err
            idx = (start - base) // 900
            for q in range(step):
                slot = idx + q
                if 0 <= slot < size:
                    buy[slot], sell[slot] = total, energy
                    delta_sum += total - energy
                    n += 1
                    tail = max(tail, slot + 1)

        # forecast tail: real day's intraday shape, then extend with the average buy-sell delta
        self.extend_tail(sell, tail, base, "sell")
        buy[tail:] = sell[tail:] + (delta_sum / n if n else 0.0)
        self.extend_buy(buy, sell, tail, base)
