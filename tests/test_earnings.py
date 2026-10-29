"""v0.17：财报日解析、跟踪列表、财报前提醒、事件研报生成（fixture，离线）。"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from stock_agent import earnings as E


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_FALLBACK", "EARNINGS", "EARNINGS_REMIND_DAYS"):
        monkeypatch.delenv(k, raising=False)


def day(n):
    return (datetime.now(E.CN) + timedelta(days=n)).strftime("%Y-%m-%d")


def mem():
    from stock_agent.memory import Memory
    return Memory()


# ---------------------------------------------------------------- 财报日解析
def test_calendar_a_parses_eastmoney(monkeypatch):
    rows = [{"SECURITY_NAME_ABBR": "贵州茅台", "REPORT_TYPE_NAME": "2026年 三季报", "REPORT_DATE": "2026-09-30 00:00:00", "FIRST_APPOINT_DATE": "2026-10-31 00:00:00",
             "FIRST_CHANGE_DATE": "2026-10-28 00:00:00", "SECOND_CHANGE_DATE": None, "THIRD_CHANGE_DATE": None, "ACTUAL_PUBLISH_DATE": None, "IS_PUBLISH": "0",
             "APPOINT_PUBLISH_DATE": "2026-10-28 00:00:00"},
            {"SECURITY_NAME_ABBR": "贵州茅台", "REPORT_TYPE_NAME": "2026年 半年报", "REPORT_DATE": "2026-06-30 00:00:00", "FIRST_APPOINT_DATE": "2026-08-15 00:00:00",
             "ACTUAL_PUBLISH_DATE": "2026-08-14 00:00:00", "IS_PUBLISH": "1"}]

    class R:
        def json(self):
            return {"result": {"data": rows}}
    seen = {}

    def fake(url, **kw):
        seen.update(kw.get("params") or {})
        return R()
    monkeypatch.setattr(E, "_req", fake)
    c = E.calendar_a("600519.SS")
    assert 'SECURITY_CODE="600519"' in seen["filter"] and seen["reportName"] == "RPT_PUBLIC_BS_APPOIN"
    a, b = c["events"]
    assert a == {"date": "2026-10-28", "period": "2026年三季报", "report_date": "2026-09-30", "published": False, "changed": True, "name": "贵州茅台"}
    assert b["date"] == "2026-08-14" and b["published"]  # 实际披露日优先于预约日
    assert c["source"].startswith("东方财富")


def test_calendar_yahoo_parses_earnings_dates(monkeypatch):
    import yfinance as yf
    idx = pd.DatetimeIndex([pd.Timestamp("2026-11-17 16:00", tz="America/New_York"), pd.Timestamp("2026-08-26 16:00", tz="America/New_York")])
    df = pd.DataFrame({"EPS Estimate": [2.47, 2.09], "Reported EPS": [float("nan"), 2.22], "Surprise(%)": [float("nan"), 6.16]}, index=idx)

    class T:
        def __init__(self, s):
            self.calendar = {"Revenue Average": 1.09e11, "Earnings Low": 2.34, "Earnings High": 2.7}

        def get_earnings_dates(self, limit=8):
            return df
    monkeypatch.setattr(yf, "Ticker", T)
    c = E.calendar_yahoo("NVDA")
    nxt, last = c["events"]
    assert nxt["date"] == "2026-11-17" and nxt["eps_est"] == 2.47 and nxt["eps_actual"] is None and not nxt["published"]
    assert last["eps_actual"] == 2.22 and last["surprise_pct"] == 6.16 and last["published"]
    assert c["revenue_est"] == 1.09e11 and "earnings?symbol=NVDA" in c["url"]


def test_tracked_from_holdings_and_watchlist():
    from stock_agent.agent import save_watchlist
    m = mem()
    m.add("user", "持有腾讯 0700.HK 200 股，成本 380 港元")
    m.add("user", "关注半导体")
    save_watchlist([{"symbol": "NVDA", "name": "英伟达"}, {"symbol": "0700.HK", "name": "腾讯控股"}])
    t = E.tracked(m)
    assert set(t) == {"0700.HK", "NVDA"}
    assert t["0700.HK"]["why"] == ["持仓", "自选"] and t["NVDA"]["why"] == ["自选"]


# ---------------------------------------------------------------- 研报
class Fake(E.Fetch):
    def __init__(self, events, quarterly=None, financials=None, consensus=None, anns=None, news=None, source="Yahoo Finance · Earnings"):
        self.ev, self.q, self.f, self.c = events, quarterly or [], financials or {}, consensus or {}
        self.a, self.n, self.src = anns or [], news or [], source
        self.calls = []

    def calendar(self, sym):
        self.calls.append(("calendar", sym))
        return {"symbol": sym, "events": self.ev, "source": self.src, "url": "https://example.com/cal", "as_of": "2026-10-29 21:00"}

    def financials(self, sym):
        return self.f

    def quarterly(self, sym):
        return self.q

    def consensus(self, sym):
        return self.c

    def announcements(self, sym):
        return self.a

    def news(self, sym):
        return self.n


Q = [{"period": "2026-06-30", "Total Revenue": 2.0e11, "Net Income": 5.6e10, "Diluted EPS": 6.1},
     {"period": "2026-03-31", "Total Revenue": 1.9e11}, {"period": "2025-12-31", "Total Revenue": 1.9e11}, {"period": "2025-09-30", "Total Revenue": 1.9e11},
     {"period": "2025-06-30", "Total Revenue": 1.8e11, "Net Income": 5.5e10, "Diluted EPS": 5.9}]
CONS = {"source": "Yahoo Finance", "url": "https://finance.yahoo.com/quote/0700.HK/analysis", "as_of": "2026-10-28", "target": {"low": 430.0, "mean": 650.0, "high": 890.0},
        "ratings": {"买入": 30}, "price_now": 512.4, "upside_pct": 26.9, "warnings": []}


def test_report_vs_consensus_yoy_thesis_and_files():
    m = mem()
    m.add("user", "持有腾讯 0700.HK 200 股，成本 380 港元")
    m.record_call("0700.HK", "腾讯控股", "看涨", 480, 30, "游戏流水增长、广告需求复苏")
    ev = {"date": "2026-08-12", "period": "季度财报", "eps_est": 7.24, "eps_actual": 7.43, "surprise_pct": 2.73, "published": True}
    f = Fake([ev], Q, consensus=CONS, news=[{"time": "2026-08-13 09:00", "title": "管理层：下半年广告展望乐观", "source": "东方财富", "url": "https://x/n1"}])
    pushed = []
    rep = E.make_report("0700.HK", m, fetch=f, push=lambda t, b: pushed.append((t, b)))
    md = rep["markdown"]
    assert rep["beat"] == "超预期" and rep["verdict"] == "加强" and rep["mode"] == "rules"
    for s in ("实际 vs 一致预期", "关键指标同比", "管理层指引", "与你的持仓逻辑对照", "—— 来源 ——", "7.43", "7.24", "+2.73%", "+11.11%", "管理层：下半年广告展望乐观",
              "游戏流水增长", "得到营收增长的印证", "仅供参考"):
        assert s in md, s
    assert "2026-06-30 季度" in md  # 报告期从利润表补上
    j = E.load_report(rep["id"])
    assert j["facts"]["vs_consensus"]["surprise_pct"] == 2.73 and j["sources"] and j["markdown"] == md
    st = E.load()
    assert st["reported"]["0700.HK|2026-08-12"] == rep["id"] and st["reports"][-1]["verdict"] == "加强"
    assert pushed and "财报研报" in pushed[0][0]
    from stock_agent import alerts
    assert alerts.load()["events"][-1]["kind"] == "earnings_report"


def test_a_share_completion_method():
    m = mem()
    m.record_call("600519.SS", "贵州茅台", "看涨", 1400, 30, "高端白酒需求复苏")
    fin = {"name": "贵州茅台", "source": "东方财富 F10", "main_indicators": [
        {"报告期": "2026三季报", "营业总收入": "1205.31亿", "营收同比%": -3.5, "归母净利润": "598.12亿", "净利同比%": -8.2, "每股收益": 48.0},
        {"报告期": "2025年报", "每股收益": 65.85}, {"报告期": "2025三季报", "每股收益": 49.5}]}
    cons = {"source": "东方财富 · 盈利预测", "url": "https://data.eastmoney.com/stockdata/600519.html", "as_of": "2026-10-28",
            "eps": [{"year": 2025, "kind": "实际", "eps": 65.85}, {"year": 2026, "kind": "预测", "eps": 67.18}]}
    rep = E.make_report("600519.SS", m, fetch=Fake([{"date": "2026-10-25", "period": "2026年三季报", "published": True}], financials=fin, consensus=cons), push=False)
    j = E.load_report(rep["id"])
    vc = j["facts"]["vs_consensus"]
    assert vc["completion"] == 71.4 and vc["completion_last"] == 75.2 and vc["verdict"] == "不及预期"
    assert rep["verdict"] == "削弱" and "1205.31亿" in rep["markdown"]


def test_judge_thesis_rules():
    vc_bad = {"verdict": "不及预期", "surprise_pct": -12.0}
    growth_bad = [{"指标": "营收", "同比%": -8.0}, {"指标": "净利润", "同比%": -35.0}]
    assert E.judge_thesis({"direction": "看多", "calls": []}, vc_bad, growth_bad)["verdict"] == "被证伪"
    assert E.judge_thesis({"direction": "看空", "calls": []}, vc_bad, growth_bad)["verdict"] == "加强"
    assert E.judge_thesis({"direction": "看空", "calls": []}, {"verdict": "超预期", "surprise_pct": 5}, [{"指标": "营收", "同比%": 12}])["verdict"] == "削弱"
    assert E.judge_thesis({"direction": "看多", "calls": []}, {"verdict": "符合预期"}, [{"指标": "营收", "同比%": 1}])["verdict"] == "基本不变"
    assert E.judge_thesis({"direction": None, "calls": [], "user": []}, vc_bad, growth_bad)["verdict"] is None


class JsonLLM:
    ready = True

    def __init__(self, payload):
        self.payload = payload

    def __repr__(self):
        return "fake"

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        return {"role": "assistant", "content": json.dumps(self.payload, ensure_ascii=False)}


def test_llm_layer_cannot_invent_numbers_or_be_rosier():
    facts = {"vs_consensus": {"rows": [{"实际": 7.43, "一致预期": 7.24}]}, "rule_thesis": {"verdict": "削弱"}}
    bad = {"headline": "EPS 9.99 大超预期", "guidance": "未见指引表述", "thesis_verdict": "加强", "thesis_reason": "很好", "watch": []}
    assert E.llm_layer(JsonLLM(bad), facts) is None  # 9.99 没出处 → 丢弃
    ok = {"headline": "EPS 7.43 略超 7.24", "guidance": "未见指引表述", "thesis_verdict": "加强", "thesis_reason": "超预期", "watch": ["指引"]}
    assert E.llm_layer(JsonLLM(ok), facts)["headline"].startswith("EPS 7.43")
    m = mem()
    m.add("user", "持有腾讯 0700.HK 200 股")
    ev = {"date": "2026-08-12", "eps_est": 7.24, "eps_actual": 7.0, "surprise_pct": -3.3, "published": True}
    rep = E.make_report("0700.HK", m, fetch=Fake([ev], [{"period": "2026-06-30", "Total Revenue": 1.0e11}, {}, {}, {}, {"period": "2025-06-30", "Total Revenue": 1.2e11}]),
                        llm=JsonLLM({**ok, "headline": "业绩不及预期"}), push=False)
    assert rep["verdict"] == "削弱" and rep["mode"] == "llm"  # 模型说「加强」也不能比规则（削弱）更乐观


def test_cite_registers_report_rows():
    from stock_agent import cite
    m = mem()
    ev = {"date": "2026-08-12", "eps_est": 7.24, "eps_actual": 7.43, "surprise_pct": 2.73, "published": True}
    res = E.report_for_agent("0700.HK", m, fetch=Fake([ev], Q, consensus=CONS))
    src = cite.Sources()
    out = src.add("earnings_report", {"symbol": "0700.HK"}, res)
    assert out["_cite"] and src.match(7.43, 2) and any(it["url"].startswith("https://finance.yahoo.com") for it in src.items)
    assert cite.verify("实际 EPS 7.43，一致预期 7.24，超出 2.73%。", src)["ok"]


# ---------------------------------------------------------------- 调度：提醒与研报
def test_tick_reminds_once_and_reports_after_release(monkeypatch):
    from stock_agent import alerts
    from stock_agent.agent import save_watchlist
    m = mem()
    m.add("user", "持有腾讯 0700.HK 200 股，成本 380 港元")
    m.record_call("0700.HK", "腾讯控股", "看涨", 480, 30, "广告需求复苏")
    save_watchlist([{"symbol": "NVDA", "name": "英伟达"}])

    class F(Fake):
        def calendar(self, sym):
            if sym == "NVDA":
                return {"symbol": sym, "events": [{"date": day(2), "period": "季度财报", "eps_est": 2.47, "eps_actual": None, "published": False}],
                        "source": "Yahoo Finance · Earnings", "url": "u", "as_of": "t"}
            return {"symbol": sym, "events": [{"date": day(-1), "period": "季度财报", "eps_est": 7.24, "eps_actual": 7.43, "surprise_pct": 2.73, "published": True},
                                              {"date": day(80), "period": "季度财报", "eps_est": 7.5, "published": False}],
                    "source": "Yahoo Finance · Earnings", "url": "u", "as_of": "t"}
    f = F([], Q, consensus=CONS)
    pushed = []
    out = E.tick(log=lambda *a: None, push=lambda t, b: pushed.append(t), fetch=f, mem=m)
    kinds = sorted(e["kind"] for e in out)
    assert kinds == ["earnings_report", "earnings_soon"]
    soon = next(e for e in out if e["kind"] == "earnings_soon")
    assert soon["symbol"] == "NVDA" and "2 天后" in soon["title"] and "2.47" in soon["title"]
    assert any("财报前提醒" in t for t in pushed) and any("财报研报" in t for t in pushed)
    assert (E.reports_dir() / f"0700.HK-{day(-1)}.md").exists()
    ev_kinds = [e["kind"] for e in alerts.load()["events"]]
    assert "earnings_soon" in ev_kinds and "earnings_report" in ev_kinds
    # 第二轮：不重复提醒、不重复出研报
    assert E.tick(log=lambda *a: None, push=lambda t, b: pushed.append(t), fetch=f, mem=m) == []
    v = E.calendar_view(m, fetch=f)
    assert [r["symbol"] for r in v["tracked"]] == ["NVDA", "0700.HK"] and v["tracked"][0]["days_left"] == 2 and v["reports"]


def test_disabled_and_window(monkeypatch):
    monkeypatch.setenv("EARNINGS", "off")
    assert E.tick(fetch=Fake([]), mem=mem()) == []
    monkeypatch.delenv("EARNINGS")
    m = mem()
    m.add("user", "持有腾讯 0700.HK 100 股")
    f = Fake([{"date": day(-20), "period": "季度财报", "eps_est": 1, "eps_actual": 1.1, "published": True}])
    assert E.tick(log=lambda *a: None, push=lambda *a: None, fetch=f, mem=m) == []  # 太久以前的财报不补研报
