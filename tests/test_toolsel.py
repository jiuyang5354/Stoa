"""v0.37 工具面收敛：常驻 12 个 + 意图预选组 + tool_search 兜底；不多一次模型请求；每轮固定开销大幅下降。"""
import json
from pathlib import Path

import pytest
import yaml

from stock_agent import context, toolsel


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    monkeypatch.delenv("TOOL_SELECT", raising=False)
    return tmp_path


class LLM:
    ready = True

    def __init__(self, script=()):
        self.script, self.calls, self.seen_tools, self.seen_sys = list(script), 0, [], []

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        self.seen_tools.append([t["name"] for t in tools or []])
        self.seen_sys.append(messages[0]["content"])
        if self.script:
            name, args = self.script.pop(0)
            return {"role": "assistant", "content": "查一下。", "tool_calls": [
                {"id": f"c{self.calls}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
        return {"role": "assistant", "content": "好的。"}


def _agent(llm, results=None):
    from stock_agent.agent import Agent
    from stock_agent.memory import Memory

    class A(Agent):
        def run_tool(self, name, args):
            if name == "tool_search":
                return self._tool_search(args)
            return (results or {}).get(name, {"ok": True})
    a = A(Memory(), channel="web", session="web-ts")
    a.llm = llm
    a._no_debate = True
    return a


def test_core_is_twelve_and_known():
    from stock_agent.agent import TOOLS
    names = {t["name"] for t in TOOLS}
    assert len(toolsel.CORE_TOOLS) == 12 and set(toolsel.CORE_TOOLS) <= names
    grouped = {n for tools, _, _ in toolsel.GROUPS.values() for n in tools}
    assert names - set(toolsel.CORE_TOOLS) - {"sweep_filings"} <= grouped  # 每个域工具都属于某个组（能被预选/检索到）
    assert all(n in toolsel.DOMAIN_INDEX for n in grouped)
    assert len(toolsel.DOMAIN_INDEX) < 900


def test_fixed_overhead_drops(home):
    full = _agent(LLM())
    full_cost = context.tools_tokens(full.tools) + context.est_tokens(full.messages[0]["content"])
    llm = LLM()
    a = _agent(llm)
    a.ask("茅台今天涨了多少？")
    sent = [t for t in a.turn_tools()]
    tools_cost = context.tools_tokens(sent)
    assert len(sent) == 13 and tools_cost < 1800  # 估算器比 tiktoken 偏高约 15%：≈1,500 token
    fixed = tools_cost + context.est_tokens(llm.seen_sys[0])
    assert fixed < 0.35 * full_cost and fixed < 6000, (fixed, full_cost)
    a.ctx_budget = None
    assert a.budget() > context.budget(a.llm, full.tools)  # 省下的开销回到可用上下文


def test_intent_groups_cover_eval_cases():
    cases = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("stock_agent/evals/cases.yaml").read_text(encoding="utf-8"))
    cases = cases.get("cases", cases) if isinstance(cases, dict) else cases
    pool = [{"name": n} for n in set(toolsel.CORE_TOOLS) | {n for tools, _, _ in toolsel.GROUPS.values() for n in tools}]
    miss = []
    for c in cases:
        want = (c.get("checks") or {}).get("tool_called")
        if not want or len(c.get("turns") or []) != 1:
            continue
        want = [want] if isinstance(want, str) else want
        got = {t["name"] for t in toolsel.select(c["turns"][0], pool)[0]}
        if not set(want) & got:
            miss.append((c["id"], want))
    assert not miss, miss


@pytest.mark.parametrize("q,must", [
    ("茅台最近 5 天主力资金是流入还是流出？", "get_fund_flow"),
    ("NVDA 期权的隐含波动率是多少", "get_options"),
    ("Calculate FTAI's FY2024 FCCR from the 10-K", "sec_xbrl"),
    ("2008年金融危机期间恒生指数最大单周跌幅", "price_history"),
    ("2018年3月23日申万一级行业跌幅前五", "cn_data"),
    ("英伟达跌破 180 美元提醒我", "alert_set"),
    ("我以前给腾讯定的离场价是多少来着？", "search_memory"),
    ("宁德时代的支撑位和阻力位", "price_levels"),
])
def test_preselect_examples(q, must):
    from stock_agent.agent import TOOLS
    assert must in {t["name"] for t in toolsel.select(q, TOOLS)[0]}


def test_tool_search_unlocks_without_extra_round(home):
    llm = LLM([("tool_search", {"query": "龙虎榜 席位"}), ("get_dragon_tiger", {"symbol": "600519.SS"})])
    a = _agent(llm)
    a.ask("帮我看看这只票 600519 的情况")
    assert "get_dragon_tiger" not in llm.seen_tools[0] and "tool_search" in llm.seen_tools[0]
    assert "get_dragon_tiger" in llm.seen_tools[1]  # 找到后下一步就带上 schema
    assert llm.calls == 3  # tool_search 本身就是一次正常工具步，没有额外的模型请求


def test_direct_call_by_name_runs_even_without_schema(home):
    llm = LLM([("get_dragon_tiger", {"symbol": "600519.SS"})])
    a = _agent(llm, {"get_dragon_tiger": {"records": []}})
    a.ask("帮我看看 600519")
    assert "get_dragon_tiger" not in llm.seen_tools[0] and llm.calls == 2
    assert any(s["name"] == "get_dragon_tiger" for s in a._steps)
    a.ask("那它呢？")  # 追问：上一轮用过的域工具继续带上
    assert "get_dragon_tiger" in llm.seen_tools[-1]


def test_search_ranks_by_keywords_and_vectors():
    from stock_agent.agent import TOOLS
    assert toolsel.search("龙虎榜", TOOLS)[0]["name"] == "get_dragon_tiger"
    assert "macro_series" in [t["name"] for t in toolsel.search("FRED macro series vintage", TOOLS, 3)]
    assert "task_create" in [t["name"] for t in toolsel.search("每天定时执行", TOOLS, 3)]


def test_group_notes_only_when_selected(home):
    llm = LLM()
    a = _agent(llm)
    a.ask("茅台今天涨了多少？")
    assert "域工具" in llm.seen_sys[0] and "get_fund_flow（仅 A股" not in llm.seen_sys[0] and "关于 Stoa 自己" not in llm.seen_sys[0]
    a.ask("茅台主力资金流入了吗")
    assert "get_fund_flow（仅 A股" in llm.seen_sys[-1]
    a.ask("Stoa 更新了什么？")
    assert "关于 Stoa 自己" in llm.seen_sys[-1]


def test_select_all_escape_hatch(home, monkeypatch):
    monkeypatch.setenv("TOOL_SELECT", "all")
    from stock_agent.agent import TOOLS
    picked, groups = toolsel.select("你好", TOOLS)
    assert len(picked) == len(TOOLS) and groups == ["all"]


def test_mcp_tools_always_sent():
    pool = [{"name": "get_quotes"}, {"name": "mcp__x__lookup"}]
    assert "mcp__x__lookup" in {t["name"] for t in toolsel.select("你好", pool)[0]}
