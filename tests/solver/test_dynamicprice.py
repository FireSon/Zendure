import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np
import pytest

from custom_components.zenmaster.solver import dynamicprice as dp
import custom_components.zenmaster.solver.dynamicprice as dynamicprice
from custom_components.zenmaster.solver.dynamicprices import (
    energycharts,
    frank,
    nextenergy,
    tibber,
    zonneplan,
)

Provider = dp.Provider
ProviderError = dp.ProviderError

SIZE = 7 * 96
RAW = 0.10   # constant raw wholesale (EUR/kWh) the stubbed base writes into the whole curve
NOW = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)


class _SD:
    def __init__(self):
        self.data = np.zeros(SIZE)


def make_dp(provider):
    o = type("DP", (), {})()
    o.price_buy = _SD()
    o.price_sell = _SD()
    o.access_token = "tok"
    o.provider = provider
    o.country = "NL"
    o.hass = None
    o.import_markup = None
    o.fixed_cost_per_day = None
    o.grid_fee = None
    o.offpeak_hours = ""
    o.offpeak_hours_summer = ""
    o.export_factor = None
    o.export_markup = None
    return o


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientError(f"HTTP {self.status}")

    async def json(self, content_type=None):
        return self._payload


class _Session:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, *a, **k):
        return _Resp(self._payload, self._status)

    def post(self, *a, **k):
        return _Resp(self._payload, self._status)


def fake_session(payload, status=200):
    return lambda *a, **k: _Session(payload, status)


async def _stub_base(self, dynamicPrice, now):
    dynamicPrice.price_buy.data[:] = RAW
    dynamicPrice.price_sell.data[:] = RAW
    self.import_markup = 0.0
    self.energy_tax = 0.09
    self.vat = 21.0
    self.fixed_cost_per_day = 0.0
    self.zone = "NL"
    self.tou = np.zeros(dynamicPrice.price_buy.data.size)  # no time-of-use grid fee in NL
    self.last_fetch = now


def stub_base(monkeypatch):
    monkeypatch.setattr(Provider, "fetch", _stub_base)


def test_base_fills_raw_and_sets_overlay(monkeypatch):
    base_ts = int(NOW.timestamp()) // 900 * 900
    payload = {"s": [base_ts + 900 * i for i in range(4)], "t": [0.05, 0.06, 0.07, 0.08]}
    monkeypatch.setattr(dp, "async_get_clientsession", fake_session(payload))
    o = make_dp("FrankProvider")
    p = Provider()
    asyncio.run(p.fetch(o, NOW))
    assert o.price_buy.data[12] == pytest.approx(0.05)   # raw at slot 12 (now), no overlay
    assert o.price_sell.data[12] == pytest.approx(0.05)
    assert o.price_buy.data[15] == pytest.approx(0.08)
    assert p.energy_tax == pytest.approx(0.09161)   # from tariffs.json NL
    assert p.vat == pytest.approx(21.0)
    assert p.zone == "NL"
    assert p.last_fetch is None   # set by DynamicPrice._refresh after a FULLY completed refresh, not by fetch


def test_offset_override_wins(monkeypatch):
    # a spot provider reads its offset from tariffs.json, but a value set on the price card overrides it
    base_ts = int(NOW.timestamp()) // 900 * 900
    payload = {"s": [base_ts + 900 * i for i in range(4)], "t": [0.05, 0.06, 0.07, 0.08]}
    monkeypatch.setattr(dp, "async_get_clientsession", fake_session(payload))
    o = make_dp("anwb")        # anwb table default: import_markup 0.0149, fixed_cost_per_day 0.28
    o.import_markup = 0.055    # user override from the card
    p = Provider()
    asyncio.run(p.fetch(o, NOW))
    assert p.import_markup == pytest.approx(0.055)       # override wins
    assert p.fixed_cost_per_day == pytest.approx(0.28)   # not overridden -> anwb table default


