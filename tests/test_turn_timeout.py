"""v0.36：单轮时长上限可配置（默认 900 秒）；到点不再空返回「模型等待超时」，而是用已拿到的工具结果收尾并注明未完成部分。"""
import json
import time

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_TURN_TIMEOUT", raising=False)
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    return tmp_path


Q = {"600519.SS": {"name": "贵州茅台", "date": "2026-10-28", "close": 1354.0, "chg_pct": -0.84, "ma20": 1398.6, "source": "测试"}}


class LLM:
    ready = True

    def __init__(self, hard=False):
        self.hard, self.calls, self.final_tools = hard, 0, None

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        if messages[-1]["role"] == "tool" and self.hard:
            raise TimeoutError("request deadline")
        if tools:
            return {"role": "assistant", "content": "查行情。", "tool_calls": [
                {"id": f"c{self.calls}", "type": "function", "function": {"name": "get_quotes", "arguments": json.dumps({"symbols": ["600519.SS"]})}}]}
        self.final_tools = tools
        return {"role": "assistant", "content": "贵州茅台收于 1354.0，其余对比没来得及查完。"}


def _agent(llm, delay=0.0):
    from stock_agent.agent import Agent
    from stock_agent.memory import Memory

    class A(Agent):
        def run_tool(self, name, args):
            time.sleep(delay)
            return dict(Q)
    a = A(Memory(), channel="web", session="web-tt")
    a.llm = llm
    a.tools = [t for t in a.tools if t["name"] == "get_quotes"]
    return a


def test_turn_timeout_config(home, monkeypatch):
    from stock_agent import agent as AG
    assert AG.turn_timeout() == 900
    monkeypatch.setattr(AG, "load_cfg", lambda: {"agent": {"turn_timeout": 1200}})
    assert AG.turn_timeout() == 1200
    monkeypatch.setenv("AGENT_TURN_TIMEOUT", "45")
    assert AG.turn_timeout() == 45
    monkeypatch.setenv("AGENT_TURN_TIMEOUT", "5")
    assert AG.turn_timeout() == 30  # 下限
    monkeypatch.setenv("AGENT_TURN_TIMEOUT", "abc")
    assert AG.turn_timeout() == 900


def test_repo_config_default_is_900():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).parents[1] / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["agent"]["turn_timeout"] == 900


def test_soft_deadline_finishes_with_tool_results(home, monkeypatch):
    """接近上限：不再给工具，让模型用已拿到的结果作答，并注明之后的查询没有继续。"""
    from stock_agent import agent as AG
    monkeypatch.setattr(AG, "turn_timeout", lambda: 1)
    llm = LLM()
    a = _agent(llm, delay=1.0)
    r = a.ask("茅台和五粮液、泸州老窖对比一下")
    assert "1354.0" in r and "秒上限" in r and "模型等待超时" not in r
    assert llm.final_tools is None and llm.calls == 2  # 第二次调用没有给工具


def test_hard_timeout_answers_from_tool_results(home):
    from stock_agent import ledger
    llm = LLM(hard=True)
    a = _agent(llm)
    r = a.ask("茅台现在多少")
    assert "模型等待超时" not in r and "秒上限" in r and "未完成" in r
    assert "查行情" in r and "1354" in r  # 已拿到的工具结果写进了答复
    assert a.last_meta["timeout"] and a.last_meta["partial"]
    hist = a.mem.history("web-tt")
    assert [h["role"] for h in hist] == ["user", "assistant"] and "秒上限" in hist[-1]["text"]
    assert [m["role"] for m in a.messages[1:]] == ["user", "assistant"]  # 上下文仍然合法，下一问可以接着聊
    runs = ledger.runs_list()
    assert runs and runs[0]["status"] == "done"
