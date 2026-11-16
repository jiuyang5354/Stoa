"""price_history：A 股优先同花顺官方日线，其它市场/失败回落 Yahoo。"""
import pytest

from stock_agent import edgar, hithink

BARS = [
    {"date": "2025-11-10", "open": 1420.0, "high": 1425.0, "low": 1410.0, "close": 1420.5, "volume": 3000000, "turnover": 4.2e9},
    {"date": "2025-11-11", "open": 1422.0, "high": 1435.0, "low": 1418.0, "close": 1430.0, "volume": 3142572, "turnover": 4.48e9},
    {"date": "2025-11-12", "open": 1431.0, "high": 1440.0, "low": 1428.0, "close": 1438.0, "volume": 2800000, "turnover": 4.0e9},
]

YAHOO_ROWS = [{"date": "2025-11-11", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "adj_close": 100.5, "volume": 1}]
YAHOO_META = {"symbol": "600519.SS", "name": "Kweichow Moutai", "currency": "CNY", "exchange": "Shanghai",
              "splits": [], "first_trade_date": "2001-08-27"}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("HITHINK_API_KEY", "test-key")


def _capture(monkeypatch, bars=None):
    seen = {}

    def hist(symbol, start, end, adjust="forward"):
        seen.update(symbol=symbol, start=start, end=end, adjust=adjust)
        if isinstance(bars, Exception):
            raise bars
        return bars if bars is not None else BARS

    monkeypatch.setattr(hithink, "historical", hist)
    return seen


def test_a_share_dates_use_official_unadjusted_bars(monkeypatch):
    seen = _capture(monkeypatch)
    out = edgar.price_history("600519.SS", dates=["2025-11-11"])
    assert seen["adjust"] == "none"            # 问具体某天 → 不复权，等于当时成交价
    assert out["source"].startswith("同花顺")
    assert out["dates"][0]["close"] == 1430.0 and out["dates"][0]["trading_day"] is True
    assert out["currency"] == "CNY"


def test_a_share_range_uses_forward_adjustment(monkeypatch):
    seen = _capture(monkeypatch)
    out = edgar.price_history("600519.SS", start="2025-11-10", end="2025-11-12")
    assert seen["adjust"] == "forward"
    assert out["range"]["trading_days"] == 3
    assert out["range"]["change_pct"] == pytest.approx((1438.0 / 1420.5 - 1) * 100, abs=0.01)
    assert out["source"].startswith("同花顺")


def test_non_trading_day_reports_neighbours(monkeypatch):
    _capture(monkeypatch)
    out = edgar.price_history("600519.SS", dates=["2025-11-09"])  # 周日
    d = out["dates"][0]
    assert d["trading_day"] is False
    assert d["previous_trading_day"] is None            # 窗口里没有更早的一根
    assert d["next_trading_day"]["date"] == "2025-11-10"


def test_falls_back_to_yahoo_when_hithink_fails(monkeypatch):
    monkeypatch.setattr(hithink, "historical", lambda *a, **k: (_ for _ in ()).throw(hithink.HithinkError("429", 4001)))
    monkeypatch.setattr(edgar, "_yahoo_daily", lambda symbol, d0, d1: (YAHOO_ROWS, dict(YAHOO_META)))
    out = edgar.price_history("600519.SS", dates=["2025-11-11"])
    assert out["source"].startswith("Yahoo") and out["dates"][0]["close"] == 100.5


def test_hk_and_us_never_use_hithink(monkeypatch):
    calls = []
    monkeypatch.setattr(hithink, "historical", lambda *a, **k: calls.append(a) or [])
    monkeypatch.setattr(edgar, "_yahoo_daily", lambda symbol, d0, d1: (YAHOO_ROWS, dict(YAHOO_META)))

    edgar.price_history("0700.HK", dates=["2025-11-11"])
    edgar.price_history("AAPL", dates=["2025-11-11"])
    assert calls == []  # 港股/美股直接走 Yahoo


def test_empty_bars_fall_back(monkeypatch):
    _capture(monkeypatch, bars=[])
    monkeypatch.setattr(edgar, "_yahoo_daily", lambda symbol, d0, d1: (YAHOO_ROWS, dict(YAHOO_META)))
    out = edgar.price_history("600519.SS", dates=["2025-11-11"])
    assert out["source"].startswith("Yahoo")
