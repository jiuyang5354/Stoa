"""更深的数据：多源交叉校验、资金流合并与降级、期权最大痛点、分钟线摘要、报价校验接进 get_quotes。全部离线（数据源用假函数）。"""
import pandas as pd
import pytest

from stock_agent import marketdata as md


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


# ---------------------------------------------------------------- 交叉校验
def test_cross_check_threshold():
    sp, w = md.cross_check({"新浪": 100.0, "腾讯": 100.3}, 0.5, "报价")
    assert sp == 0.3 and w == []
    sp, w = md.cross_check({"新浪": 100.0, "Yahoo": 102.0, "腾讯": 100.1}, 0.5, "报价")
    assert sp > 1.9 and len(w) == 1 and "新浪" in w[0] and "Yahoo" in w[0] and "102" in w[0]
    sp, w = md.cross_check({"东方财富": 3.2, "新浪财经": -1.0}, 35, "主力净流入")
    assert sp >= 100.0 and w  # 方向相反至少算 100%
    sp, w = md.cross_check({"Yahoo": 9.3}, 10, "EPS")
    assert sp is None and "只有" in w[0]


def test_quote_check_basis_by_date():
    rt = {"新浪财经": ({"price": 105.0, "prev_close": 100.0, "date": "2026-10-29", "time": "10:00"}, "u1", None),
          "腾讯证券": ({"price": 100.2, "prev_close": 99.0, "date": "2026-10-28", "time": "16:00"}, "u2", None),
          "Yahoo Finance": (None, "u3", "连接失败")}
    r = md.quote_check("600519.SS", 100.0, "2026-10-28", primary="东方财富", rt=rt)
    assert r["basis"] == {"新浪财经": "昨收", "腾讯证券": "现价"}  # 新浪已经是第二天：比它的昨收
    assert r["values"]["新浪财经"] == 100.0 and r["warnings"] == []
    assert any(s["name"] == "Yahoo Finance" and not s["ok"] for s in r["sources"])
    rt["腾讯证券"] = ({"price": 103.0, "prev_close": 99.0, "date": "2026-10-28"}, "u2", None)
    r = md.quote_check("600519.SS", 100.0, "2026-10-28", primary="东方财富", rt=rt)
    assert r["warnings"] and "腾讯证券" in r["warnings"][0]


def test_get_quotes_attaches_crosscheck(monkeypatch):
    from stock_agent import agent, data
    idx = pd.bdate_range("2026-06-01", periods=80)
    ser = pd.Series([50 + i * .1 for i in range(80)], index=idx)
    ser.attrs["source"] = "Yahoo Finance"
    monkeypatch.setattr(data, "fetch_closes", lambda syms, period="1y": {s: ser for s in syms})
    last = float(ser.iloc[-1])
    monkeypatch.setattr(md, "realtime", lambda s, names=None: {"新浪财经": ({"price": last * 0.95, "prev_close": None, "date": str(idx[-1].date())}, "u", None),
                                                               "腾讯证券": ({"price": last, "prev_close": None, "date": str(idx[-1].date())}, "u", None)})
    from stock_agent.memory import home
    (home() / "config.yaml").write_text("alerts: {}\nwatchlist: []\n", encoding="utf-8")
    q = agent.quotes(["1810.HK"], check=True)["1810.HK"]
    assert q["crosscheck"]["values"]["新浪财经"] == pytest.approx(last * .95)
    assert q["warnings"] and "新浪财经" in q["warnings"][0]
    monkeypatch.setenv("QUOTE_CROSSCHECK", "off")
    assert "warnings" not in agent.quotes(["1810.HK"], check=True)["1810.HK"]


# ---------------------------------------------------------------- 资金流
EM = [{"date": "2026-10-27", "main_net": 2.0, "super_net": 1.0, "main_pct": 5.0}, {"date": "2026-10-28", "main_net": -1.0, "super_net": -0.4, "main_pct": -2.0}]
SINA = [{"date": "2026-10-27", "main_net": 2.1, "net": 2.5, "main_pct": 5.2}, {"date": "2026-10-28", "main_net": 1.5, "net": 1.0, "main_pct": 3.0}]


def test_fund_flow_merges_and_crosschecks():
    r = md.fund_flow("600519.SS", 5, fetchers={"东方财富": lambda: EM, "新浪财经": lambda: SINA, "东方财富·当日分时": lambda: []})
    assert r["source"] == "东方财富" and len(r["rows"]) == 2 and r["main_net_5d"] == 1.0 and r["super_net_5d"] == 0.6
    assert r["crosscheck"][0]["diff_pct"] < 10
    assert any("2026-10-28" in w and "差异" in w for w in r["warnings"])  # 28 日两源方向相反


def test_fund_flow_falls_back_to_sina_and_today():
    today = [{"date": "2026-10-29", "time": "15:00", "main_net": 0.7, "super_net": 0.5, "large_net": 0.2, "intraday": True}]
    r = md.fund_flow("600519.SS", 5, fetchers={"东方财富": lambda: (_ for _ in ()).throw(ConnectionError("x")), "新浪财经": lambda: SINA, "东方财富·当日分时": lambda: today})
    assert r["source"] == "新浪财经" and r["rows"][-1]["date"] == "2026-10-29" and r["rows"][-1]["intraday"]
    assert any("新浪" in w for w in r["warnings"])
    assert [s["ok"] for s in r["sources"]] == [False, True, True]


