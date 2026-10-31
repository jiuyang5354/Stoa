"""v0.37 子报告质量门（烂报告不进辩论，最多重做一次且受预算约束）+ 缺失调用记录与原样重放。"""
import json

import pytest

from stock_agent import debate as db
from stock_agent import missing, qgate
from tests.test_debate import make


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("AGORA_QGATE_RETRY", raising=False)
    monkeypatch.delenv("AGORA_QGATE_MIN_CHARS", raising=False)


# ---------------------------------------------------------------- 质量门
def test_grades():
    assert qgate.grade({"summary": "", "evidence": []})[0] == "F"
    assert qgate.grade({"summary": "看多", "evidence": []})[0] == "D"
    g, why = qgate.grade({"summary": "由于工具调用失败，无法获取最新财务数据，暂作中性判断。", "evidence": ["市盈率 18.6 倍，低于行业均值 25 倍"]})
    assert g == "D" and "放弃标记" in why[-1]
    assert qgate.grade({"summary": "I cannot retrieve the filings right now, neutral for now.", "evidence": ["n/a 1"]})[0] == "D"
    good = {"summary": "均线纠缠，趋势不明", "evidence": ["收盘 100.0", "20 日均线 100.98，60 日均线 100.931", "RSI 48.2", "近 20 日 -0.97%"]}
    assert qgate.grade(good)[0] == "A"
    assert qgate.grade({"summary": "公司宣布回购计划，管理层态度积极，市场情绪偏正面，短期关注执行进度与资金来源，同时留意大股东是否同步增持以及回购价格上限", "evidence": ["公告：董事会审议通过回购计划"]})[0] == "B"


class BadOnce(db.MockRoleLLM):
    """指定角色第一次交「无法获取」的烂报告，第二次正常（或一直烂）。"""

    def __init__(self, role="technical", always=False):
        super().__init__()
        self.role, self.always, self.n = role, always, 0

    def reply(self, role, user):
        if role == self.role:
            self.n += 1
            if self.always or self.n == 1:
                return json.dumps({"stance": "看多", "confidence": 70, "summary": "工具调用失败，无法获取行情。", "evidence": []}, ensure_ascii=False)
        return super().reply(role, user)


def test_bad_report_redone_once_then_ok():
    llm = BadOnce()
    d, _ = make(llm)
    rec = d.run(save=False)
    tech = next(a for a in rec["analysts"] if a["key"] == "technical")
    assert tech["status"] == "ok" and tech["quality"]["retried"] and tech["quality"]["grade"] in ("A", "B")
    assert llm.calls.count("technical") == 2 and rec["budget"]["calls"] <= rec["budget"]["max_calls"]


def test_still_bad_is_gated_out_of_debate():
    llm = BadOnce(always=True)
    d, _ = make(llm)
    rec = d.run(save=False)
    tech = next(a for a in rec["analysts"] if a["key"] == "technical")
    assert tech["status"] == "gated" and "质量门" in tech["error"] and "无法获取" not in (tech.get("summary") or "")
    assert llm.calls.count("technical") == 2  # 最多重做一次
    assert "技术面" in rec["degraded"]
    assert "规则降级" in d.reports_text() and "工具调用失败" not in d.reports_text()


def test_no_retry_when_budget_tight():
    llm = BadOnce(always=True)
    d, _ = make(llm, budget=db.Budget(9, 60000, 10))  # 4 分析师 + 2 多空 + 风控 + 裁决 = 8，留不出重做的调用
    rec = d.run(save=False)
    assert llm.calls.count("technical") == 1
    assert next(a for a in rec["analysts"] if a["key"] == "technical")["status"] == "gated"
    assert rec["verdict"]["view"] in db.VIEWS  # 后面的辩论/裁决照常有调用可用


def test_retry_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AGORA_QGATE_RETRY", "off")
    llm = BadOnce()
    make(llm)[0].run(save=False)
    assert llm.calls.count("technical") == 1


# ---------------------------------------------------------------- 缺失调用
def test_record_merge_and_replay():
    e1 = missing.record("get_fund_flow", {"symbol": "600519.SS", "days": 5}, "502 Bad Gateway", "web-x", "看资金流")
    e2 = missing.record("get_fund_flow", {"days": 5, "symbol": "600519.SS"}, "502 Bad Gateway", "web-x")
    assert e1["id"] == e2["id"] and e2["count"] == 2 and len(missing.listing()) == 1
    assert e1["hash"] == missing.call_hash("get_fund_flow", {"symbol": "600519.SS", "days": 5}) and e1["time"]
    r = missing.replay(e1["id"], lambda t, a: {"error": "还是 502"})
    assert not r["ok"] and missing.get(e1["id"])["status"] == "open"
    seen = []
    r = missing.replay(e1["id"], lambda t, a: seen.append((t, a)) or {"rows": [1, 2], "source": "东方财富"})
    assert r["ok"] and seen == [("get_fund_flow", {"symbol": "600519.SS", "days": 5})]  # 原样重放：同工具同参数
    assert missing.get(e1["id"])["status"] == "resolved"


def test_guard_blocks_and_side_effects_not_recorded():
    assert missing.is_failure({"blocked_by": "guard:consent", "error": "需要确认"}) is None
    assert missing.record("memory", {}, "x") is None
    assert missing.is_failure({"rows": [], "error": "部分失败", "a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6}) is None  # 大结果里的局部错误不算整次缺失


class LLM:
    ready = True

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return {"role": "assistant", "content": "查资金流。", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_fund_flow", "arguments": json.dumps({"symbol": "600519.SS"})}}]}
        return {"role": "assistant", "content": "资金流数据源暂时不可用，稍后可重放。"}


def test_agent_turn_shows_missing_card_and_learn_todo_links():
    from stock_agent import cards, learn
    from stock_agent.agent import Agent
    from stock_agent.memory import Memory

    class A(Agent):
        def run_tool(self, name, args):
            return {"error": "get_fund_flow 暂时取不到数据：502 Bad Gateway"}
    a = A(Memory(), channel="web", session="web-miss")
    a.llm = LLM()
    reply = a.ask("茅台主力资金流入了吗")
    cid = a.last_meta["cards"]
    assert cid and f"[[cards:{cid}]]" in reply
    c = next(x for x in cards.load(cid)["cards"] if x["type"] == "missing")
    assert c["items"][0]["tool"] == "get_fund_flow" and c["items"][0]["args"] == {"symbol": "600519.SS"} and "502" in c["items"][0]["error"]
    mid = c["items"][0]["id"]
    rule = learn.make_rule({"layer": "harness", "sub": "missing_data", "why": "工具没拿到数据"},
                           {"question": "茅台主力资金", "log": [{"tool": "get_fund_flow", "args": {}, "result": {"error": "502", "missing_id": mid}}]})
    assert rule["missing_ids"] == [mid] and "可重放" in rule["text"]
