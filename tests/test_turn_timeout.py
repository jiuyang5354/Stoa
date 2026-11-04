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


def test_hard_timeout_answers_from_tool_results(home, monkeypatch):
    """v0.38.1：请求一直超时（不是整轮到点）→ 退避重试用完后用已拿到的结果收尾，写真实原因（以前误写成「到了 900 秒上限」）。"""
    from stock_agent import ledger
    monkeypatch.setenv("LLM_RETRY_BASE", "0.01")
    llm = LLM(hard=True)
    a = _agent(llm)
    r = a.ask("茅台现在多少")
    assert "模型等待超时" not in r and "连续 4 次超时" in r and "900 秒上限" not in r and "未完成" in r
    assert "查行情" in r and "1354" in r  # 已拿到的工具结果写进了答复
    assert a.last_meta["timeout"] and a.last_meta["partial"]
    hist = a.mem.history("web-tt")
    assert [h["role"] for h in hist] == ["user", "assistant"] and "超时" in hist[-1]["text"]
    assert [m["role"] for m in a.messages[1:]] == ["user", "assistant"]  # 上下文仍然合法，下一问可以接着聊
    runs = ledger.runs_list()
    assert runs and runs[0]["status"] == "done"


def test_continue_after_timeout_reuses_fetched_tools_and_sources(home, monkeypatch):
    """上一轮超时中断后，用户回复「继续」直接复用上一轮已完成工具的结果与引用源，不必重跑工具。"""
    monkeypatch.setenv("LLM_RETRY_BASE", "0.01")
    tool_runs = {"n": 0}

    class ContinueLLM:
        ready = True

        def __init__(self):
            self.turn = 0
            self.calls = 0

        def chat(self, messages, tools=None, **kw):
            self.calls += 1
            if self.turn == 0:
                if messages[-1]["role"] == "tool":
                    raise TimeoutError("request deadline")
                return {"role": "assistant", "content": "先查行情。下一步并行取：盈利质量、利润桥、一致预期、持仓逻辑。", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "get_quotes", "arguments": json.dumps({"symbols": ["600519.SS"]})}}]}
            # 第二轮用户说「继续」：模型直接引用上一轮已取到的 1354.0 作答，不再调 get_quotes
            return {"role": "assistant", "content": "接着上一轮已取到的数据：贵州茅台收盘 1354.0 元[1]，跌幅 0.84%[1]。"}

    from stock_agent.agent import Agent
    from stock_agent.memory import Memory

    llm = ContinueLLM()

    class A(Agent):
        def run_tool(self, name, args):
            tool_runs["n"] += 1
            return dict(Q)

    a = A(Memory(), channel="web", session="web-cont")
    a.llm = llm
    r1 = a.ask("深度看看贵州茅台")
    assert a.last_meta["timeout"] and tool_runs["n"] == 1
    llm.turn = 1
    r2 = a.ask("继续")
    assert tool_runs["n"] == 1  # 没有重跑 get_quotes
    assert "1354.0" in r2 and "未核实" not in r2  # 引用核对通过
    picked_names = {t["name"] for t in a.turn_tools()}
    assert {"earnings_quality", "financial_bridge", "get_consensus", "memory_links"} <= picked_names


def test_fake_ip_and_sec_ua_in_fetch_url(monkeypatch):
    from stock_agent import research
    monkeypatch.setattr(research.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("198.18.0.25", 0))])
    assert research._public("www.sec.gov") is True
    monkeypatch.setattr(research.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("127.0.0.1", 0))])
    assert research._public("localhost") is False
