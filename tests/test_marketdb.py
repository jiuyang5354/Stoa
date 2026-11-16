"""本地日线库：幂等写入、增量、区间查询、统计、单只失败不影响其它。"""
import json

import pytest

from stock_agent import marketdb

ROWS_A = [
    {"date": "2025-11-10", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100, "turnover": 105.0},
    {"date": "2025-11-11", "open": 1.05, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 120, "turnover": 138.0},
]


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    return tmp_path


def _fetch(rows=None, source="测试源 · 日线", currency="CNY"):
    def fetch(symbol, d0, d1, raw=False):
        assert d0 < d1 and raw is False
        return list(rows if rows is not None else ROWS_A), {"source": source, "currency": currency}
    return fetch


def test_sync_writes_and_is_idempotent():
    first = marketdb.sync(["600519.SS"], days=30, fetcher=_fetch())
    assert first["600519.SS"]["rows"] == 2 and first["600519.SS"]["source"] == "测试源 · 日线"
    marketdb.sync(["600519.SS"], days=30, fetcher=_fetch())  # 再同步一次
    st = marketdb.stats()
    assert st["rows"] == 2 and st["symbols"] == 1
    assert st["first"] == "2025-11-10" and st["last"] == "2025-11-11"


def test_incremental_adds_new_day():
    marketdb.sync(["600519.SS"], days=30, fetcher=_fetch())
    more = ROWS_A + [{"date": "2025-11-12", "open": 1.15, "high": 1.3, "low": 1.1, "close": 1.25,
                      "volume": 130, "turnover": 162.5}]
    marketdb.sync(["600519.SS"], days=30, fetcher=_fetch(more))
    assert marketdb.stats()["rows"] == 3
    assert marketdb.bars("600519.SS")[-1]["date"] == "2025-11-12"


def test_bars_range_limit_and_order():
    marketdb.sync(["600519.SS"], days=30, fetcher=_fetch())
    assert [b["date"] for b in marketdb.bars("600519.SS")] == ["2025-11-10", "2025-11-11"]
    assert [b["date"] for b in marketdb.bars("600519.SS", start="2025-11-11")] == ["2025-11-11"]
    assert [b["date"] for b in marketdb.bars("600519.SS", limit=1)] == ["2025-11-11"]  # 最近一根
    assert marketdb.bars("000001.SZ") == []  # 库里没有就是空，不联网


def test_currency_and_source_recorded():
    marketdb.sync(["0700.HK"], days=30, fetcher=_fetch(source="Yahoo Finance 日线", currency="HKD"))
    row = marketdb.query("SELECT DISTINCT source, currency FROM bars")[0]
    assert row == {"source": "Yahoo Finance 日线", "currency": "HKD"}


def test_one_bad_symbol_does_not_stop_others():
    def fetch(symbol, d0, d1, raw=False):
        if symbol == "BOOM":
            raise RuntimeError("取数失败")
        return ROWS_A, {"source": "s", "currency": "CNY"}
    got = marketdb.sync(["600519.SS", "BOOM"], days=30, fetcher=fetch)
    assert got["600519.SS"]["rows"] == 2 and "取数失败" in got["BOOM"]["error"]
    assert marketdb.stats()["symbols"] == 1


def test_empty_rows_are_not_written():
    marketdb.sync(["600519.SS"], days=30, fetcher=_fetch(rows=[]))
    assert marketdb.stats()["rows"] == 0


def test_cli_stats_and_sql(capsys):
    marketdb.sync(["600519.SS"], days=30, fetcher=_fetch())
    assert marketdb.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"] == 2 and out["db"].endswith("bars.db")
    assert marketdb.main(["--sql", "select count(*) n from bars"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["n"] == 2
    assert marketdb.main(["--sql", "select * from nope"]) == 2
