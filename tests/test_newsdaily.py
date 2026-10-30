"""Nuntius 每日新闻快照：落盘、去重、point-in-time 读取、出错记录。全部离线。"""
import json
from datetime import datetime

import pandas as pd
import pytest

from stock_agent import newsdaily as nd


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


CFG = {"watchlist": [{"symbol": "600519.SS", "name": "贵州茅台"}], "indices": {"A股": [{"symbol": "000001.SS", "name": "上证指数"}],
                                                                              "美股": [{"symbol": "^GSPC", "name": "标普500"}]}}


def fake(feed):
    calls = []

    def f(symbol, limit=8, keyword=""):
        calls.append((symbol, keyword))
        if symbol in feed and isinstance(feed[symbol], Exception):
            raise feed[symbol]
        return {"symbol": symbol, "news": feed.get(symbol, [])[:limit]}
    f.calls = calls
    return f


N1 = {"time": "2026-10-29 10:00", "title": "茅台发布三季报", "summary": "s", "source": "证券时报", "url": "https://finance.eastmoney.com/a/1.html"}
N2 = {"time": "2026-10-30 09:00", "title": "茅台提价", "summary": "s", "source": "东方财富", "url": "https://finance.eastmoney.com/a/2.html"}


def test_snapshot_writes_and_dedups():
    f = fake({"600519.SS": [N1], "000001.SS": [], "^GSPC": RuntimeError("rss down")})
    r = nd.snapshot(cfg=CFG, fetcher=f, now=datetime(2026, 10, 29, 17, 30, tzinfo=nd.CN_TZ))
    assert r["new"] == 1 and r["symbols"] == ["600519.SS", "000001.SS", "^GSPC"] and "^GSPC" in r["errors"]
    assert ("000001.SS", "上证指数") in f.calls and ("^GSPC", "") in f.calls  # A股指数按名字搜，美股指数走代码
    rows = nd.load("600519.SS")
    assert rows[0]["fetched_at"] == "2026-10-29T17:30:00+08:00" and rows[0]["title"] == "茅台发布三季报" and rows[0]["id"]
    f2 = fake({"600519.SS": [N1, N2]})
    r2 = nd.snapshot(["600519.SS"], cfg=CFG, fetcher=f2, now=datetime(2026, 10, 30, 17, 30, tzinfo=nd.CN_TZ))
    assert r2["new"] == 1 and r2["seen"] == 1 and len(nd.load("600519.SS")) == 2
    snaps = (nd.news_dir() / "snapshots.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(snaps) == 2 and json.loads(snaps[0])["errors"]


def test_asof_uses_fetch_time_strictly_before_day():
    f = fake({"600519.SS": [N1]})
    nd.snapshot(["600519.SS"], fetcher=f, cfg=CFG, now=datetime(2026, 10, 29, 17, 30, tzinfo=nd.CN_TZ))
    nd.snapshot(["600519.SS"], fetcher=fake({"600519.SS": [N2]}), cfg=CFG, now=datetime(2026, 10, 30, 17, 30, tzinfo=nd.CN_TZ))
    assert nd.asof("600519.SS", "2026-10-29") == []  # 当天 17:30 才抓到，决策日当天不可用
    got = nd.asof("600519.SS", pd.Timestamp("2026-10-30"))
    assert [x["title"] for x in got] == ["茅台发布三季报"]  # N2 虽然发布于 10-30 09:00，但 17:30 才抓到 → 不给
    assert len(nd.asof("600519.SS", "2026-10-31")) == 2
    assert nd.asof("600519.SS", "2026-12-31", days=30) == []  # 超出回看窗口


def test_stats_and_times(monkeypatch):
    nd.snapshot(["600519.SS"], fetcher=fake({"600519.SS": [N1, N2]}), cfg=CFG, now=datetime(2026, 10, 29, 17, 30, tzinfo=nd.CN_TZ))
    s = nd.stats()
    assert s["items"] == 2 and s["snapshots"] == 1 and s["per_symbol"]["600519.SS"]["first"] == "2026-10-29"
    monkeypatch.setenv("NEWS_SNAPSHOT_TIMES", "off")
    assert nd.snapshot_times() == []
    monkeypatch.setenv("NEWS_SNAPSHOT_TIMES", "08:00,17:30")
    assert nd.snapshot_times() == ["08:00", "17:30"]


def test_targets_dedup():
    cfg = {"watchlist": [{"symbol": "NVDA"}, {"symbol": "NVDA"}], "indices": {}}
    assert [t["symbol"] for t in nd.targets(cfg)] == ["NVDA"]
