from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from homeassistant.components import persistent_notification

import numpy as np
from aiohttp import ClientError, ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .solver import LpSolver, SolverEntry
from ..config import ConfigField
from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_EPEX_URL = "https://epexpredictor.batzill.com/prices_short"  # EpexPredictor (b3nn0): indicative 7-day curve
_HTTP_TIMEOUT = ClientTimeout(total=15)
_FIT_WINDOWS = (10, 14, 21)  # candidate rolling fit windows in days; the best recent scorer wins
_FIT_SCORE_DAYS = 3  # most recent days held back to pick the window
_FIT_MIN_HOURS = 168  # below this much price history the fitted shape is not used
_JUNCTION_SLOTS = 8  # cross-fade out of the last real price over these slots


def _price_design(hours, weekend, sol, wind):
    cols = [np.ones(len(hours))]
    for n in (1, 2, 3):
        cols += [np.sin(2 * np.pi * n * hours / 24), np.cos(2 * np.pi * n * hours / 24)]
    cols += [weekend, sol / 1000.0, wind / 10.0]
    return np.vstack(cols).T


def _offpeak_spans(text: str) -> list[tuple[int, int]] | None:
    # off-peak ranges as a French bill writes them ("22h00-6h00", "23h30 à 7h30", "2h-7h ; 14h-17h"): every two times are one range, in minutes
    times = [int(h) * 60 + int(m or 0) for h, m in re.findall(r"(\d{1,2})(?:\s*[:hH]\s*(\d{2})?)?", text)]
    return list(zip(times[::2], times[1::2])) if times and len(times) % 2 == 0 and max(times) < 1440 else None


# the providers whose price is wholesale spot plus a tariff offset; the manual offset overrides only apply to these
_SPOT_PROVIDERS = ["EnergyChartsProvider", "NextEnergyProvider", "wholesale", "anwb", "vandebron", "eneco", "enbw", "ebem", "octaplus", "eneco_be", "sobry", "frank_fr", "awattar", "smartenergy"]