def test_fund_flow_only_a_shares():
    assert "只支持 A股" in md.fund_flow("NVDA")["error"]
    assert "只支持 A股" in md.fund_flow("0700.HK")["error"]


# ---------------------------------------------------------------- 期权
def test_max_pain_and_pcr():
    chain = [{"strike": 90, "call_oi": 100, "put_oi": 500}, {"strike": 100, "call_oi": 300, "put_oi": 300}, {"strike": 110, "call_oi": 600, "put_oi": 50}]
    # 到期价 90：call 赔 0、put 赔 300*10+50*20=4000；100：call 100*10=1000、put 50*10=500 → 1500；110：call 100*20+300*10=5000、put 0 → 5000
    assert md.max_pain(chain) == 100
    st = md.chain_stats([{"strike": 100, "iv": .5, "volume": 10, "oi": 300}, {"strike": 110, "iv": .45, "volume": 30, "oi": 600}],
                        [{"strike": 100, "iv": .55, "volume": 20, "oi": 300}, {"strike": 90, "iv": 9.0, "volume": 5, "oi": 500}], 101)
    assert st["pcr_oi"] == round(800 / 900, 2) and st["pcr_volume"] == round(25 / 40, 2)
    assert st["atm_iv_pct"] == 50.0  # 9.0（900%）是坏数据，被剔除


def test_options_yahoo_with_nasdaq_crosscheck():
    calls = [{"strike": 100, "iv": .5, "volume": 10, "oi": 300}, {"strike": 110, "iv": .45, "volume": 30, "oi": 600}]
    puts = [{"strike": 100, "iv": .55, "volume": 20, "oi": 300}, {"strike": 90, "iv": .6, "volume": 5, "oi": 500}]
    y = lambda: ({"2026-11-20": {"calls": calls, "puts": puts}}, 101.0, "2026-11-20")
    nq_calls = [dict(c, iv=None) for c in calls]  # Nasdaq 没有隐含波动率
    nq_puts = [dict(p, oi=p["oi"] * 3, iv=None) for p in puts]
    n = lambda sym, exp=None: ({"2026-11-20": {"calls": nq_calls, "puts": nq_puts}}, 101.0)
    r = md.options("NVDA", yahoo=y, nasdaq=n)
    assert r["source"].startswith("Yahoo") and r["expiries"][0]["nasdaq"]["pcr_oi"] > r["expiries"][0]["pcr_oi"]
    assert any("Put/Call" in w and "差异" in w for w in r["warnings"])
    r2 = md.options("NVDA", yahoo=lambda: (_ for _ in ()).throw(RuntimeError("429")), nasdaq=n)
    assert r2["source"].startswith("Nasdaq") and r2["expiries"][0]["atm_iv_pct"] is None
    assert "只支持美股" in md.options("0700.HK")["error"]


# ---------------------------------------------------------------- 分钟线
def test_summarize_bars():
    bars = [{"t": "2026-10-29 09:30", "o": 10, "h": 10.2, "l": 9.9, "c": 10.1, "v": 100},
            {"t": "2026-10-29 09:35", "o": 10.1, "h": 10.5, "l": 10.0, "c": 10.4, "v": 300},
            {"t": "2026-10-29 09:40", "o": 10.4, "h": 10.4, "l": 9.5, "c": 9.6, "v": 200}]
    d = md.summarize_bars(bars, prev_close=10.0)[0]
    assert d["high"] == 10.5 and d["high_time"] == "09:35" and d["low"] == 9.5 and d["low_time"] == "09:40"
    assert d["chg_pct"] == -4.0 and d["open"] == 10 and d["volume"] == 600
    assert 9.8 < d["vwap"] < 10.3 and d["points"][-1] == ["09:40", 9.6]


def test_parse_symbols():
    assert md.parse("600519")["em"] == "1.600519" and md.parse("000858.SZ")["sina"] == "sz000858"
    assert md.parse("0700.HK")["tencent"] == "hk00700" and md.parse("700.HK")["yahoo"] == "0700.HK"
    assert md.parse("BRK.B")["yahoo"] == "BRK-B" and md.parse("NVDA")["sina"] == "gb_nvda"


def test_consensus_target_mismatch_warns(monkeypatch):
    monkeypatch.setattr(md, "_yahoo_consensus", lambda s: {"target": {"mean": 3000.0}})
    monkeypatch.setattr(md, "_em_consensus", lambda p: {"ratings": {"买入": 3}, "orgs": 3, "eps": [{"year": 2026, "kind": "预测", "eps": 60.0}],
                                                        "target": {"low": 1400.0, "high": 2000.0}})
    monkeypatch.setattr(md, "_em_reports", lambda p: [{"date": "2026-09-01", "org": "A", "rating": "买入", "eps_this_year": 80.0, "eps_next_year": 90.0, "target": None}])
    monkeypatch.setattr(md, "realtime", lambda s, names=None: {"Yahoo Finance": ({"price": 1500.0}, "u", None)})
    r = md.consensus("600519.SS")
    assert r["source"].startswith("东方财富") and r["upside_pct"] == round((1700 / 1500 - 1) * 100, 1)
    assert any("目标价两源不一致" in w for w in r["warnings"])
    if md.now_cn().year == 2026:
        assert any("EPS" in w for w in r["warnings"])
