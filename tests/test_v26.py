import json

import numpy as np
import pandas as pd

from stock_agent import agent, cards, quant, skills


def test_calc_chain_and_errors():
    r = quant.calc({"gm": "share(g, rev)", "yoy": "pct(rev, prev)", "d": "gm - 40", "bad": "__import__('os')"}, {"g": 45, "rev": 100, "prev": 80})["results"]
    assert r["gm"] == 45 and r["yoy"] == 25 and r["d"] == 5 and r["bad"] is None
    assert quant.calc("cagr(200, 100, 2)")["results"]["result"] == round((2 ** .5 - 1) * 100, 6)
    assert quant.calc("2 ** 1000")["results"]["result"] is None


def test_levels_from_closes():
    x = np.concatenate([np.linspace(100, 120, 20), np.linspace(120, 100, 20)] * 4 + [np.linspace(100, 110, 10)])
    lv = quant.levels_from_closes(x)
    assert lv["supports"] and lv["resistances"]
    assert abs(lv["supports"][0]["price"] - 100) < 2 and abs(lv["resistances"][0]["price"] - 120) < 2
    json.dumps(lv)


def test_option_payoff_bull_spread():
    r = quant.option_payoff([{"type": "call", "side": "long", "strike": 100, "premium": 5}, {"type": "call", "side": "short", "strike": 120, "premium": 2}], 100)
    assert r["breakevens"] == [103.0] and r["max_profit"] == 17.0 and r["max_loss"] == -3.0
    assert quant.option_payoff([{"type": "call", "side": "short", "strike": 100, "premium": 5}], 100)["max_loss"] == "无下限"


def test_replay_paths_probabilities():
    rets = np.random.default_rng(1).normal(0, 0.02, 750)
    r = quant.replay_paths(rets, 100, 95, 105, horizon=20, n=4000)
    assert abs(r["p_target_first"] + r["p_stop_first"] + r["p_neither"] - 100) < 0.2
    assert 25 < r["p_target_first"] < 60 and 25 < r["p_stop_first"] < 60  # 对称的计划概率接近
    assert len(r["fan"]) == 20 and len(r["sample"]) <= 24


def test_trade_replay_validation(monkeypatch):
    from stock_agent import data
    ser = pd.Series(np.exp(np.cumsum(np.random.default_rng(2).normal(0, .01, 800))) * 50, index=pd.bdate_range("2023-01-02", periods=800))
    monkeypatch.setattr(data, "fetch_closes", lambda s, period="1y": {s[0]: ser})
    assert "error" in quant.trade_replay("X", stop=60, target=70)
    r = quant.trade_replay("X", stop=float(ser.iloc[-1]) * .9, target=float(ser.iloc[-1]) * 1.1, n=2000)
    assert r["n"] == 2000 and r["entry"] == float(ser.iloc[-1])


def test_bridge_steps_and_cards():
    st = quant._bridge_steps(100.0, 40.0, 15.0, 10.0)
    assert [s["label"] for s in st] == ["营收", "营业成本", "毛利", "经营费用", "经营利润", "利息税项及其他", "净利润"]
    assert st[1]["value"] == -60 and st[3]["value"] == -25 and st[5]["value"] == -5
    c = cards.build("financial_bridge", {}, {"symbol": "X", "steps": st, "ratios": {"毛利率%": 40}, "source": "t"})
    assert c[0]["type"] == "bridge"
    c = cards.build("trade_replay", {}, {"symbol": "X", "fan": [[1, 1, 2, 3]], "p_target_first": 40})
    assert c[0]["type"] == "replay" and c[0]["fan"]


def test_tools_registered_and_skills():
    names = {t["name"] for t in agent.TOOLS}
    assert agent.QUANT_TOOLS <= names and agent.QUANT_TOOLS <= set(agent.TOOL_LABELS)
    assert agent.QUANT_TOOLS - {"calc"} <= cards.CARD_TOOLS
    have = {s["name"] if isinstance(s, dict) else s.name for s in skills.list_skills()}
    assert {"trade-replay", "equity-deep-dive", "crowd-pulse", "options-strategy"} <= have


# ---------------- v0.27 fundamentals ----------------
from stock_agent import fundamentals as F


def _p(rev, cogs, ni, cfo, ta, ar, inv, ca, cl, tl, eq, ppe=10.0, capex=5.0, sh=100.0):
    return {"period": "x", "income": {"revenue": rev, "cogs": cogs, "net_income": ni, "parent_ni": ni, "selling": rev * .1, "admin": rev * .05, "pretax": ni * 1.25},
            "balance": {"total_assets": ta, "receivables": ar, "inventory": inv, "current_assets": ca, "current_liab": cl, "total_liab": tl, "equity": eq,
                        "parent_equity": eq, "ppe": ppe, "share_capital": sh, "retained": eq * .5, "cash": 20.0},
            "cashflow": {"cfo": cfo, "capex": capex, "cfi": -capex, "cff": -10.0, "cash_change": cfo - capex - 10.0, "begin_cash": 10.0, "end_cash": 10.0 + cfo - capex - 10.0}}


