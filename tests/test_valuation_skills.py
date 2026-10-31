"""v0.37 计算口径模板：LBO/DCF/EV/FCCR 与宏观取数口径做成按需加载的内置技能——命中相关问题才附全文，回答后从上下文拿掉。"""
import json

import pytest

from stock_agent import skills


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    return tmp_path


@pytest.mark.parametrize("q,want", [
    ("Build a simple LBO: what equity check does the sponsor need?", "valuation-math"),
    ("计算 FY2024 的 FCCR（fixed charge coverage ratio）", "valuation-math"),
    ("用 DCF 估算每股价值，WACC 9%", "valuation-math"),
    ("What is FTAI's EV/EBITDA versus the peer average?", "valuation-math"),
    ("Bank credit of commercial banks, data from July 18, 2025, seasonally adjusted", "macro-data-rules"),
    ("In September 2022, what was the federal funds rate?", "macro-data-rules"),
    ("2008 年金融危机期间恒生指数最大单周跌幅是哪一周", "macro-data-rules"),
])
def test_auto_load_hits(home, q, want):
    names = [a["name"] for a in skills.auto_load(q)]
    assert want in names


@pytest.mark.parametrize("q", ["茅台今天涨了多少？", "帮我设一个跌破 1300 的提醒", "给我讲讲腾讯最近的新闻", "推荐一本书"])
def test_auto_load_quiet_on_everyday_questions(home, q):
    assert skills.auto_load(q) == []


def test_checklist_covers_known_failure_modes(home):
    v = skills.view("valuation-math", count=False)["content"]
    for must in ("毛利息", "不默认当资金来源", "优先股", "少数股东", "10-Q", "mid-year", "同业均值", "不要因为财年错位就拒算"):
        assert must in v, must
    m = skills.view("macro-data-rules", count=False)["content"]
    for must in ("vintage", "目标区间", "上周最后一个交易日", "SA", "full=true"):
        assert must in m, must


def test_user_override_body_used(home):
    skills.save("valuation-math", "我的口径", "只用我的清单：股本取 10-K。")
    hit = skills.auto_load("LBO equity check")
    assert hit and hit[0]["content"] == "只用我的清单：股本取 10-K。"


class LLM:
    ready = True

    def __init__(self):
        self.seen = []

    def chat(self, messages, tools=None, **kw):
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        return {"role": "assistant", "content": "按毛利息口径，FCCR 需要先取利息费用原值。"}


def _agent(llm):
    from stock_agent.agent import Agent
    from stock_agent.memory import Memory
    a = Agent(Memory(), channel="web", session="web-vs")
    a.llm = llm
    return a


def test_agent_attaches_skill_for_this_turn_only(home):
    llm = LLM()
    a = _agent(llm)
    a.ask("Compute the FCCR for FY2024 using the credit agreement definition")
    first = llm.seen[0][-1]["content"]
    assert "valuation-math" in first and "毛利息" in first
    assert a.auto_skills == ["valuation-math"]
    hist = [m for m in a.messages if m["role"] == "user"][-1]["content"]
    assert "毛利息" not in hist and hist.startswith("Compute the FCCR")  # 回答后从上下文拿掉
    a.ask("谢谢，再说说今天大盘")
    assert "不默认当资金来源" not in json.dumps(llm.seen[-1], ensure_ascii=False) and a.auto_skills == []
