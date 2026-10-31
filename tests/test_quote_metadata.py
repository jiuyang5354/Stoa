from datetime import datetime

import pandas as pd

from stock_agent import data, serve


def test_quote_metadata_and_cache_age(monkeypatch):
    cfg = {"indices": {"港股": [{"symbol": "^HSI", "name": "恒指"}]},
           "watchlist": [{"symbol": "0700.HK", "name": "腾讯"}, {"symbol": "AAPL", "name": "苹果"}]}
    monkeypatch.setattr(serve, "load_cfg", lambda: cfg)
    monkeypatch.setattr(serve, "trading_now", lambda: False)
    series = pd.Series([100., 101.], index=pd.to_datetime(["2026-10-29", "2026-10-30"]))
    series.attrs["source"] = "Yahoo Finance"
    monkeypatch.setattr(data, "fetch_closes", lambda *a, **k: {s: series for s in ["^HSI", "0700.HK", "AAPL"]})
    monkeypatch.setattr(serve, "_IDX", {"ts": 0, "data": None})
    first = serve.indices()
    hk = next(x for x in first if x["symbol"] == "0700.HK")
    assert hk["source"] == "Yahoo Finance" and hk["currency"] == "HKD" and hk["fallback"]
    assert hk["quote_time"] == "2026-10-30" and hk["quote_precision"] == "date"
    assert not hk["cached"] and hk["retrieved_at"]
    assert next(x for x in first if x["symbol"] == "^HSI")["currency"] == "points"
    assert next(x for x in first if x["symbol"] == "AAPL")["timezone"] == "America/New_York"
    second = serve.indices()
    assert all(x["cached"] for x in second)
    assert not first[0]["cached"]  # cached responses must not mutate earlier responses
    serve._IDX["ts"] = 0
    serve._IDX["cts"] = 0
    monkeypatch.setattr(data, "fetch_closes", lambda *a, **k: {})
    stale = serve.indices()
    assert len(stale) == 3 and all(x["cached"] and x["refresh_failed"] for x in stale)


def test_market_schedule_uses_local_time_and_exposes_uncertainty():
    # DST makes 14:00 UTC 10:00 in New York on this date.
    from datetime import timezone
    result = serve.market_metadata("AAPL", False, datetime(2026, 10, 30, 14, tzinfo=timezone.utc))
    assert result["market_state"] == "schedule_open"
    assert result["calendar_verified"] is False
    lunch = serve.market_metadata("600519.SS", False, datetime(2026, 10, 30, 12, tzinfo=serve.CN_TZ))
    assert lunch["market_state"] == "schedule_closed"