def test_tie_outs_and_scores():
    t, p = _p(120, 60, 24, 30, 200, 20, 15, 100, 50, 80, 120), _p(100, 52, 18, 20, 180, 18, 14, 90, 50, 80, 100)
    assert all(x["ok"] for x in F.tie_outs(t))
    bad = _p(120, 60, 24, 30, 200, 20, 15, 100, 50, 80, 100)
    assert any(not x["ok"] for x in F.tie_outs(bad))
    m, _ = F._beneish(t, p)
    assert m["m_score"] is not None and set(m["components"]) == {"DSRI", "GMI", "AQI", "SGI", "DEPI", "SGAI", "LVGI", "TATA"}
    f = F._piotroski(t, p)
    assert 0 <= f["f_score"] <= 9 and f["signals"]["ROA>0"] and f["signals"]["经营现金流 > 净利润"]
    z = F._altman(t, 300.0)
    assert z["z_score"] > 0


def test_pv_monotonic_and_reverse_solves():
    assert F._pv(100, 0.10, 10, 0.09, 0.025) > F._pv(100, 0.05, 10, 0.09, 0.025)
    target = F._pv(100, 0.08, 10, 0.09, 0.025)
    lo, hi = -0.5, 1.5
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if F._pv(100, mid, 10, 0.09, 0.025) < target else (lo, mid)
    assert abs(mid - 0.08) < 1e-6


def test_quarter_split(monkeypatch):
    rows = {"income": [{"REPORT_DATE": d + " 00:00:00", "TOTAL_OPERATE_INCOME": v, "NETPROFIT": v / 10, "BASIC_EPS": v / 100}
                       for d, v in [("2026-09-30", 300), ("2026-06-30", 200), ("2026-03-31", 90), ("2025-12-31", 400)]],
            "balance": [{"REPORT_DATE": d + " 00:00:00", "TOTAL_ASSETS": 1000} for d in ("2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31")],
            "cashflow": [{"REPORT_DATE": d + " 00:00:00", "NETCASH_OPERATE": v} for d, v in [("2026-09-30", 60), ("2026-06-30", 50), ("2026-03-31", 5), ("2025-12-31", 80)]]}
    monkeypatch.setattr(F, "_em_rows", lambda secu, kind, n=24: rows[kind])
    st = F._statements_a({"code": "600000", "name": "T", "symbol": "600000.SS"}, "quarter", 3)
    q = {p["period"]: p for p in st["periods"]}
    assert q["2026Q3"]["income"]["revenue"] == 100 and q["2026Q2"]["income"]["revenue"] == 110 and q["2026Q1"]["income"]["revenue"] == 90
    assert q["2026Q3"]["cashflow"]["cfo"] == 10 and q["2026Q3"]["income"]["eps"] is None and q["2026Q1"]["income"]["eps"] == 0.9
    st = F._statements_a({"code": "600000", "name": "T", "symbol": "600000.SS"}, "annual", 3)
    assert [p["period"] for p in st["periods"]] == ["2025年报"]


def test_reg_pattern_and_registration():
    assert F.REG_PAT.search("关于对公司2025年年报的问询函的回复公告") and not F.REG_PAT.search("2026年第三季度报告")
    names = {t["name"] for t in agent.TOOLS}
    assert agent.FUND_TOOLS <= names and agent.FUND_TOOLS <= set(agent.TOOL_LABELS)
    assert {"earnings_quality", "reverse_dcf", "dcf_scenarios"} <= cards.CARD_TOOLS


def test_cite_no_false_markers():
    from stock_agent import cite
    src = cite.Sources()
    src.add("skill_view", {"name": "x"}, {"content": "折现率 7% 情景 30% 阈值 0.8"})
    src.add("get_financials", {"symbol": "600519.SS"}, {"symbol": "600519.SS", "pe": 16.79, "revenue_yoy": 1.82, "code": 600519, "out": 2354000})
    src.add("web_search", {"query": "q"}, {"results": [{"title": "白酒产量", "url": "https://x.com/a", "snippet": "产量 235.4 万千升"}]})
    text = "贵州茅台（600519）市盈率 16.79 倍[2]，收入增长 1.82%，产量 235.4 万千升，折现率 7%。"
    out, _ = cite.finalize(text, src)
    body = cite.strip_sources(out)
    assert "600519[" not in body and "7%[" not in body
    assert "16.79 倍[" in body and "16.79[" not in body
    assert "万千升[" in body and "万[" not in body
    assert "1.82%[" in body
