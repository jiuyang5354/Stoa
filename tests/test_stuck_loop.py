"""原地打转的硬约束（guards 规则 7）：同一工具同一参数，结果一字不差地调两次，第三次拦下。

对应 Super-agent 那篇里的 Watchdog：用确定性代码判断「有没有实质进展」，而不是等模型自省——
模型陷入无效循环时，提示词救不回来，代码可以。
"""
import json

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    return tmp_path


def test_third_identical_call_is_blocked_and_counted():
    from stock_agent import guards
    c = {}
    for _ in range(2):
        assert guards.check("get_quotes", {"symbols": ["NVDA"]}, "", c) is None
        guards.observe("get_quotes", {"symbols": ["NVDA"]}, {"NVDA": {"close": 100.5}}, c)
    b = guards.check("get_quotes", {"symbols": ["NVDA"]}, "", c)
    assert b and b["blocked_by"] == "guard:repeat"
    assert "一模一样" in b["error"] and "卡在哪" in b["error"]
    assert c["_repeat"] == ["get_quotes"]


def test_changing_results_are_never_called_a_loop():
    """轮询行情：参数一样但结果在变，是正常的，不能拦。"""
    from stock_agent import guards
    c = {}
    for i in range(8):
        assert guards.check("get_quotes", {"symbols": ["NVDA"]}, "", c) is None
        guards.observe("get_quotes", {"symbols": ["NVDA"]}, {"NVDA": {"close": 100 + i}}, c)
    assert "_repeat" not in c


def test_different_arguments_are_a_different_call():
    from stock_agent import guards
    c = {}
    for sym in ("NVDA", "AMD", "INTC"):
        assert guards.check("get_quotes", {"symbols": [sym]}, "", c) is None
        guards.observe("get_quotes", {"symbols": [sym]}, {"close": 1}, c)
    assert "_repeat" not in c


def test_arg_order_does_not_matter():
    from stock_agent import guards
    c = {}
    a1, a2 = {"symbol": "NVDA", "days": 5}, {"days": 5, "symbol": "NVDA"}
    for args in (a1, a2):
        assert guards.check("history", args, "", c) is None
        guards.observe("history", args, {"bars": []}, c)
    assert guards.check("history", a1, "", c)["blocked_by"] == "guard:repeat"


def test_agent_turns_a_stuck_loop_into_a_visible_error(home):
    """模型连续 5 次调同一个工具：工具只真跑 2 次，第 3 次起模型看到明确的拦截理由。"""
    from stock_agent.agent import Agent

    class Stuck:
        """永远要求再查一次行情。"""

        ready = True

        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, **kw):
            self.n += 1
            if self.n > 6:
                return {"role": "assistant", "content": "查不动了，先说结论。"}
            return {"role": "assistant", "content": "再查一次。", "tool_calls": [
                {"id": f"c{self.n}", "type": "function",
                 "function": {"name": "get_quotes", "arguments": json.dumps({"symbols": ["NVDA"]})}}]}

    ran = {"n": 0}

    class A(Agent):
        def run_tool(self, name, args):
            ran["n"] += 1
            return {"NVDA": {"close": 100.5, "date": "2026-10-30", "source": "测试"}}

    a = A(channel="web", session="web-stuck")
    a.llm = Stuck()
    a.tools = [t for t in a.tools if t["name"] == "get_quotes"]
    a.ask("英伟达现在多少？")
    assert ran["n"] <= 2, "同一调用最多真跑两次，之后不该再执行"
    blocked = [str(m.get("content")) for m in a.messages
               if m.get("role") == "tool" and "一模一样" in str(m.get("content"))]
    assert blocked, "模型必须看到「别再重复」的理由，而不是默默拿到同样的结果"
