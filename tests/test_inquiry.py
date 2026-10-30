import json
import os
import threading

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("STOA_MOCK_LLM", "1")
    from stock_agent import data
    import pandas as pd
    idx = pd.date_range("2025-01-01", periods=300, freq="B")
    s = pd.Series([1000 + i for i in range(300)], index=idx, dtype=float)
    s.attrs["source"] = "test"
    monkeypatch.setattr(data, "fetch_closes", lambda syms, *a, **k: {x: s for x in syms})
    from stock_agent import research, marketdata
    monkeypatch.setattr(research, "news", lambda *a, **k: {"news": [{"title": "测试新闻", "time": "2026-10-30", "url": "https://e.com/n"}]})
    monkeypatch.setattr(research, "announcements", lambda *a, **k: {"announcements": [{"title": "三季报", "time": "2026-10-30", "url": "https://e.com/a"}]})
    monkeypatch.setattr(research, "financials", lambda *a, **k: {"市盈率": 20.1, "date": "2026-10-30"})
    monkeypatch.setattr(marketdata, "quote_check", lambda *a, **k: {"warnings": []})
    return tmp_path


def test_parse_minutes():
    from stock_agent.inquiry import parse_minutes
    assert parse_minutes("茅台还能不能涨 2小时") == ("茅台还能不能涨", 120)
    assert parse_minutes("研究腾讯 90分钟") == ("研究腾讯", 90)
    assert parse_minutes("研究腾讯") == ("研究腾讯", None)


def test_full_run_writes_report_memory_alert(home):
    from stock_agent import inquiry, alerts
    from stock_agent.memory import Memory
    mem = Memory()
    st = inquiry.create("贵州茅台 600519.SS 现在该不该买", 30)
    st = inquiry.run(st["id"], mem)
    assert st["status"] == "done", st.get("error")
    done = [q for q in st["questions"] if q["status"] == "done"]
    assert len(done) >= 4  # 3 个计划问题 + q1 追加的 1 个
    assert any(q["parent"] == "q1" for q in st["questions"])
    assert all(q["strength"] == "强" for q in done)
    assert all("<<<META" not in (q["answer"] or "") for q in done)
    assert st["report"] and st["one_line"] == "质量强但现价安全边际不足"
    assert (home / "data" / "inquiries" / st["id"] / "report.md").exists()
    assert any(st["id"] in e for e in mem.entries("memory"))
    rules = alerts.listing()["rules"]
    assert any(r["note"].startswith(f"课题{st['id']}") and r["value"] == 1200 for r in rules)
    assert st["next_check"]
    # 不被直接转去 Agora 辩论
    assert not any("Agora" in (q["answer"] or "") and "[[debate:" in (q["answer"] or "") for q in done)


def test_budget_and_stop(home):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    st = inquiry.create("腾讯 0700.HK 长期研究", 5)
    ev = threading.Event(); ev.set()
    st = inquiry.run(st["id"], Memory(), stop=ev)
    assert st["status"] == "paused"
    assert all(q["status"] == "todo" for q in st["questions"])
    st = inquiry.run(st["id"], Memory())  # 续跑
    assert st["status"] == "done"


def test_followup_triggers(home):
    from stock_agent import inquiry, alerts
    from stock_agent.memory import Memory
    mem = Memory()
    st = inquiry.run(inquiry.create("贵州茅台 600519.SS", 10)["id"], mem)
    assert inquiry.triggers(st) == []
    rid = st["alerts"][0]
    alerts.add_events([{"time": "2099-01-01 00:00:00", "kind": "price", "symbol": "600519.SS", "title": "茅台 跌破 1200", "rule": rid, "note": f"课题{st['id']}：x"}])
    why = inquiry.triggers(inquiry.load(st["id"]))
    assert why and "失效条件触发" in why[0]
    st = inquiry.followup(st["id"], why, mem)
    assert st["followups"][-1]["verdict"] == "削弱"
    assert any(e.get("kind") == "inquiry" for e in alerts.load()["events"])
    assert any("续研" in e for e in mem.entries("memory"))