class DynamicPrice(SolverEntry):
    CONFIG = [
        ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True),
        ConfigField("provider", {"select": {"options": [
            {"value": "none", "label": "Geen"},
            {"value": "FrankProvider", "label": "Frank Energie"},
            {"value": "TibberProvider", "label": "Tibber"},
            {"value": "ZonneplanProvider", "label": "Zonneplan"},
            {"value": "EnergyChartsProvider", "label": "Energy-Charts (spot)"},
            {"value": "NextEnergyProvider", "label": "NextEnergy"},
            {"value": "wholesale", "label": "Groothandel (spot, kaal)"},
            {"value": "anwb", "label": "ANWB Energie"},
            {"value": "vandebron", "label": "Vandebron"},
            {"value": "eneco", "label": "Eneco"},
            {"value": "enbw", "label": "EnBW (DE)"},
            {"value": "ebem", "label": "Ebem (BE)"},
            {"value": "octaplus", "label": "Octa+ (BE)"},
            {"value": "eneco_be", "label": "Eneco (BE)"},
            {"value": "sobry", "label": "Sobry (FR)"},
            {"value": "frank_fr", "label": "Frank Énergie (FR)"},
            {"value": "awattar", "label": "aWATTar (AT)"},
            {"value": "smartenergy", "label": "smartENERGY (AT)"},
        ]}}, cast=str),
        ConfigField("saldering", {"boolean": {}}, cast=bool),
        ConfigField("import_markup", {"number": {"step": 0.001, "unit_of_measurement": "EUR/kWh"}}, cast=lambda v: float(v) if v not in (None, "") else None, required=False, visible_when={"provider": _SPOT_PROVIDERS}),
        ConfigField("fixed_cost_per_day", {"number": {"step": 0.01, "unit_of_measurement": "EUR/dag"}}, cast=lambda v: float(v) if v not in (None, "") else None, required=False, visible_when={"provider": _SPOT_PROVIDERS}),
        ConfigField("country", {"text": {}}, cast=str, required=False, readonly=True, hidden=True),
        ConfigField("grid_fee", {"number": {"step": 0.001, "unit_of_measurement": "EUR/kWh"}}, cast=lambda v: float(v) if v not in (None, "") else None, required=False, visible_when={"country": ["BE", "DE", "AT"]}),
        ConfigField("offpeak_hours", {"text": {}}, cast=str, required=False, visible_when={"country": ["FR"]}),
        ConfigField("offpeak_hours_summer", {"text": {}}, cast=str, required=False, visible_when={"country": ["FR"]}),
        ConfigField("export_factor", {"number": {"step": 0.001}}, cast=lambda v: float(v) if v not in (None, "") else None, required=False, visible_when={"country": ["DE", "FR", "AT"]}),
        ConfigField("export_markup", {"number": {"step": 0.001, "unit_of_measurement": "EUR/kWh"}}, cast=lambda v: float(v) if v not in (None, "") else None, required=False, visible_when={"country": ["DE", "FR", "AT"]}),
        ConfigField("email", {"text": {}}, cast=str, required=False, visible_when={"provider": ["ZonneplanProvider"]}),
        ConfigField("access_token", {"text": {"type": "password"}}, cast=str, required=False, visible_when={"provider": ["TibberProvider"]}),
        ConfigField("request_code", {"button": {"label": "Vraag code aan", "command": "price_auth", "icon": "mail"}}, cast=None, required=False, readonly=True, visible_when={"provider": ["ZonneplanProvider"]}),
        ConfigField("needcode", {"boolean": {}}, cast=bool, required=False, readonly=True, hidden=True),
        ConfigField("otp", {"text": {}}, cast=str, required=False, transient=True, visible_when={"provider": ["ZonneplanProvider"], "needcode": [True]}),
    ]

    def __init__(self):
        super().__init__()
        self.id = "price"
        self.provider = "none"  # no dynamic price: no solver, the fleet runs on its own control
        self.email = ""
        self.access_token = ""
        # the login attempt lives in memory only, like the official integration does: a restart in the
        # middle of it simply starts over. The token itself is kept in the entry store, out of the form.
        self.auth_uuid = ""
        self.code_verifier = ""
        self.token: dict = {}
        self.needcode = False  # a code is being waited for; the card shows the otp field while this is set
        self.saldering = False  # net metering: exported kWh settled at the import price -> sell = buy
        self.import_markup = None  # offset overrides for spot providers; None = use the tariffs.json default
        self.fixed_cost_per_day = None
        self.grid_fee = None  # card overrides; None = the supplier's value, then the country's
        self.offpeak_hours = ""  # FR: the off-peak hours Enedis set for this address, "22:00-06:00[,...]"; empty = the country default
        self.offpeak_hours_summer = ""  # FR: the summer (April-October) off-peak hours when they differ; empty = the same as winter
        self.export_factor = None
        self.export_markup = None
        self.country = "NL"  # set by the coordinator from the HA config; drives which fields the card shows
        self.price_entities: dict[str, str] = {}  # our own published price sensors, per series
        self.on_save = None  # set by the coordinator: persists the config after a new token
        self.lat = 0.0  # location for the shape model, taken from the solver blackboard
        self.lon = 0.0
        self.hass = None  # set by the coordinator; needed for the background task

    def entry_config(self, lp):
        self.price_buy = lp.price_buy = lp.data("price_buy", "euro", 7, source=self.provider)
        self.price_sell = lp.price_sell = lp.data("price_sell", "euro", 7, source=self.provider)
        providers = {c.__name__.removesuffix("Provider").casefold(): c for c in Provider.__subclasses__()}
        name = self.provider.removesuffix("Provider").casefold()
        if name not in providers and self.provider != "none":
            _LOGGER.warning("Unknown price provider %r, falling back to energycharts (wholesale)", self.provider)
        self._provider = providers.get(name, providers["energycharts"])()
        self._provider.init(self)

    async def entry_prerun(self, lp: LpSolver, now: datetime):
        # cold start, then refresh each new day once the market publish time has passed (to catch tomorrow)
        self.lat = lp.globals.get("latitude", 0.0)  # Forecast publishes these; needed before the refresh
        self.lon = lp.globals.get("longitude", 0.0)
        device = lp.globals.get("price_device")  # the device that publishes our price sensors
        if device is not None:
            self.price_entities = {k: e for k in ("buy", "sell") if (e := getattr(device.entities.get(f"price_{k}"), "entity_id", None))}
        p = self._provider
        if self.hass is not None:
            if p.last_fetch is None:
                await self._refresh(now)
            elif p.last_fetch < datetime.combine(now.date(), p.publish, now.tzinfo) <= now:
                self.hass.async_create_task(self._refresh(now))
        lp.globals["price_valid"] = self._provider.last_fetch is not None
        if self.saldering:  # net metering: exported kWh are settled at the import price
            self.price_sell.data[:] = self.price_buy.data[:]

    async def authorise(self, hass, otp: str) -> None:
        # A code typed into the price card, submitted with that save. It is exchanged straight away and
        # never stored; what is stored is the token. Asking for a code is the button, not this.
        provider = self._provider
        if not otp or not hasattr(provider, "submit_otp"):
            return
        try:
            await provider.submit_otp(self, otp.strip())
        except ProviderError as err:
            _LOGGER.warning("price authorisation: %s", err)
            persistent_notification.async_create(hass, f"Code afgewezen: {err}", "ZenMaster", f"{DOMAIN}_auth")
            return
        self.needcode = False
        await self._store(hass)
        _LOGGER.info("price authorisation: token received")
        persistent_notification.async_create(hass, "Zonneplan is gekoppeld.", "ZenMaster", f"{DOMAIN}_auth")
        await self._refresh(datetime.now().astimezone())  # pull the prices now, not at the next tick

    async def request_code(self, hass) -> bool:
        # The "vraag code aan" button on the price card: mails a fresh code and opens a new attempt.
        # Nothing else sends mail, so saving another field on that card never triggers one.
        provider = self._provider
        if not hasattr(provider, "request_otp"):
            return False
        self.auth_uuid = self.code_verifier = ""
        try:
            await provider.request_otp(self)
        except ProviderError as err:
            _LOGGER.warning("price authorisation: %s", err)
            await self._store(hass)
            persistent_notification.async_create(hass, f"Code aanvragen mislukt: {err}", "ZenMaster", f"{DOMAIN}_auth")
            return False
        self.needcode = True
        await self._store(hass)
        persistent_notification.async_create(hass, "Zonneplan heeft een code gemaild. Vul die in en sla op.", "ZenMaster", f"{DOMAIN}_auth")
        return True

    async def _store(self, hass) -> None:
        await self.save_params(hass, {"token": self.token, "auth_session": self.auth_uuid, "code_verifier": self.code_verifier})

    async def restore(self, hass) -> None:
        # token and open attempt both come back, so a restart between the two saves is harmless
        data = await self.load_params(hass)
        self.token = data.get("token") or {}
        self.auth_uuid = data.get("auth_session") or ""
        self.code_verifier = data.get("code_verifier") or ""
        self.needcode = False  # a restart drops the attempt: press the button again

    async def _refresh(self, now: datetime) -> None:
        # last_fetch marks a FULLY completed refresh; any failure leaves it unset so the next tick retries
        await self._provider.fetch(self, now)
        self.price_buy.known = self.price_sell.known = self._provider.known_until  # the outlook dashes the prices past this point
        self._provider.last_fetch = now

