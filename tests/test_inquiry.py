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
