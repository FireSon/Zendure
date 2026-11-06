"""Zonneplan OTP login: the two saves, the token exchange and what happens when a code is refused."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.zenmaster.solver.dynamicprice import DynamicPrice, ProviderError
from custom_components.zenmaster.solver.dynamicprices import zonneplan


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError(f"HTTP {self.status}")


class _Session:
    """Records every post and answers from a scripted list."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        return _Resp(*self.script.pop(0))


def _dp():
    o = DynamicPrice()
    o.email = "user@example.com"
    o.provider = "ZonneplanProvider"
    o.price_buy, o.price_sell = SimpleNamespace(known=None), SimpleNamespace(known=None)  # what entry_config sets up
    return o


def _patch(monkeypatch, script):
    session = _Session(script)
    monkeypatch.setattr(zonneplan, "async_get_clientsession", lambda *a, **k: session)
    return session


def test_request_otp_stores_the_session_and_verifier(monkeypatch):
    # the 403 with otp_required is the success path of the challenge call
    session = _patch(monkeypatch, [(403, {"otp_required": True, "auth_session": "S1"})])
    dp = _dp()
    assert asyncio.run(zonneplan.ZonneplanProvider().request_otp(dp)) is True
    assert dp.auth_uuid == "S1"
    assert len(dp.code_verifier) == 128
    url, body = session.calls[0]
    assert url.endswith("/oauth/authorize-challenge")
    assert body["email"] == "user@example.com" and body["code_challenge_method"] == "S256"
    assert body["response_type"] == "code" and body["code_challenge"]


def test_request_otp_rejects_an_unexpected_answer(monkeypatch):
    _patch(monkeypatch, [(200, {"something": "else"})])
    with pytest.raises(ProviderError, match="unexpected"):
        asyncio.run(zonneplan.ZonneplanProvider().request_otp(_dp()))


def test_submit_otp_exchanges_the_code_for_tokens(monkeypatch):
    session = _patch(monkeypatch, [
        (200, {"authorization_code": "AC"}),
        (200, {"access_token": "AT", "refresh_token": "RT"}),
    ])
    dp = _dp()
    dp.auth_uuid, dp.code_verifier = "S1", "V" * 128
    assert asyncio.run(zonneplan.ZonneplanProvider().submit_otp(dp, "123456")) is True
    assert dp.token["access_token"] == "AT" and dp.token["refresh_token"] == "RT"
    assert dp.auth_uuid == "" and dp.code_verifier == ""  # the attempt is closed
    assert session.calls[0][1] == {"auth_session": "S1", "otp": "123456"}
    assert session.calls[1][1] == {"grant_type": "authorization_code", "code": "AC", "code_verifier": "V" * 128}


def test_submit_otp_reports_a_refused_code(monkeypatch):
    _patch(monkeypatch, [(400, {"error": "invalid_session"})])
    dp = _dp()
    dp.auth_uuid, dp.code_verifier = "S1", "V" * 128
    with pytest.raises(ProviderError, match="rejected"):
        asyncio.run(zonneplan.ZonneplanProvider().submit_otp(dp, "000000"))


def test_submit_otp_without_an_open_attempt(monkeypatch):
    _patch(monkeypatch, [])
    with pytest.raises(ProviderError, match="no open login attempt"):
        asyncio.run(zonneplan.ZonneplanProvider().submit_otp(_dp(), "123456"))


def test_the_token_carries_an_expiry(monkeypatch):
    # the crash that ate a live code: datetime.time shadowed the time module, so stamping expires_at blew up
    _patch(monkeypatch, [
        (200, {"authorization_code": "AC"}),
        (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}),
    ])
    dp = _dp()
    dp.auth_uuid, dp.code_verifier = "S1", "V" * 128
    asyncio.run(zonneplan.ZonneplanProvider().submit_otp(dp, "123456"))
    assert dp.token["expires_at"] > 0, "zonder stempel weet een refresh nooit wanneer hij moet"


def test_refresh_swaps_the_refresh_token(monkeypatch):
    session = _patch(monkeypatch, [(200, {"access_token": "AT2", "refresh_token": "RT2"})])
    dp = _dp()
    dp.token = {"refresh_token": "RT1"}
    assert asyncio.run(zonneplan.ZonneplanProvider().refresh(dp)) is True
    assert dp.token["access_token"] == "AT2" and dp.token["refresh_token"] == "RT2"
    assert session.calls[0][1] == {"grant_type": "refresh_token", "refresh_token": "RT1"}


