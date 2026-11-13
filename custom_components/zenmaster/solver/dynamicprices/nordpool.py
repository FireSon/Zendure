from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_AREAS = {"DE-LU": "GER"}  # Nord Pool's own name where it differs from the bidding zone


class NordPoolProvider(Provider):
    async def fetch(self, dynamic_price: DynamicPrice, now: datetime):
        from pynordpool import Currency, NordPoolClient, NordPoolError  # loaded only when Nord Pool is the provider

        buy, sell = await super().fetch(dynamic_price, now)  # raw wholesale forecast in working copies

        # today and tomorrow from Nord Pool's public day-ahead data; a day not published yet is skipped by the client
        area = _AREAS.get(self.zone, self.zone)
        try:
            client = NordPoolClient(async_get_clientsession(dynamic_price.hass))  # HA's shared session, never closed here
            days = await client.async_get_delivery_periods([now, now + timedelta(days=1)], Currency.EUR, [area])
        except NordPoolError as err:
            raise ProviderError(f"Nord Pool fetch failed for {area}: {err}") from err

        # every delivery period as (start, quarters, EUR/MWh)
        points = [(int(e.start.timestamp()), int((e.end - e.start).total_seconds()) // 900, e.entry.get(area)) for day in days.entries.values() for e in day.entries]
        return self.fill_spot(buy, sell, now, points)
