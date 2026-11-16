"""hithink 客户端：代码映射、信封、快照/日线/日历解析，以及两个接入点（报价源、交易状态）。

全部离线：`hithink._http` / `requests.get` 被 monkeypatch，不发真实请求。
"""
import datetime as dt

import pytest
import requests

from stock_agent import hithink, marketdata, serve

OK_SNAPSHOT = {
    "timestamp": 1762905000000,  # 2025-11-12 03:50 UTC → 11:50 北京
    "total": 2,
    "item": [
        {"thscode": "600519.SH", "ticker": "600519", "last_price": 1430.0, "prev_price": 1420.5,
         "price_change_ratio_pct": 0.67, "open_price": 1422.0, "high_price": 1435.0,
         "low_price": 1418.0, "volume": 3142572, "turnover": 4480000000.0},
    ],
}

OK_BARS = {
    "timestamp": 1762905000000,
    "item": [
        {"date_ms": 1762818600000, "open_price": 1422.0, "high_price": 1435.0, "low_price": 1418.0,
         "close_price": 1430.0, "volume": 3142572, "turnover": 4480000000.0},
        {"date_ms": 1762732200000, "open_price": None, "high_price": 1420.0, "low_price": 1410.0,
         "close_price": 1420.5, "volume": None, "turnover": None},
    ],
}

OK_CAL = {"timestamp": 1762905000000,
          "item": [{"date_ms": 1762818600000, "date": "20251111"},
                   {"date_ms": 1762732200000, "date": "20251110"},
                   {"date_ms": 1762645800000, "date": "20251107"}]}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("HITHINK_API_KEY", "test-key")
    hithink._CAL.update(at=0.0, days=None)
    yield
    hithink._CAL.update(at=0.0, days=None)


# ---------------------------------------------------------------- 代码映射
@pytest.mark.parametrize("sym,want", [
    ("600519.SS", "600519.SH"), ("600519.SH", "600519.SH"), ("000858.SZ", "000858.SZ"),
    ("430047.BJ", "430047.BJ"), ("600519", "600519.SH"), ("002594", "002594.SZ"),
    ("0700.HK", None), ("AAPL", None), ("^GSPC", None), ("", None),
])
def test_thscode_mapping(sym, want):
    assert hithink.thscode(sym) == want


def test_ready_requires_key(monkeypatch):
    assert hithink.ready() is True
    monkeypatch.delenv("HITHINK_API_KEY", raising=False)
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    assert hithink.ready() is False


# ---------------------------------------------------------------- 信封
def test_envelope_error_carries_code(monkeypatch):
    monkeypatch.setattr(hithink.requests, "get",
                        lambda *a, **k: Resp(200, {"code": 1001, "message": "invalid thscode"}))
    with pytest.raises(hithink.HithinkError) as e:
        hithink.snapshot("600519.SS")
    assert e.value.code == 1001 and "1001" in str(e.value)


def test_rate_limit_maps_to_4001(monkeypatch):
    monkeypatch.setattr(hithink.requests, "get", lambda *a, **k: Resp(429, {}))
    with pytest.raises(hithink.HithinkError) as e:
        hithink._http("/api/a-share/prices/snapshot")
    assert e.value.code == 4001


def test_calendar_swallows_rate_limit_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(hithink.requests, "get", lambda *a, **k: Resp(429, {}))
    assert hithink.trading_days() is None


def test_request_sends_key_header(monkeypatch):
    seen = {}

    def get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return Resp(200, {"code": 0, "data": OK_SNAPSHOT})

    monkeypatch.setattr(hithink.requests, "get", get)
    hithink.snapshot("600519.SS")
    assert seen["headers"]["X-api-key"] == "test-key"
    assert seen["params"]["thscodes"] == "600519.SH"


# ---------------------------------------------------------------- 快照 / 日线
def test_snapshot_fields_and_cn_date(monkeypatch):
    monkeypatch.setattr(hithink, "_http", lambda path, params=None: OK_SNAPSHOT)
    got = hithink.snapshot("600519.SS")["600519.SH"]
    assert got["price"] == 1430.0 and got["prev_close"] == 1420.5
    assert got["date"] == "2025-11-12" and got["currency"] == "CNY"


def test_quote_only_for_a_shares(monkeypatch):
    monkeypatch.setattr(hithink, "_http", lambda path, params=None: OK_SNAPSHOT)
    assert hithink.quote(marketdata.parse("0700.HK")) is None
    assert hithink.quote(marketdata.parse("AAPL")) is None
    q = hithink.quote(marketdata.parse("600519.SS"))
    assert q["price"] == 1430.0 and q["date"] == "2025-11-12"


def test_historical_sorted_and_keeps_nulls(monkeypatch):
    monkeypatch.setattr(hithink, "_http", lambda path, params=None: OK_BARS)
    rows = hithink.historical("600519.SS", "2025-11-01", "2025-11-12")
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    assert rows[0]["open"] is None and rows[0]["volume"] is None  # null 不当 0
    assert rows[-1]["close"] == 1430.0