def test_the_code_is_never_stored_and_the_attempt_is():
    # the one-shot code rides along with the save; the session, verifier and token are what persist
    from custom_components.zenmaster.config import Config

    dp = _dp()
    dp.auth_uuid, dp.code_verifier = "S1", "V"
    Config().deserialize_poly([dp], {"id": "price", "otp": "123456"})
    assert not hasattr(dp, "otp"), "de code mag nooit op het object belanden"
    payload = {}
    Config().serialize(dp, "price", payload)
    assert "otp" not in payload["price"], "de code mag nooit opgeslagen worden"
    assert {f.id for f in DynamicPrice.CONFIG} == {"id", "provider", "saldering", "import_markup", "fixed_cost_per_day", "country", "grid_fee", "offpeak_hours", "offpeak_hours_summer", "export_factor", "export_markup", "email", "access_token", "request_code", "needcode", "otp"}


def test_needcode_drives_the_code_field():
    # the card only shows the code input while a code is actually being waited for
    fields = {f.id: f for f in DynamicPrice.CONFIG}
    assert fields["otp"].visible_when == {"provider": ["ZonneplanProvider"], "needcode": [True]}
    assert fields["needcode"].hidden and fields["needcode"].readonly, "de vlag stuurt alleen, hij wordt niet getoond"
    assert fields["request_code"].selector["button"]["command"] == "price_auth"
    assert not fields["needcode"].transient, "de vlag moet met de kaart meekomen"


def test_saving_the_card_never_asks_for_a_code():
    # the whole point: changing saldering, or any other field, must not send mail
    class Fake:
        last_fetch = None
        known_until = 0

        async def request_otp(self, dp):
            raise AssertionError("opslaan mag nooit een code aanvragen")

        async def submit_otp(self, dp, otp):
            raise AssertionError("zonder code valt er niets in te wisselen")

    dp = _dp()
    dp._provider = Fake()
    asyncio.run(dp.authorise(None, ""))


def test_the_button_asks_and_the_next_save_exchanges():
    # the button opens the attempt and stores it; a restart keeps it; the code then buys the token
    store = {}
    steps = []

    class Fake:
        last_fetch = None
        known_until = 0

        async def request_otp(self, dp):
            steps.append("request")
            dp.auth_uuid, dp.code_verifier = "S1", "V1"
            return True

        async def submit_otp(self, dp, otp):
            steps.append(("submit", otp, dp.auth_uuid))
            dp.token, dp.auth_uuid, dp.code_verifier = {"access_token": "AT"}, "", ""
            return True

        async def fetch(self, dp, now):
            steps.append("fetch")

    def wire(o):
        o._provider = Fake()

        async def save(hass, params):
            store.clear()
            store.update(params)

        async def load(hass):
            return dict(store)

        o.save_params, o.load_params = save, load
        return o

    first = wire(_dp())
    assert asyncio.run(first.request_code(None)) is True
    assert first.needcode and store["auth_session"] == "S1"  # the attempt is on disk, not only in memory

    second = wire(_dp())  # a fresh object, as after a restart
    asyncio.run(second.restore(None))
    assert not second.needcode, "een herstart laat de aanvraag vallen: opnieuw op de knop"
    second.needcode = True  # the button again, on the attempt that is still open server-side
    asyncio.run(second.authorise(None, "123456"))
    assert steps == ["request", ("submit", "123456", "S1"), "fetch"]
    assert store["token"] == {"access_token": "AT"} and not store["auth_session"]
    assert not second.needcode, "met een token is de coderij weer weg"


def test_a_refused_code_keeps_the_field_open():
    class Fake:
        last_fetch = None
        known_until = 0

        async def submit_otp(self, dp, otp):
            raise ProviderError("Zonneplan: code rejected (HTTP 400)")

    dp = _dp()
    dp._provider = Fake()
    dp.auth_uuid, dp.code_verifier, dp.needcode = "S1", "V", True
    asyncio.run(dp.authorise(None, "000000"))
    assert dp.needcode, "na een afgewezen code moet je het opnieuw kunnen proberen"


