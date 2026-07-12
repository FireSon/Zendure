from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time
from pathlib import Path

import numpy as np
from aiohttp import ClientError, ClientSession, ClientTimeout

from .solver import LpSolver, SolverEntry

_LOGGER = logging.getLogger(__name__)
_EPEX_URL = "https://epexpredictor.batzill.com/prices_short"  # EpexPredictor (b3nn0): indicative 7-day curve
_HTTP_TIMEOUT = ClientTimeout(total=15)


class DynamicPrice(SolverEntry):
    def __init__(self):
        super().__init__()
        self.provider = "FrankProvider"
        self.email = ""
        self.password = ""
        self.access_token = ""
        self.auth_uuid = ""
        self.saldering = False  # net metering: exported kWh settled at the import price -> sell = buy
        self.hass = None  # set by the coordinator; needed for the background task

    def entry_config(self, lp):
        self.price_buy = lp.price_buy = lp.data("price_buy", "euro", 7, source=self.provider)
        self.price_sell = lp.price_sell = lp.data("price_sell", "euro", 7, source=self.provider)
        providers = {c.__name__.removesuffix("Provider").casefold(): c for c in Provider.__subclasses__()}
        name = self.provider.removesuffix("Provider").casefold()
        if name not in providers:
            _LOGGER.warning("Unknown price provider %r, falling back to energycharts (wholesale)", self.provider)
        self._provider = providers.get(name, providers["energycharts"])()
        self._provider.init(self)

    async def entry_prerun(self, lp: LpSolver, now: datetime):
        # cold start, then refresh each new day once the market publish time has passed (to catch tomorrow)
        p = self._provider
        if self.hass is not None:
            if p.last_fetch is None:
                await self._refresh(now)
            elif (p.last_fetch.date() != now.date() and now.time() >= p.publish):
                self.hass.async_create_task(self._refresh(now))
        if self.saldering:  # net metering: exported kWh are settled at the import price
            self.price_sell.data[:] = self.price_buy.data[:]

    async def _refresh(self, now: datetime) -> None:
        # last_fetch marks a FULLY completed refresh; any failure leaves it unset so the next tick retries
        await self._provider.fetch(self, now)
        self._provider.last_fetch = now

class Provider:
    def __init__(self):
        # buy = (wholesale + import_markup + energy_tax) * (1 + vat/100) + fixed_cost_per_day/96
        self.import_markup: float = 0.0
        self.energy_tax: float = 0.0
        self.vat: float = 21.0
        self.fixed_cost_per_day: float = 0.0
        self.export_price: float = 0.07
        self.last_fetch = None  # actual datetime of the last completed refresh
        self.publish = time(13, 0)  # market publish time of the day-ahead prices

    def init(self, dynamicPrice: DynamicPrice):
        pass

    def extend_tail(self, wholesale: np.ndarray, tail: int, base_ts: int) -> None:
        # Give the forecast tail (beyond the real day-ahead) the intraday SHAPE of the real day-ahead,
        # anchored per tail-day to the indicative level. The raw indicative curve has a weak midday dip;
        # persisting the real day's shape restores a realistic cheap-midday / expensive-evening profile
        # so the battery gets a cheap recharge window between peaks. Quarters-of-day with no real sample
        # keep the indicative value. Needs enough real data (>=6h) to infer a shape.
        size = len(wholesale)
        if tail <= 12 or tail >= size:
            return
        q0 = (base_ts // 900) % 96  # quarter-of-day at slot 0
        sums, cnt = np.zeros(96), np.zeros(96)
        for s in range(12, tail):
            q = (q0 + s) % 96
            sums[q] += wholesale[s]
            cnt[q] += 1
        covered = cnt > 0
        if int(covered.sum()) < 24:
            return
        shape = np.ones(96)
        shape[covered] = sums[covered] / cnt[covered]
        mean = shape[covered].mean()
        if mean <= 0:
            return
        shape[covered] /= mean  # covered quarters -> mean-1 factor; uncovered stay at 1 (keep indicative)
        for start in range(tail, size, 96):
            block = wholesale[start : start + 96]
            level = float(block.mean())
            if level <= 0:
                continue
            for i in range(len(block)):
                q = (q0 + start + i) % 96
                if covered[q]:
                    wholesale[start + i] = level * shape[q]

    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        # read the tariff information for this provider
        _TARIFFS_PATH = os.path.join(os.path.dirname(__file__), "dynamicprices", "tariffs.json")
        _TARIFFS = json.loads(await asyncio.get_running_loop().run_in_executor(None, Path(_TARIFFS_PATH).read_text, "utf-8"))

        # look up settings by HA country code + provider name (strip "Provider"), case-insensitive; fall back to wholesale
        country = (dynamicPrice.hass.config.country if dynamicPrice.hass else None) or "NL"
        land = next((v for k, v in _TARIFFS.items() if k.casefold() == country.casefold()), None) or _TARIFFS["NL"]
        name = dynamicPrice.provider.removesuffix("Provider").casefold()
        entry = next((v for k, v in land["providers"].items() if k.casefold() == name), None) or land["providers"]["wholesale"]
        self.energy_tax = land["energy_tax"]
        self.vat = land["vat"]
        self.zone = land["wholesale_zone"]
        self.import_markup = entry.get("import_markup", 0.0)
        self.fixed_cost_per_day = entry.get("fixed_cost_per_day", 0.0)
        self.export_price = entry.get("export_price", self.export_price)
        self.publish = time.fromisoformat(land.get("publish", "13:00"))

        # fetch EpexPredictor's indicative 7-day wholesale curve, from now
        size = dynamicPrice.price_buy.data.size
        params = {"country": country, "surcharge": 0, "taxPercent": 0, "evaluation": "false",
                  "unit": "EUR_PER_KWH", "hourly": "false", "hours": size // 4, "timezone": "UTC"}
        try:
            async with ClientSession() as session:
                async with session.get(_EPEX_URL, params=params, timeout=_HTTP_TIMEOUT) as resp:
                    resp.raise_for_status()
                    d = await resp.json(content_type=None)
        except (ClientError, asyncio.TimeoutError, KeyError) as err:
            raise ProviderError(f"EpexPredictor fetch failed: {err}") from err
        # write the raw EpexPredictor wholesale price into buy and sell; the provider turns buy into an all-in price
        base = int(now.timestamp()) // 900 * 900 - 12 * 900  # slot 12 = current quarter (12 history slots before now)
        for ts, val in zip(d.get("s") or [], d.get("t") or []):
            idx = (int(ts) - base) // 900
            if val is not None and 0 <= idx < size:
                dynamicPrice.price_buy.data[idx] = val
                dynamicPrice.price_sell.data[idx] = val


class ProviderError(Exception):
    pass


# import the provider implementations so they register as Provider subclasses (used in entry_config)
from .dynamicprices import energycharts, frank, tibber, zonneplan, nextenergy  # noqa: E402,F401
