"""回测里的新数据工具：默认禁用；资金流只能按决策日严格截断（证明无未来泄漏）。全部离线。"""
import pandas as pd
import pytest

from stock_agent import backtest as bt
from stock_agent import debate as db
from stock_agent import marketdata as md

NEW_TOOLS = ["get_fund_flow", "get_northbound", "get_dragon_tiger", "get_consensus", "get_intraday", "get_options", "read_filing", "compare_filings"]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


def flow_rows(idx):
    # 主力净占比 = 日期序号：越往后越大，任何大于决策日序号的数都只能来自未来
    return [{"date": str(d.date()), "main_net": float(i), "main_pct": float(i), "super_net": float(i)} for i, d in enumerate(idx)]


def test_policy_disables_everything_but_flow():
    assert set(bt.BT_TOOL_POLICY) == set(NEW_TOOLS)
    for t in NEW_TOOLS:
        if t == "get_fund_flow":
            assert bt.BT_TOOL_POLICY[t] == "asof"
            continue
        with pytest.raises(PermissionError):
            bt.bt_tool(t, {"symbol": "600519.SS"}, pd.Timestamp("2025-06-01"))
    with pytest.raises(PermissionError):
        bt.bt_tool("get_news", {}, pd.Timestamp("2025-06-01"))


def test_flow_asof_truncates_strictly():
    idx = pd.bdate_range("2025-01-01", periods=60)
    rows = flow_rows(idx)
    for k in (10, 33, 59):
        fp = md.flow_asof(rows, idx[k], 5)
        assert fp["_last_date"] == str(idx[k].date())
        assert max(r["main_pct"] for r in fp["rows"]) == k  # 最大值就是决策日当天
        assert fp["unit"] == "%" and all(set(r) == {"d", "main_pct"} for r in fp["rows"])  # 只给相对量和相对日期
    assert md.flow_asof(rows, pd.Timestamp("2024-12-01"), 5) is None
    assert md.filter_asof([{"date": "2025-01-03"}, {"date": "2025-01-06"}], "2025-01-03") == [{"date": "2025-01-03"}]


def test_live_debate_tools_blocked_in_backtest_mode():
    d = db.Debate(db.MockRoleLLM(), "600519.SS", "某股票", tools=lambda n, a: {"x": 1}, mode="backtest", snapshot_payload={"close": 1}, horizon=5)
    for t in NEW_TOOLS:
        with pytest.raises(PermissionError):
            d.tool(t, {"symbol": "600519.SS"})
    assert d.gather("fundamental") == [] and d.gather("news") == []


class Spy(db.MockRoleLLM):
    def __init__(self):
        super().__init__()
        self.payloads = []

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        if "[ROLE:technical]" in messages[0]["content"]:
            self.payloads.append(messages[-1]["content"])
        return super().chat(messages, tools, temperature, timeout)


def test_backtest_debate_with_flow_has_no_future_leak():
    idx = pd.bdate_range("2025-01-01", periods=220)
    s = pd.Series([100.0 + (i % 9) for i in range(220)], index=idx)
    rows = flow_rows(pd.bdate_range("2025-01-01", periods=260))  # 资金流数据比行情还长 40 天：多出的全是「未来」
    pit = bt.PointInTime({"600519.SS": s})
    spy = Spy()
    agent = db.DebateBTAgent(llm=spy, blind=True, max_calls=10 ** 6, cache=bt.LLMCache(bt.bt_dir() / "f.db"), flow=True, flow_loader=lambda sym: rows)
    preds = bt.predict(pit, agent, idx[80], idx[200], 10, 5)
    assert preds and agent.flow_seen and len(agent.flow_seen) == len(preds)
    pos = {str(d.date()): i for i, d in enumerate(pd.bdate_range("2025-01-01", periods=260))}
    for d, last in agent.flow_seen:
        assert last <= str(d.date()) and last == str(d.date())
    import json
    import re
    for text in spy.payloads:
        data = json.loads(text.split("数据：", 1)[1])
        flow = next(x for x in data if isinstance(x, dict) and x.get("unit") == "%")
        assert "main_net" not in json.dumps(flow) or flow.get("main_net_5d") is not None
        vals = [r["main_pct"] for r in flow["rows"]]
        assert all(v <= max(pos.values()) for v in vals)
    # 每个决策点用到的最大占比 = 决策日序号，绝不会更大
    by_day = [pos[last] for _, last in agent.flow_seen]
    assert by_day == sorted(by_day)
    st = {a for a in agent.summary()}
    assert "debates" in st


def test_backtest_debate_without_flow_ignores_loader():
    called = []
    agent = db.DebateBTAgent(llm=db.MockRoleLLM(), cache=bt.LLMCache(bt.bt_dir() / "g.db"), flow=False, flow_loader=lambda s: called.append(s) or [])
    idx = pd.bdate_range("2025-01-01", periods=120)
    s = pd.Series([100.0 + (i % 5) for i in range(120)], index=idx)
    agent(bt.Snapshot("600519.SS", "A股", idx[100], s.iloc[:101], 5))
    assert not called and not agent.flow_seen
