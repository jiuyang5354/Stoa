"""思路可见：why / got / thoughts 事件与 provider 推理字段解析。"""
import json
from stock_agent import providers
from stock_agent.agent import _digest, _why_line


def test_reasoning_text_shapes():
    f = providers._reasoning_text
    assert f("想一想") == "想一想"
    assert f({"summary": [{"text": "A"}, {"text": "B"}]}) == "A\nB"
    assert f(None) == "" and f([]) == ""


def test_why_and_digest():
    assert _why_line("**先看现价**和估值。然后再说") == "先看现价和估值。"
    assert _why_line("") == ""
    assert "1392.05" in _digest({"symbol": "600519.SS", "price": 1392.05, "change_pct": 2.81})
    assert _digest({"error": "无数据"}).startswith("没取到")
    assert "2 条" in _digest({"items": [{"title": "公告"}, {"title": "b"}], "_cite": 1})
    assert _digest("第一行\n第二行") == "第一行"


def test_openai_reasoning_field(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "答", "reasoning_content": "推理过程"}}], "usage": {}}
    monkeypatch.setattr(providers.requests, "post", lambda *a, **k: R())
    llm = providers.LLM(cfg={"provider": "openai", "model": "x", "api_key": "k", "base_url": "http://x/v1"})
    llm.key, llm.base, llm.proto = "k", "http://x/v1", "openai"
    m = llm._openai([{"role": "user", "content": "hi"}], None, 0.3, 10) if hasattr(llm, "_openai") else llm.chat([{"role": "user", "content": "hi"}])
    assert m["_reasoning"] == "推理过程"


def test_agent_emits_why_think_got(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("STOA_MOCK_LLM", "1")
    from stock_agent import data, marketdata
    import pandas as pd
    s = pd.Series([1300.0 + i for i in range(120)], index=pd.date_range("2026-05-01", periods=120))
    monkeypatch.setattr(data, "fetch_closes", lambda syms, *a, **k: {x: s for x in syms})
    monkeypatch.setattr(marketdata, "quote_check", lambda *a, **k: {"warnings": []})
    from stock_agent.agent import Agent
    ag = Agent(channel="test-reason")
    evs = []
    ag.on_event = evs.append
    ag.ask("贵州茅台 600519.SS 现在多少钱")
    kinds = [e["t"] for e in evs]
    assert "why" in kinds and "think" in kinds
    assert any(e["t"] == "tool_done" and "got" in e for e in evs)
    meta = ag.last_meta
    assert meta["thoughts"] and meta["thoughts"][0]["at"] == 0
    assert all("why" in s and "got" in s for s in meta["steps"])
    # 推理字段不回传给模型
    assert not any("_reasoning" in m for m in ag.messages)


def test_english_mode_adds_language_note(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    from stock_agent.agent import Agent, EN_NOTE
    ag = Agent(channel="test-lang")
    seen = {}
    ag.llm = type("L", (), {"chat": lambda self, m, t=None: seen.setdefault("sys", m[0]["content"]) and {"role": "assistant", "content": "ok", "tool_calls": None}})()
    ag.lang = "en"
    ag._chat([{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}])
    assert seen["sys"].endswith(EN_NOTE)


def test_inquiry_lang_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    from stock_agent import inquiry
    st = inquiry.create("Moutai growth", 10, lang="en")
    assert st["lang"] == "en"
    got = {}
    llm = type("L", (), {"chat": lambda self, m, t=None: got.setdefault("p", m[0]["content"]) and {"content": "{}"}})()
    inquiry._chat(llm, "x", "en")
    assert inquiry.EN_HINT in got["p"]