def test_agent_tool_and_listing(home):
    from stock_agent import inquiry
    from stock_agent.agent import Agent
    ag = Agent()
    r = ag.run_tool("inquiry_status", {})
    assert r == {"inquiries": []}
    st = inquiry.create("研究美股 AAPL", 10)
    assert inquiry.listing()[0]["id"] == st["id"]
    assert ag.run_tool("inquiry_status", {"id": st["id"]})["goal"] == "研究美股 AAPL"
    ag._no_debate = True
    assert "error" in ag.run_tool("start_inquiry", {"goal": "x"})


def _fast(monkeypatch, inquiry, step):
    monkeypatch.setattr(inquiry, "plan", lambda st, mem, llm: [inquiry._add_q(st, {"text": f"问题{i}"}, 0) for i in range(2)])
    monkeypatch.setattr(inquiry, "reflect", lambda st, llm: None)
    monkeypatch.setattr(inquiry, "synthesize", lambda st, *a, **k: st.update(report="r", one_line="x"))
    monkeypatch.setattr(inquiry, "_announce", lambda *a, **k: None)
    monkeypatch.setattr(inquiry, "research_step", step)


def _ok(st, q, *a, **k):
    q.update(status="done", answer="ok", strength="中", finished="t")


def test_time_left_deepens_instead_of_stopping(home, monkeypatch):
    """计划问题做完、时间还多：追加深挖问题，而不是 20 分钟就收尾。"""
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    calls = []
    def deepen(st, llm):
        calls.append(1)
        return sum(inquiry._add_q(st, {"text": f"深挖{len(calls)}-{i}"}, 1) for i in range(2)) if len(calls) < 3 else 0
    _fast(monkeypatch, inquiry, _ok); monkeypatch.setattr(inquiry, "deepen", deepen)
    st = inquiry.run(inquiry.create("腾讯 0700.HK", 60)["id"], Memory())
    assert st["status"] == "done"
    assert len(calls) == 3 and sum(q["status"] == "done" for q in st["questions"]) == 6


def test_goal_met_ignored_before_most_budget_used(home, monkeypatch):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    def step(st, q, *a, **k):
        _ok(st, q); st["_goal_met"] = True
    _fast(monkeypatch, inquiry, step)
    monkeypatch.setattr(inquiry, "deepen", lambda st, llm: 0)
    st = inquiry.run(inquiry.create("腾讯 0700.HK", 60)["id"], Memory())
    assert sum(q["status"] == "done" for q in st["questions"]) == 2  # 第一题后没有因为「达标」提前停


def test_repeated_errors_pause_not_finish(home, monkeypatch):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    def bad(st, q, *a, **k):
        raise ValueError("模型格式错")
    _fast(monkeypatch, inquiry, bad)
    monkeypatch.setattr(inquiry, "plan", lambda st, mem, llm: [inquiry._add_q(st, {"text": f"问题{i}"}, 0) for i in range(4)])
    st = inquiry.run(inquiry.create("腾讯 0700.HK", 60)["id"], Memory())
    assert st["status"] == "paused" and "连续" in st["error"]
    assert all(q["status"] == "todo" for q in st["questions"])


def test_transient_error_retries(home, monkeypatch):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    n = {"c": 0}
    def flaky(st, q, *a, **k):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("HTTP 429 rate limit")
        _ok(st, q)
    _fast(monkeypatch, inquiry, flaky); monkeypatch.setattr(inquiry, "deepen", lambda st, llm: 0)
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)
    st = inquiry.run(inquiry.create("腾讯 0700.HK", 60)["id"], Memory())
    assert st["status"] == "done" and all(q["status"] == "done" for q in st["questions"])


def test_chat_resume_picks_latest_paused(home, monkeypatch):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    st = inquiry.create("腾讯 0700.HK", 30)
    st["status"] = "paused"; st["used"]["seconds"] = 30 * 60; inquiry.save(st)
    got = []
    monkeypatch.setattr(inquiry.RUNNER, "submit", lambda iid, *a, **k: got.append(iid) or True)
    r = inquiry.for_agent("resume", {}, Memory())
    assert r["id"] == st["id"] and got == [st["id"]]
    assert inquiry.load(st["id"])["budget"]["minutes"] == 60  # 预算用完就自动加 30 分钟