class Provider:
    def __init__(self):
        # buy = (spot * import_factor + import_markup + energy_tax + tou) * (1 + vat/100) + fixed_cost_per_day/96, energy_tax incl. the grid fee
        # sell = spot * export_factor + export_markup, unless saldering settles export at the buy price
        self.import_factor: float = 1.0
        self.import_markup: float = 0.0
        self.energy_tax: float = 0.0
        self.vat: float = 21.0
        self.fixed_cost_per_day: float = 0.0
        self.export_factor: float = 1.0
        self.export_markup: float = 0.0
        self.tou = np.zeros(0)  # time-of-use grid fee per slot (FR TURPE), zero elsewhere
        self.last_fetch = None  # actual datetime of the last completed refresh
        self.known_until = 0  # epoch where the real prices end; 0 = none are real, the whole curve is indicative (NextEnergy)
        self.publish = time(13, 0)  # market publish time of the day-ahead prices
        self._coef: dict[str, np.ndarray] = {}  # fitted shape model per series, empty while there is too little history
        self._weather = {}  # hour timestamp -> (irradiance, wind), covers history and the tail
        self._tz = None  # local zone of the fit, so the tail uses the same hour-of-day

    def init(self, dynamicPrice: DynamicPrice):
        pass

    def extend_tail(self, wholesale: np.ndarray, tail: int, base_ts: int, key: str = "sell") -> None:
        # Rebuild the forecast tail as (indicative level per day) x (intraday shape). The shape comes
        # from the fitted weather+calendar model when there is enough history, otherwise from the real
        # day-ahead's own per-quarter profile. Keeping the level per day preserves the indicative
        # forecast's day-to-day contrast, which is what the battery plans its arbitrage on.
        size = len(wholesale)
        if tail <= 12 or tail >= size:
            return
        q0 = (base_ts // 900) % 96  # quarter-of-day at slot 0
        n = size - tail
        shape, mask = None, np.ones(n, dtype=bool)

        coef = self._coef.get(key)
        if coef is not None and self._weather and self._tz is not None:
            hours, weekend, sol, wind = np.empty(n), np.empty(n), np.empty(n), np.empty(n)
            for i in range(n):
                ts = base_ts + (tail + i) * 900
                local = datetime.fromtimestamp(ts, self._tz)
                hours[i] = local.hour + local.minute / 60.0
                weekend[i] = 1.0 if local.weekday() >= 5 else 0.0
                sol[i], wind[i] = self._weather.get(ts // 3600 * 3600, (0.0, 0.0))
            shape = _price_design(hours, weekend, sol, wind) @ coef

        if shape is None:
            sums, cnt = np.zeros(96), np.zeros(96)
            for s in range(12, tail):
                q = (q0 + s) % 96
                sums[q] += wholesale[s]
                cnt[q] += 1
            covered = cnt > 0
            if int(covered.sum()) < 24:  # needs a full day of real samples to infer a profile
                return
            profile = np.ones(96)
            profile[covered] = sums[covered] / cnt[covered]
            mean = profile[covered].mean()
            if mean <= 0:
                return
            profile[covered] /= mean
            quarters = np.array([(q0 + tail + i) % 96 for i in range(n)])
            shape = profile[quarters]
            mask = covered[quarters]  # a quarter-of-day with no real sample keeps its indicative value

        # per tail day: hold the indicative level, impose the shape. The level comes from the slots the
        # indicative curve actually filled, so a block it did not reach cannot drag the level down.
        for start in range(tail, size, 96):
            block = wholesale[start : start + 96]
            lo = start - tail
            take = mask[lo : lo + len(block)]
            usable = block[block > 0]
            segment = shape[lo : lo + len(block)]
            if not len(usable) or not take.any():
                continue
            span = float(segment[take].mean())
            if span <= 0:
                continue
            block[take] = float(usable.mean()) * segment[take] / span

        # cross-fade out of the last real price: the junction is the one place a step is visible
        last = float(wholesale[tail - 1])
        for i in range(min(_JUNCTION_SLOTS, n)):
            w = (i + 1) / (_JUNCTION_SLOTS + 1)
            wholesale[tail + i] = last * (1 - w) + float(wholesale[tail + i]) * w

    async def fit_shape_model(self, dynamicPrice: DynamicPrice, now: datetime) -> None:
        # Fit every published price series on its own recorded history: price ~ hour-of-day + weekend +
        # solar + wind. Of the candidate windows the one with the lowest error on the most recent days
        # wins, so the length follows the season. Net metering, a market-linked export tariff and a
        # fixed one all follow from the recorded data, so there is no branch per provider here.
        self._coef, self._weather, self._tz = {}, {}, now.tzinfo
        if dynamicPrice.hass is None or now.tzinfo is None:
            return
        ids = dynamicPrice.price_entities
        if not ids:
            return
        # the hourly price history of every series, long enough for the longest window plus the score days
        span = max(_FIT_WINDOWS) + _FIT_SCORE_DAYS
        if not (history := await self.price_history(dynamicPrice, now, span, ids)):
            _LOGGER.info("price shape model: not enough history yet, keeping the day-shape method")
            return

        params = {"latitude": dynamicPrice.lat, "longitude": dynamicPrice.lon, "timezone": "UTC",
                  "hourly": "direct_normal_irradiance,diffuse_radiation,wind_speed_10m",
                  "past_days": span + 1, "forecast_days": 8}
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
            async with session.get(_OPEN_METEO_URL, params=params, timeout=ClientTimeout(total=40)) as resp:
                resp.raise_for_status()
                hourly = (await resp.json())["hourly"]
        except (ClientError, asyncio.TimeoutError, KeyError) as err:
            _LOGGER.warning("price shape model, meteo fetch: %s", err)
            return
        for i, stamp in enumerate(hourly["time"]):
            ts = int(datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp())
            dni = hourly["direct_normal_irradiance"][i] or 0.0
            dhi = hourly["diffuse_radiation"][i] or 0.0
            self._weather[ts] = (dni + dhi, hourly["wind_speed_10m"][i] or 0.0)
        self.fit_windows(history, now)

    async def price_history(self, dynamicPrice: DynamicPrice, now: datetime, span: int, ids: dict) -> dict:
        # per series its hourly means over the span, keyed on the hour; a series with too few hours is left out
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        stats = await get_instance(dynamicPrice.hass).async_add_executor_job(
            statistics_during_period, dynamicPrice.hass, now - timedelta(days=span), now, set(ids.values()), "hour", None, {"mean"}
        )
        history = {}
        for key, entity in ids.items():
            hours = {}
            for row in stats.get(entity, []):
                mean, st = row.get("mean"), row.get("start")
                if mean is None or st is None:
                    continue
                ts = int(st if isinstance(st, (int, float)) else st.timestamp())
                hours[ts // 3600 * 3600] = float(mean)
            if len(hours) >= _FIT_MIN_HOURS:
                history[key] = hours
        return history

    def fit_windows(self, history: dict, now: datetime) -> None:
        # per series: score every candidate window on the most recent days, then refit the winner up to now
        score_from = int(now.timestamp()) - _FIT_SCORE_DAYS * 86400
        for key, price in history.items():
            stamps = sorted(ts for ts in price if ts in self._weather)
            if len(stamps) < _FIT_MIN_HOURS:
                continue
            y = np.array([price[t] for t in stamps])
            local = [datetime.fromtimestamp(t, now.tzinfo) for t in stamps]
            design = _price_design(
                np.array([d.hour for d in local], dtype=float),
                np.array([1.0 if d.weekday() >= 5 else 0.0 for d in local]),
                np.array([self._weather[t][0] for t in stamps]),
                np.array([self._weather[t][1] for t in stamps]),
            )
            test = np.array([t >= score_from for t in stamps])
            if int(test.sum()) < 24 or int((~test).sum()) < 48:
                continue
            best, chosen = None, 0
            for window in _FIT_WINDOWS:
                train = np.array([score_from - window * 86400 <= t < score_from for t in stamps])
                if int(train.sum()) < window * 18:
                    continue
                coef, *_ = np.linalg.lstsq(design[train], y[train], rcond=None)
                error = float(np.abs(y[test] - design[test] @ coef).mean())
                if best is None or error < best:
                    best, chosen = error, window
            if best is None:
                continue
            # refit the winning window on everything up to now, so the newest days are in the model too
            train = np.array([int(now.timestamp()) - chosen * 86400 <= t for t in stamps])
            coef, *_ = np.linalg.lstsq(design[train], y[train], rcond=None)
            self._coef[key] = coef
            _LOGGER.info("price shape model %s: window %d days, MAE %.4f on the last %d days, %d fit hours",
                         key, chosen, best, _FIT_SCORE_DAYS, int(train.sum()))

    def extend_buy(self, buy: np.ndarray, sell: np.ndarray, tail: int, base_ts: int) -> None:
        self.known_until = base_ts + tail * 900  # the real prices end here, past this the shape model fills in
        # the buy tail already carries the level the provider derived; give it its own fitted shape,
        # then make sure the two series cannot cross, which would read as free money to the solver
        self.extend_tail(buy, tail, base_ts, "buy")
        np.minimum(sell[tail:], buy[tail:], out=sell[tail:])

    async def fetch(self, dynamicPrice: DynamicPrice, now: datetime):
        # read the tariff information for this provider
        _TARIFFS_PATH = os.path.join(os.path.dirname(__file__), "dynamicprices", "tariffs.json")
        _TARIFFS = json.loads(await asyncio.get_running_loop().run_in_executor(None, Path(_TARIFFS_PATH).read_text, "utf-8"))

        # look up settings by HA country code + provider name (strip "Provider"), case-insensitive; fall back to wholesale
        country = dynamicPrice.country
        land = next((v for k, v in _TARIFFS.items() if k.casefold() == country.casefold()), None) or _TARIFFS["NL"]
        name = dynamicPrice.provider.removesuffix("Provider").casefold()
        entry = next((v for k, v in land["providers"].items() if k.casefold() == name), None) or land["providers"]["wholesale"]
        # a value on the card wins over the supplier's, the supplier's over the country's
        pick = lambda key, default=0.0: v if (v := getattr(dynamicPrice, key, None)) is not None else entry.get(key, land.get(key, default))
        self.energy_tax = land["energy_tax"] + pick("grid_fee")
        self.vat = land["vat"]
        self.zone = land["wholesale_zone"]
        self.import_factor = pick("import_factor", 1.0)
        self.import_markup = pick("import_markup")
        self.fixed_cost_per_day = pick("fixed_cost_per_day")
        self.export_factor = pick("export_factor", 1.0)
        self.export_markup = pick("export_markup")
        self.publish = time.fromisoformat(land.get("publish", "13:00"))

        await self.fit_shape_model(dynamicPrice, now)

        # fetch EpexPredictor's indicative 7-day wholesale curve, from now
        size = dynamicPrice.price_buy.data.size
        params = {"country": country, "surcharge": 0, "taxPercent": 0, "evaluation": "false",
                  "unit": "EUR_PER_KWH", "hourly": "false", "hours": size // 4, "timezone": "UTC"}
        try:
            session = async_get_clientsession(dynamicPrice.hass)  # HA's shared session, never closed here
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

        # a time-of-use grid fee (FR TURPE): off-peak in the address's hours, else peak, winter or summer by month; zero elsewhere
        self.tou = np.zeros(size)
        if tou := land.get("grid_tou"):
            # the address's off-peak hours for winter and summer; unreadable input falls back to the country default with a warning
            spans = []
            for text in (dynamicPrice.offpeak_hours, dynamicPrice.offpeak_hours_summer or dynamicPrice.offpeak_hours):
                if (found := _offpeak_spans(text or tou["offpeak"])) is None:
                    _LOGGER.warning("Off-peak hours %r are not readable, using %s", text, tou["offpeak"])
                spans.append(found or _offpeak_spans(tou["offpeak"]))
            for i in range(size):
                local = datetime.fromtimestamp(base + i * 900, now.tzinfo)
                minute, season = local.hour * 60 + local.minute, 0 if local.month in tou["winter"] else 1
                offpeak = any(a <= minute < b if a < b else minute >= a or minute < b for a, b in spans[season])
                self.tou[i] = tou["offpeak_fee" if offpeak else "peak_fee"][season]


class ProviderError(Exception):
    pass


# import the provider implementations so they register as Provider subclasses (used in entry_config)
from .dynamicprices import energycharts, frank, tibber, zonneplan, nextenergy  # noqa: E402,F401