class _GetSession:
    """Answers the chart GET with one scripted payload and records the call."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        self.headers = headers or {}
        return _Resp(200, self.payload)


def _fetch(monkeypatch, payload):
    from datetime import datetime

    import numpy as np

    session = _GetSession(payload)
    monkeypatch.setattr(zonneplan, "async_get_clientsession", lambda *a, **k: session)

    async def wholesale(self, dp, now):  # the indicative base curve, replaced here so only the chart is under test
        dp.price_buy.data[:] = dp.price_sell.data[:] = 0.20

    monkeypatch.setattr(zonneplan.Provider, "fetch", wholesale)
    dp = _dp()
    dp.token = {"access_token": "AT"}
    dp.price_buy = SimpleNamespace(data=np.zeros(7 * 96))
    dp.price_sell = SimpleNamespace(data=np.zeros(7 * 96))
    asyncio.run(zonneplan.ZonneplanProvider().fetch(dp, datetime.now().astimezone()))
    return dp, session


def test_a_chart_without_prices_leaves_the_series_untouched(monkeypatch):
    # the silent failure we hit in production: no exception, no real prices, buy stays equal to sell
    dp, session = _fetch(monkeypatch, {"data": {"chart": {"series": {}}}})
    assert session.calls, "de chart-endpoint moet aangeroepen zijn"
    assert all(abs(a - b) < 1e-9 for a, b in zip(dp.price_buy.data, dp.price_sell.data))


def test_the_app_chart_lands_as_all_in_and_market_price(monkeypatch):
    # exactly the shape the app returns: everything under "data", amounts as whole numbers of 1e-7 euro
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Amsterdam")
    start = datetime.now(tz).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    prices = [{"start_date": (start + timedelta(minutes=15 * k)).isoformat(),
               "price_tax_included": {"amount": 2562524 + k * 1000},
               "price_tax_excluded": {"amount": 1454043 + k * 1000}} for k in range(96)]
    dp, session = _fetch(monkeypatch, {"data": {"chart": {"series": {"prices": prices}}}})
    assert "consumer-prices/charts/electricity-quarter-hourly" in session.calls[0][0]
    buy, sell = dp.price_buy.data, dp.price_sell.data
    assert 0.25 < buy[12] < 0.27, f"all-in hoort rond 0,256 EUR/kWh te liggen, niet {buy[12]}"
    assert 0.14 < sell[12] < 0.16, f"markt hoort rond 0,145 EUR/kWh te liggen, niet {sell[12]}"
    assert abs((buy[12] - sell[12]) - 0.1108481) < 1e-6, "het verschil is de energiebelasting plus btw"


def test_prices_under_the_old_root_are_not_read(monkeypatch):
    # the bug that cost a day: the app wraps everything in "data", so a payload without it holds no prices
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Amsterdam")
    start = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    prices = [{"start_date": (start + timedelta(minutes=15 * k)).isoformat(),
               "price_tax_included": {"amount": 2562524}, "price_tax_excluded": {"amount": 1454043}} for k in range(8)]
    dp, _ = _fetch(monkeypatch, {"chart": {"series": {"prices": prices}}})
    assert all(abs(a - b) < 1e-9 for a, b in zip(dp.price_buy.data, dp.price_sell.data))


def test_the_chart_call_carries_the_bearer_token(monkeypatch):
    dp, session = _fetch(monkeypatch, {"data": {"chart": {"series": {"prices": []}}}})
    assert session.headers.get("Authorization") == "Bearer AT"


def test_the_token_is_written_to_the_store():
    # the store is the only place the token survives; if this never runs, every restart loses it
    written = []

    class Fake:
        last_fetch = None
        known_until = 0

        async def request_otp(self, dp):
            raise AssertionError("met een code hoort hij niet opnieuw te vragen")

        async def submit_otp(self, dp, otp):
            assert otp == "123456", "de code hoort getrimd binnen te komen"
            dp.token, dp.auth_uuid, dp.code_verifier = {"access_token": "AT", "refresh_token": "RT"}, "", ""
            return True

        async def fetch(self, dp, now):
            pass

    dp = _dp()
    dp._provider = Fake()

    async def save_params(hass, params):
        written.append(params)

    dp.save_params = save_params
    dp.auth_uuid, dp.code_verifier, dp.needcode = "S1", "V", True
    asyncio.run(dp.authorise(None, " 123456 "))
    assert written == [{"token": {"access_token": "AT", "refresh_token": "RT"}, "auth_session": "", "code_verifier": ""}]


def test_restore_reads_the_token_back():
    dp = _dp()

    async def load_params(hass):
        return {"token": {"access_token": "AT"}}

    dp.load_params = load_params
    asyncio.run(dp.restore(None))
    assert dp.token == {"access_token": "AT"}
