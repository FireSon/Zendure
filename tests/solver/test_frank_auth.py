"""Frank Energie login: the password once, the delivery site, the customer prices and a renewed token."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.zenmaster.solver import dynamicprice as dp
from custom_components.zenmaster.solver.dynamicprice import DynamicPrice, ProviderError
from custom_components.zenmaster.solver.dynamicprices import frank

NOW = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class _Session:
    """Records every post with its headers and answers from a scripted list."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((json, headers))
        return _Resp(self.script.pop(0))


def _dp():
    o = DynamicPrice()
    o.email = "user@example.com"
    o.provider = "FrankProvider"
    o.price_buy, o.price_sell = SimpleNamespace(data=np.zeros(7 * 96)), SimpleNamespace(data=np.zeros(7 * 96))
    return o


def _patch(monkeypatch, script):
    session = _Session(script)
    monkeypatch.setattr(frank, "async_get_clientsession", lambda *a, **k: session)
    return session


async def _stub_base(self, dynamicPrice, now):
    # the wholesale base the provider overlays: flat, with the NL tariffs, as test_dynamicprice stubs it
    self.import_markup, self.energy_tax, self.vat, self.fixed_cost_per_day, self.zone = 0.0, 0.09, 21.0, 0.0, "NL"
    self.tou = np.zeros(7 * 96)
    self.last_fetch = now
    return np.full(7 * 96, 0.10), np.full(7 * 96, 0.10)


def test_login_keeps_the_token_and_the_site_in_delivery(monkeypatch):
    session = _patch(monkeypatch, [
        {"data": {"login": {"authToken": "A1", "refreshToken": "R1"}}},
        {"data": {"userSites": [{"reference": "OLD", "status": "DELIVERY_ENDED"}, {"reference": "S1", "status": "IN_DELIVERY"}]}},
    ])
    o = _dp()
    assert asyncio.run(frank.FrankProvider().submit_otp(o, "secret")) is True
    assert o.token == {"authToken": "A1", "refreshToken": "R1", "site": "S1"}  # the password itself is not kept
    assert session.calls[0][0]["variables"] == {"email": "user@example.com", "password": "secret"}
    assert session.calls[1][1] == {"Authorization": "Bearer A1"}  # the sites are asked with the new token


def test_the_password_is_never_stored():
    # the password rides along with the save like the Zonneplan code; only the token persists
    from custom_components.zenmaster.config import Config

    o = _dp()
    Config().deserialize_poly([o], "price", {"password": "secret"})
    assert not hasattr(o, "password"), "het wachtwoord mag nooit op het object belanden"
    payload = {}
    Config().serialize(o, "price", payload)
    assert "password" not in payload["price"], "het wachtwoord mag nooit opgeslagen worden"


def test_login_reports_a_wrong_password(monkeypatch):
    _patch(monkeypatch, [{"errors": [{"message": "user-error:password-invalid"}], "data": {"login": None}}])
    o = _dp()
    with pytest.raises(ProviderError, match="login failed"):
        asyncio.run(frank.FrankProvider().submit_otp(o, "wrong"))
    assert o.token == {}


def test_logged_in_fetch_asks_the_customer_prices_of_the_site(monkeypatch):
    monkeypatch.setattr(dp.Provider, "fetch", _stub_base)
    h0 = NOW.replace(minute=0)
    row = {"from": h0.isoformat(), "till": (h0 + timedelta(hours=1)).isoformat(), "marketPrice": 0.20, "marketPriceTax": 0.04, "sourcingMarkupPrice": 0.02, "energyTaxPrice": 0.12}
    session = _patch(monkeypatch, [{"data": {"customerMarketPrices": {"electricityPrices": [row]}}}] * 3)
    o = _dp()
    o.token = {"authToken": "A1", "refreshToken": "R1", "site": "S1"}
    buy, sell = asyncio.run(frank.FrankProvider().fetch(o, NOW))
    assert buy[12] == pytest.approx(0.38) and sell[12] == pytest.approx(0.20)  # parsed like the public rows
    body, headers = session.calls[0]
    assert "customerMarketPrices" in body["query"] and body["variables"]["siteReference"] == "S1"
    assert headers == {"Authorization": "Bearer A1"}


def test_an_expired_token_is_renewed_once(monkeypatch):
    session = _patch(monkeypatch, [
        {"errors": [{"message": "user-error:auth-not-authorised"}]},
        {"data": {"renewToken": {"authToken": "A2", "refreshToken": "R2"}}},
        {"data": {"userSites": []}},
    ])
    o = _dp()
    o.token = {"authToken": "A1", "refreshToken": "R1", "site": "S1"}
    stored = []
    async def store(hass):
        stored.append(dict(o.token))
    o._store = store
    asyncio.run(frank.FrankProvider()._post(o, frank._SITES, {}))
    assert o.token == {"authToken": "A2", "refreshToken": "R2", "site": "S1"}
    assert stored == [o.token]  # the new pair is saved
    assert session.calls[1][0]["variables"] == {"authToken": "A1", "refreshToken": "R1"}
    assert session.calls[2][1] == {"Authorization": "Bearer A2"}  # the call goes again with the new token


def test_a_refused_renewal_asks_for_the_password(monkeypatch):
    _patch(monkeypatch, [
        {"errors": [{"message": "user-error:auth-not-authorised"}]},
        {"errors": [{"message": "user-error:refresh-token-invalid"}], "data": {"renewToken": None}},
    ])
    o = _dp()
    o.token = {"authToken": "A1", "refreshToken": "R1", "site": "S1"}
    with pytest.raises(ProviderError, match="login expired"):
        asyncio.run(frank.FrankProvider()._post(o, frank._SITES, {}))