def test_frank(monkeypatch):
    stub_base(monkeypatch)
    h0 = NOW.astimezone(ZoneInfo("Europe/Amsterdam")).replace(minute=0, second=0, microsecond=0)  # hour of now -> slot 12
    rows = [
        {"from": h0.isoformat(), "till": (h0 + timedelta(hours=1)).isoformat(),
         "marketPrice": 0.20, "marketPriceTax": 0.04, "sourcingMarkupPrice": 0.02, "energyTaxPrice": 0.12},
        {"from": (h0 + timedelta(hours=1)).isoformat(), "till": (h0 + timedelta(hours=2)).isoformat(),
         "marketPrice": 0.30, "marketPriceTax": 0.06, "sourcingMarkupPrice": 0.02, "energyTaxPrice": 0.12},
    ]
    monkeypatch.setattr(frank, "async_get_clientsession", fake_session({"data": {"marketPrices": {"electricityPrices": rows}}}))
    o = make_dp("FrankProvider")
    asyncio.run(frank.FrankProvider().fetch(o, NOW))
    buy, sell = o.price_buy.data, o.price_sell.data
    assert buy[12] == pytest.approx(0.38)   # 0.20 + 0.04 + 0.02 + 0.12
    assert buy[16] == pytest.approx(0.50)   # 0.30 + 0.06 + 0.02 + 0.12
    assert sell[12] == pytest.approx(0.20) and sell[16] == pytest.approx(0.30)
    assert buy[20] == pytest.approx((RAW + 0.12 + 0.02) * 1.21)   # tail: last sourcing + etx, VAT%
    assert buy[0] == pytest.approx(RAW) and buy[11] == pytest.approx(RAW)   # first 12 untouched


def test_energycharts(monkeypatch):
    stub_base(monkeypatch)
    ts0 = int(NOW.timestamp())   # NOW is exactly on the hour -> first row covers slot 12
    payload = {"unix_seconds": [ts0, ts0 + 3600], "price": [100.0, 200.0]}   # hourly, EUR/MWh
    monkeypatch.setattr(energycharts, "async_get_clientsession", fake_session(payload))
    o = make_dp("EnergyChartsProvider")
    asyncio.run(energycharts.EnergyChartsProvider().fetch(o, NOW))
    buy, sell = o.price_buy.data, o.price_sell.data
    assert buy[12] == pytest.approx((0.10 + 0.09) * 1.21)   # step 4, w = 0.10, markup 0.09, VAT
    assert buy[16] == pytest.approx((0.20 + 0.09) * 1.21)
    assert sell[12] == pytest.approx(0.10) and sell[16] == pytest.approx(0.20)
    assert buy[20] == pytest.approx((RAW + 0.09) * 1.21)   # tail overlay
    assert buy[0] == pytest.approx(RAW) and buy[11] == pytest.approx(RAW)


def test_tibber(monkeypatch):
    stub_base(monkeypatch)
    h0 = NOW.astimezone(ZoneInfo("Europe/Amsterdam"))   # NOW is exactly on the hour -> first row covers slot 12
    today = [
        {"startsAt": h0.isoformat(), "total": 0.30, "energy": 0.18, "tax": 0.12, "level": "NORMAL"},
        {"startsAt": (h0 + timedelta(hours=1)).isoformat(), "total": 0.40, "energy": 0.25, "tax": 0.15, "level": "NORMAL"},
    ]
    payload = {"data": {"viewer": {"homes": [{"currentSubscription": {"priceInfo": {"today": today, "tomorrow": []}}}]}}}
    monkeypatch.setattr(tibber, "async_get_clientsession", fake_session(payload))
    o = make_dp("TibberProvider")
    asyncio.run(tibber.TibberProvider().fetch(o, NOW))
    buy, sell = o.price_buy.data, o.price_sell.data
    assert buy[12] == pytest.approx(0.30) and sell[12] == pytest.approx(0.18)
    assert buy[16] == pytest.approx(0.40) and sell[16] == pytest.approx(0.25)
    delta = ((0.30 - 0.18) * 4 + (0.40 - 0.25) * 4) / 8
    assert buy[20] == pytest.approx(RAW + delta)   # tail = sell + mean(buy - sell)
    assert buy[0] == pytest.approx(RAW) and buy[11] == pytest.approx(RAW)


