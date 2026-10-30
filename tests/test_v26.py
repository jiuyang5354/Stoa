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