# ---------------------------------------------------------------- 日历 / 交易状态
def test_calendar_is_cached(monkeypatch):
    calls = {"n": 0}

    def http(path, params=None):
        calls["n"] += 1
        return OK_CAL

    monkeypatch.setattr(hithink, "_http", http)
    assert hithink.trading_days() == {"2025-11-11", "2025-11-10", "2025-11-07"}
    hithink.trading_days()
    assert calls["n"] == 1


def test_session_state_holiday_weekend_and_session(monkeypatch):
    monkeypatch.setattr(hithink, "_http", lambda path, params=None: OK_CAL)
    # 周三 10:00 北京，但日历里没有 11-12 → 法定节假日休市
    st = hithink.session_state("600519.SS", dt.datetime(2025, 11, 12, 10, 0, tzinfo=hithink.CN))
    assert st["trading_day"] is False and st["reason"] == "holiday" and st["calendar_verified"] is True
    # 周日 → weekend
    st = hithink.session_state("600519.SS", dt.datetime(2025, 11, 9, 10, 0, tzinfo=hithink.CN))
    assert st["reason"] == "weekend"
    # 交易日 10:00 → 在连续竞价时段
    st = hithink.session_state("600519.SS", dt.datetime(2025, 11, 11, 10, 0, tzinfo=hithink.CN))
    assert st["in_session"] is True and st["reason"] is None
    # 交易日 12:10 → 午休
    st = hithink.session_state("600519.SS", dt.datetime(2025, 11, 11, 12, 10, tzinfo=hithink.CN))
    assert st["in_session"] is False and st["reason"] == "outside_hours"
    # 港股不在覆盖范围
    assert hithink.session_state("0700.HK") is None


def test_calendar_failure_returns_none_not_exception(monkeypatch):
    def boom(path, params=None):
        raise hithink.HithinkError("down", 500)

    monkeypatch.setattr(hithink, "_http", boom)
    assert hithink.trading_days() is None
    assert hithink.session_state("600519.SS") is None


# ---------------------------------------------------------------- 接入点：报价源
def test_hithink_joins_quote_sources_only_with_key(monkeypatch):
    assert "同花顺" in marketdata._sources()
    assert list(marketdata._sources())[0] == "同花顺"
    monkeypatch.delenv("HITHINK_API_KEY", raising=False)
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    assert "同花顺" not in marketdata._sources()


def test_realtime_reports_hithink_error_without_breaking_others(monkeypatch):
    # 同花顺抛错、其它源正常 → 报价表里同花顺那一条记错误，整体仍然返回
    monkeypatch.setattr(hithink, "quote", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    # realtime 用的是 _sources() 里的函数对象，所以替换 RT_SOURCES 的条目而不是模块属性
    monkeypatch.setitem(marketdata.RT_SOURCES, "新浪财经",
                        (lambda p: {"price": 1.0, "prev_close": 0.9, "date": "2025-11-12"}, lambda p: "x"))
    monkeypatch.setitem(marketdata.RT_SOURCES, "腾讯证券", (lambda p: None, lambda p: "x"))
    monkeypatch.setitem(marketdata.RT_SOURCES, "Yahoo Finance", (lambda p: None, lambda p: "x"))
    out = marketdata.realtime("600519.SS", ["同花顺", "新浪财经"])
    assert out["同花顺"][0] is None and "boom" in (out["同花顺"][2] or "")
    assert out["新浪财经"][0]["price"] == 1.0


# ---------------------------------------------------------------- 接入点：交易状态
def test_market_metadata_uses_verified_calendar_for_a_shares(monkeypatch):
    monkeypatch.setattr(hithink, "session_state", lambda symbol, at=None: {
        "trading_day": False, "in_session": False, "reason": "holiday",
        "calendar_verified": True, "date": "2025-11-10"})
    meta = serve.market_metadata("600519.SS", at=dt.datetime(2025, 11, 10, 10, 0, tzinfo=serve.CN_TZ))
    assert meta["calendar_verified"] is True and meta["closed_reason"] == "holiday"
    assert meta["market_state"] == "schedule_closed"


def test_market_metadata_stays_unverified_without_calendar(monkeypatch):
    monkeypatch.setattr(hithink, "session_state", lambda symbol, at=None: None)
    meta = serve.market_metadata("600519.SS", at=dt.datetime(2025, 11, 10, 10, 0, tzinfo=serve.CN_TZ))
    assert meta["calendar_verified"] is False and "closed_reason" not in meta
    # 港股永远不由 A 股日历背书
    meta_hk = serve.market_metadata("0700.HK", at=dt.datetime(2025, 11, 10, 10, 0, tzinfo=serve.CN_TZ))
    assert meta_hk["calendar_verified"] is False
