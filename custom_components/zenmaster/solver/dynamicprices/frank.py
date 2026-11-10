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
# logged in: the prices of the customer's own delivery site, its fields aliased to the public names
_CUSTOMER_QUERY = """
query MarketPrices($date: String!, $siteReference: String!) {
  customerMarketPrices(date: $date, siteReference: $siteReference) {
    electricityPrices { from till marketPrice marketPriceTax sourcingMarkupPrice: consumptionSourcingMarkupPrice energyTaxPrice: energyTax }
  }
}
"""
_LOGIN = "mutation Login($email: String!, $password: String!) { login(email: $email, password: $password) { authToken refreshToken } }"
_RENEW = "mutation RenewToken($authToken: String!, $refreshToken: String!) { renewToken(authToken: $authToken, refreshToken: $refreshToken) { authToken refreshToken } }"
_SITES = "query UserSites { userSites { reference status } }"
_AUTH_ERRORS = ("user-error:auth-not-authorised", "user-error:auth-required")


class FrankProvider(Provider):
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        buy, sell = await super().fetch(dynamicPrice, now)  # raw wholesale forecast in working copies

        # the new API serves one day per call at 15-minute resolution; fetch today and the next two days
        rows = []
        # logged in the customer's own site, else the public prices
        site = dynamicPrice.token.get("site")
        query, key = (_CUSTOMER_QUERY, "customerMarketPrices") if site else (_QUERY, "marketPrices")
        for days in range(3):
            date = (now.date() + timedelta(days=days)).isoformat()
            payload = await self._post(dynamicPrice, query, {"date": date, "siteReference": site} if site else {"date": date, "resolution": "PT15M"})
            if prices := ((payload.get("data") or {}).get(key) or {}).get("electricityPrices"):
                rows.extend(prices)
        if not rows:
            raise ProviderError("Frank Energie returned no prices")

        # real day-ahead aligned by each row's 'from' timestamp (slot 12 = the quarter containing now),
        # mirroring the base fetch; keep the last fixed components for the forecast tail
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

        # remaining forecast slots: the real day's intraday shape, then all-in buy as the real rows: VAT on the market price, the last sourcing and energy tax
        self.extend_tail(sell, tail, base, "sell")
        buy[tail:] = sell[tail:] * (1 + self.vat / 100.0) + sourcing + etx
        self.extend_buy(buy, sell, tail, base)
        return buy, sell

    async def _post(self, dynamicPrice, query: str, variables: dict, retry: bool = True) -> dict:
        # one GraphQL call; logged in it carries the token, an expired token is renewed once
        headers = {"Authorization": f"Bearer {auth}"} if (auth := dynamicPrice.token.get("authToken")) else {}
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            async with session.post(_FE_URL, json={"query": query, "variables": variables}, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                payload = await resp.json() or {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Frank Energie fetch failed: {err}") from err
        if any(e.get("message") in _AUTH_ERRORS for e in payload.get("errors") or []):
            if retry and auth and await self.refresh(dynamicPrice):
                await dynamicPrice._store(dynamicPrice.hass)
                return await self._post(dynamicPrice, query, variables, retry=False)
            raise ProviderError("Frank Energie: login expired, enter the password on the price card again")
        return payload

    async def submit_otp(self, dynamicPrice, password: str) -> bool:
        # the password from the price card logs in once and is never stored; kept are the token and the delivery site
        if not dynamicPrice.email:
            raise ProviderError("Frank Energie: missing 'email' in config")
        dynamicPrice.token = {}
        payload = await self._post(dynamicPrice, _LOGIN, {"email": dynamicPrice.email, "password": password}, retry=False)
        if not (login := (payload.get("data") or {}).get("login")):
            raise ProviderError(f"Frank Energie login failed: {str(payload.get('errors'))[:200]}")
        dynamicPrice.token = {"authToken": login["authToken"], "refreshToken": login["refreshToken"]}
        # the site in delivery, else the first one on the account
        sites = ((await self._post(dynamicPrice, _SITES, {})).get("data") or {}).get("userSites") or []
        if not (site := next((s for s in sites if s.get("status") == "IN_DELIVERY"), sites[0] if sites else None)):
            raise ProviderError("Frank Energie: no delivery site on this account")
        dynamicPrice.token["site"] = site["reference"]
        return True

    async def refresh(self, dynamicPrice) -> bool:
        # trade the refresh token for a new pair; the site stays
        token = dynamicPrice.token
        payload = await self._post(dynamicPrice, _RENEW, {"authToken": token.get("authToken"), "refreshToken": token.get("refreshToken")}, retry=False)
        if not (renewed := (payload.get("data") or {}).get("renewToken")):
            return False
        token.update(authToken=renewed["authToken"], refreshToken=renewed["refreshToken"])
        return True