def test_zonneplan(monkeypatch):
    # rows are hourly from local midnight; hour h encodes its price as (h+1)/100 so alignment is visible
    stub_base(monkeypatch)
    # the app's chart: quarter-hourly rows from local midnight, all-in and market price per row
    local_midnight = datetime.combine(NOW.astimezone(ZoneInfo("Europe/Amsterdam")).date(), time(0), ZoneInfo("Europe/Amsterdam"))
    rows = [{"start_date": (local_midnight + timedelta(hours=h)).isoformat(),
             "price_tax_included": {"amount": (h + 1) / 100.0 * 1e7},  # the app sends whole numbers of 1e-7 euro
             "price_tax_excluded": {"amount": 0.05 * 1e7}} for h in range(24)]

    async def fake_chart(self, dynamicPrice, chart):
        return rows if chart == "electricity-quarter-hourly" else []

    monkeypatch.setattr(zonneplan.ZonneplanProvider, "_chart", fake_chart)
    o = make_dp("ZonneplanProvider")
    asyncio.run(zonneplan.ZonneplanProvider().fetch(o, NOW))
    buy, sell = o.price_buy.data, o.price_sell.data
    now_hour = NOW.astimezone(ZoneInfo("Europe/Amsterdam")).hour
    assert buy[12] == pytest.approx((now_hour + 1) / 100.0) and sell[12] == pytest.approx(0.05)   # slot 12 = the hour containing now
    assert buy[0] == pytest.approx((now_hour - 2) / 100.0)   # history slots get the real past prices (3h back)
    placed = range(now_hour - 3, 24)   # base = now - 3h, so those hours land in [0, size)
    delta = sum((h + 1) / 100.0 - 0.05 for h in placed) / len(placed)
    tail = (23 - (now_hour - 3)) * 4 + 1   # first slot after the last placed row (one quarter each)
    assert sell[tail] == pytest.approx(sell[tail - 1], abs=0.01)  # the junction fades out of the last real wholesale
    assert buy[tail + dynamicprice._JUNCTION_SLOTS] == pytest.approx(RAW + delta)  # extrapolated level past the fade


def test_nextenergy(monkeypatch):
    stub_base(monkeypatch)
    o = make_dp("NextEnergyProvider")
    asyncio.run(nextenergy.NextEnergyProvider().fetch(o, NOW))
    buy, sell = o.price_buy.data, o.price_sell.data
    assert buy[0] == pytest.approx(RAW) and buy[11] == pytest.approx(RAW)   # no day-ahead, first 12 untouched
    assert buy[12] == pytest.approx((RAW + 0.09) * 1.21)
    assert buy[-1] == pytest.approx((RAW + 0.09) * 1.21)
    assert sell[12] == pytest.approx(RAW)   # sell untouched


def test_frank_malformed_raises(monkeypatch):
    stub_base(monkeypatch)
    monkeypatch.setattr(frank, "async_get_clientsession", fake_session({"data": {}}))
    o = make_dp("FrankProvider")
    with pytest.raises(ProviderError):
        asyncio.run(frank.FrankProvider().fetch(o, NOW))


def test_frank_aligns_rows_to_wall_clock_slot(monkeypatch):
    # Frank returns a full day from midnight; the price at slot 12 must be the row covering 'now', not midnight.
    # marketPrice encodes the local hour (h -> h/100) so we can read off which hour landed on slot 12.
    stub_base(monkeypatch)
    tz = ZoneInfo("Europe/Amsterdam")
    day = NOW.astimezone(tz).date()
    rows = []
    for h in range(24):
        frm = datetime(day.year, day.month, day.day, h, tzinfo=tz)
        rows.append({"from": frm.isoformat(), "till": (frm + timedelta(hours=1)).isoformat(),
                     "marketPrice": h / 100.0, "marketPriceTax": 0.0, "sourcingMarkupPrice": 0.0, "energyTaxPrice": 0.0})
    monkeypatch.setattr(frank, "async_get_clientsession", fake_session({"data": {"marketPrices": {"electricityPrices": rows}}}))
    o = make_dp("FrankProvider")
    asyncio.run(frank.FrankProvider().fetch(o, NOW))
    now_hour = NOW.astimezone(tz).hour  # slot 12 = the quarter containing 'now'
    assert o.price_sell.data[12] == pytest.approx(now_hour / 100.0)
