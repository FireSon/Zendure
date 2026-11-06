from __future__ import annotations

import asyncio
import secrets
import hashlib
import base64
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_LOGGER = logging.getLogger(__name__)
_ZP_BASE = "https://app-api.zonneplan.nl"
_ZP_CHALLENGE = f"{_ZP_BASE}/oauth/authorize-challenge"
_ZP_TOKEN = f"{_ZP_BASE}/oauth/token"  # noqa: S105
_ZP_HEADERS = {"content-type": "application/json;charset=utf-8", "x-app-version": "5.10.1", "x-app-environment": "production"}
_HTTP_TIMEOUT = ClientTimeout(total=5)
_AMOUNT_SCALE = 1e7  # the app sends prices as whole numbers: 2562524 is 0.2562524 EUR/kWh


def _with_expiry(tokens: dict) -> dict:
    """Keep the token as one dict and stamp expires_at, so a refresh knows when it is due."""
    out = dict(tokens)
    if "expires_at" not in out and out.get("expires_in"):
        try:
            out["expires_at"] = time.time() + int(out["expires_in"])
        except (TypeError, ValueError):
            pass
    return out


class ZonneplanProvider(Provider):
    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        await super().fetch(dynamicPrice, now)   # raw wholesale forecast in buy + sell
        buy = dynamicPrice.price_buy.data
        sell = dynamicPrice.price_sell.data
        size = buy.size
        base = int(now.timestamp()) // 900 * 900 - 12 * 900  # slot 12 = the quarter containing now
        tail, delta_sum, n = 12, 0.0, 0
        # the app serves both resolutions on the same chart endpoint; prefer the finer one
        for chart, span in (("electricity-quarter-hourly", 1), ("electricity-hourly", 4)):
            prices = await self._chart(dynamicPrice, chart)
            if not prices:
                continue
            for row in prices:
                try:
                    stamp = datetime.fromisoformat(row["start_date"])
                    all_in = float(row["price_tax_included"]["amount"]) / _AMOUNT_SCALE
                    market = float(row.get("price_tax_excluded", {}).get("amount", all_in * _AMOUNT_SCALE)) / _AMOUNT_SCALE
                except (KeyError, TypeError, ValueError) as err:
                    raise ProviderError(f"Zonneplan row malformed: {err}") from err
                idx = (int(stamp.timestamp()) - base) // 900
                for q in range(span):
                    slot = idx + q
                    if 0 <= slot < size:
                        buy[slot], sell[slot] = all_in, market
                        delta_sum += all_in - market
                        n += 1
                        tail = max(tail, slot + 1)
            break

        # forecast tail: real day's intraday shape, then extend with the average buy-sell delta
        self.extend_tail(sell, tail, base, "sell")
        buy[tail:] = sell[tail:] + (delta_sum / n if n else 0.0)
        self.extend_buy(buy, sell, tail, base)

    async def _chart(self, dynamicPrice, chart: str, retry: bool = True):
        """The app's consumer-prices chart: chart.series.prices, each with start_date and amounts."""
        if not (access := dynamicPrice.token.get("access_token")):
            raise ProviderError("Zonneplan: not authorized; save the price card to receive a code")
        headers = {**_ZP_HEADERS, "Authorization": f"Bearer {access}"}
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            async with session.get(f"{_ZP_BASE}/api/consumer-prices/charts/{chart}", headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                if resp.status in (401, 403):
                    # an expired access token: trade the refresh token once, keep the new token and ask again
                    if retry and await self.refresh(dynamicPrice):
                        await dynamicPrice._store(dynamicPrice.hass)
                        return await self._chart(dynamicPrice, chart, retry=False)
                    raise ProviderError("Zonneplan auth expired, save the price card for a new code")
                if resp.status >= 400:
                    raise ProviderError(f"Zonneplan {chart}: HTTP {resp.status}")
                payload = await resp.json()
        except ProviderError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Zonneplan fetch failed: {err}") from err
        prices = ((((payload or {}).get("data") or {}).get("chart") or {}).get("series") or {}).get("prices") or []
        if not prices:
            _LOGGER.info("Zonneplan %s: no prices in the response: %s", chart, str(payload)[:200])
        return prices

    async def request_otp(self, dynamicPrice: DynamicPrice) -> bool:
        """Step 1: ask Zonneplan to mail a one-time code. A 403 with otp_required is the success path."""
        if not dynamicPrice.email:
            raise ProviderError("Zonneplan: missing 'email' in config")
        verifier = secrets.token_urlsafe(96)[:128]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        body = {"response_type": "code", "email": dynamicPrice.email, "code_challenge": challenge,
                "code_challenge_method": "S256", "source_name": "ZenMaster"}
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            async with session.post(_ZP_CHALLENGE, json=body, headers=_ZP_HEADERS, timeout=_HTTP_TIMEOUT) as resp:
                if resp.status not in (200, 403):
                    raise ProviderError(f"Zonneplan authorize-challenge: HTTP {resp.status}")
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Zonneplan request_otp failed: {err}") from err
        if not payload.get("otp_required") or "auth_session" not in payload:
            raise ProviderError(f"Zonneplan authorize-challenge unexpected response: {str(payload)[:200]}")
        dynamicPrice.auth_uuid = str(payload["auth_session"])  # the session the code belongs to
        dynamicPrice.code_verifier = verifier
        return True

    async def submit_otp(self, dynamicPrice: DynamicPrice, otp: str) -> bool:
        """Step 2 and 3: exchange the mailed code for an authorization_code, then for the tokens."""
        if not dynamicPrice.auth_uuid or not dynamicPrice.code_verifier:
            raise ProviderError("Zonneplan: no open login attempt; save the mail address again first")
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            async with session.post(_ZP_CHALLENGE, json={"auth_session": dynamicPrice.auth_uuid, "otp": otp},
                                    headers=_ZP_HEADERS, timeout=_HTTP_TIMEOUT) as resp:
                if resp.status >= 400:
                    raise ProviderError(f"Zonneplan: code rejected (HTTP {resp.status}), it may be wrong or expired")
                payload = await resp.json()
            code = payload.get("authorization_code")
            if not code:
                raise ProviderError(f"Zonneplan: no authorization_code in the response: {str(payload)[:200]}")
            grant = {"grant_type": "authorization_code", "code": code, "code_verifier": dynamicPrice.code_verifier}
            async with session.post(_ZP_TOKEN, json=grant, headers=_ZP_HEADERS, timeout=_HTTP_TIMEOUT) as resp:
                if resp.status >= 400:
                    raise ProviderError(f"Zonneplan token exchange: HTTP {resp.status}")
                tokens = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Zonneplan submit_otp failed: {err}") from err
        if not tokens.get("access_token"):
            raise ProviderError(f"Zonneplan token response without access_token: {str(tokens)[:200]}")
        dynamicPrice.token = _with_expiry(tokens)
        dynamicPrice.auth_uuid = ""
        dynamicPrice.code_verifier = ""
        return True

    async def refresh(self, dynamicPrice: DynamicPrice) -> bool:
        """Trade the refresh token for a fresh access token; called when the API answers 401."""
        if not (refresh := dynamicPrice.token.get("refresh_token")):
            return False
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            grant = {"grant_type": "refresh_token", "refresh_token": refresh}
            async with session.post(_ZP_TOKEN, json=grant, headers=_ZP_HEADERS, timeout=_HTTP_TIMEOUT) as resp:
                if resp.status >= 400:
                    return False
                tokens = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        if not tokens.get("access_token"):
            return False
        dynamicPrice.token = _with_expiry(tokens)
        return True
