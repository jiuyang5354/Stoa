"""v0.17：语义记忆（本地嵌入、混合检索、RRF）、长对话压缩与纪要恢复、引用元数据保留、工具轮数收尾。全部离线。"""
import json

import numpy as np
import pytest

from stock_agent import context, vectors


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("EMBEDDINGS", "auto")
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_FALLBACK", "CONTEXT_BUDGET", "AGENT_MAX_ROUNDS", "EMBED_PROVIDER", "EMBED_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    vectors._CACHE.clear()
    vectors._REMOTE_FAIL.update(until=0.0, error="")


def mem():
    from stock_agent.memory import Memory
    return Memory()


def cos(a, b):
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# ---------------------------------------------------------------- 本地嵌入
def test_tokens_cjk_bigrams_and_codes():
    t = vectors.tokens("持有腾讯 0700.HK 200股")
    assert "腾讯" in t and "持有" in t and "0700.hk" in t and "200" in t
    assert "腾" in t  # 向量用单字
    assert "腾" not in vectors.tokens("持有腾讯", unigrams=False)  # BM25 只用双字


def test_local_embedding_synonyms_closer_than_unrelated():
    c = vectors.Concepts(None)
    a = vectors.local_tf("我决定腾讯跌破 455 就止损", c)
    b = vectors.local_tf("我以前定的离场价是多少", c)
    z = vectors.local_tf("英伟达期权隐含波动率", c)
    assert cos(a, b) > cos(a, z)
    assert "§止损" in c.of("离场价") and "§成本" in c.of("多少钱买进的")


def test_rrf_math():
    f = vectors.rrf([[1, 2, 3], [3, 1]], k=60)
    assert f[1] == pytest.approx(1 / 61 + 1 / 62)
    assert f[3] == pytest.approx(1 / 63 + 1 / 61)
    assert f[2] == pytest.approx(1 / 62)
    assert max(f, key=f.get) == 1


def test_bm25_prefers_term_match():
    from collections import Counter
    docs = [Counter(vectors.tokens(t, False)) for t in ("茅台市盈率 21 倍", "腾讯回购", "茅台公告")]
    s = vectors.bm25("茅台市盈率", docs)
    assert s.argmax() == 0 and s[1] == 0


# ---------------------------------------------------------------- 混合检索
def seed(m):
    m.add("user", "持有腾讯 0700.HK 200 股，成本 380 港元")
    m.add("user", "风险偏好保守，不碰杠杆和期权")
    m.add("user", "不喜欢高估值的票，市盈率超过 40 倍的不考虑")
    m.log("web-a", "user", "我决定把腾讯的止损设在 455 港元")
    m.log("web-a", "assistant", "好的，腾讯止损 455。\n\n—— 来源 ——\n[1] Stoa 记忆")
    m.log("web-b", "user", "英伟达 RSI 超买了吗")
    m.record_call("NVDA", "英伟达", "看涨", 180.5, 20, "数据中心资本开支上行，GPU 订单饱满")


def test_hybrid_search_synonym_recall_and_kinds():
    m = mem()
    seed(m)
    top = vectors.search(m, "我之前定的腾讯离场价是多少", limit=3)
    assert "455" in top[0]["text"] and top[0]["via"] in ("语义", "关键词+语义")
    top = vectors.search(m, "我手里那只港股的买入价", kinds=("user",), limit=1)
    assert "380" in top[0]["text"] and top[0]["kind"] == "user"
    top = vectors.search(m, "对太贵的股票什么态度", limit=1)
    assert "高估值" in top[0]["text"]
    top = vectors.search(m, "看好英伟达的理由", kinds=("call",), limit=1)
    assert "订单" in top[0]["text"] and top[0]["meta"]["verdict"] == "未到期"
    assert all(h["kind"] in ("msg", "digest") for h in vectors.search(m, "腾讯", kinds=("msg", "digest")))


def test_incremental_sync_and_deletion():
    m = mem()
    seed(m)
    s1 = vectors.sync(m)
    assert s1["msg"] == 3 and s1["user"] == 3 and s1["call"] == 1
    assert vectors.sync(m) == {}  # 再次同步不重复建
    vectors.sync(m, full=True)
    n0 = vectors.stats(m)["items"]["msg"]
    mid = m.log("web-c", "user", "茅台三季报怎么样")
    s = vectors.sync(m)
    assert s.get("msg") == 1 and vectors.stats(m)["items"]["msg"] == n0 + 1
    hits = vectors.search(m, "茅台三季报", exclude_refs=[mid])
    assert not any(h["ref"] == str(mid) for h in hits)
    m.delete_session("web-c")
    vectors.sync(m)
    assert vectors.stats(m)["items"]["msg"] == n0
    m.remove("user", "风险偏好保守")
    vectors.sync(m)
    assert vectors.stats(m)["items"]["user"] == 2


def test_session_search_is_hybrid_with_fts_fallback():
    m = mem()
    seed(m)
    r = m.search("腾讯离场价")
    assert r and "455" in r[0]["text"] and r[0]["via"]
    assert m.search_fts("止损设在")  # 旧的 FTS 仍在


def test_remote_backend_used_and_fallback(monkeypatch):
    m = mem()
    seed(m)
    rc = {"base": "http://x", "key": "k", "model": "e", "name": "fake:e"}
    calls = {"n": 0}

    def fake_embed(texts, cfg, timeout=30):
        calls["n"] += 1
        out = np.stack([vectors.local_tf(t, vectors.Concepts(m)) for t in texts]).astype(np.float32)
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)

    monkeypatch.setattr(vectors, "remote_config", lambda: rc)
    monkeypatch.setattr(vectors, "remote_embed", fake_embed)
    vectors.sync(m)
    assert vectors.stats(m)["vectors"].get("fake:e") == vectors.stats(m)["vectors"]["local"]
    h = vectors.search(m, "腾讯离场价", limit=1)
    assert h[0]["backend"] == "fake:e" and "455" in h[0]["text"]

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(vectors, "remote_embed", boom)
    h = vectors.search(m, "腾讯离场价", limit=1)
    assert h[0]["backend"] == "local" and vectors.backend_status()["remote_paused"]


def test_embed_presets_and_context():
    from stock_agent.providers import CONTEXT, EMBED_MODELS, LLM
    assert "openai" in EMBED_MODELS and "deepseek" not in EMBED_MODELS
    assert LLM("deepseek", primary=False).context == CONTEXT["deepseek"]
    assert LLM("openai", primary=False).context == 128000


def test_graph_recall_semantic_supplement():
    from stock_agent import graph
    m = mem()
    seed(m)
    r = graph.recall("离场价", m)
    assert r["found"] and any("455" in x for x in r.get("语义相关", []))
    m.add("memory", "腾讯止损线 455 港元，跌破就离场")
    g = graph.build(m)
    sim = [e for e in g["edges"] if e["kind"] == "similar"]
    assert sim and all(e["sim"] >= .1 for e in sim)
    assert any({e["s"], e["t"]} == {"user:0", "note:0"} for e in sim)
    sem = graph.semantic("买入价", m)
    assert sem and sem[0]["node"] == "user:0"


# ---------------------------------------------------------------- token 与工具结果收缩
def test_est_tokens_cjk_and_ascii(monkeypatch):
    monkeypatch.setattr(context, "_ENC", {"enc": None, "tried": True})
    assert context.est_tokens("腾讯控股") == 4
    assert context.est_tokens("a" * 40) == 10
    assert context.est_tokens({"k": "茅台"}) >= 2


def test_budget_from_model_window(monkeypatch):
    class L:
        context = 64000
    assert context.budget(L()) == int(64000 * .6)
    monkeypatch.setenv("CONTEXT_BUDGET", "5000")
    assert context.budget(L()) == 5000


def test_clip_tool_keeps_citation_metadata():
    res = {"symbol": "600519.SS", "title": "2025 年年度报告", "source": "巨潮", "passages": [
        {"page": i, "text": "营业收入 1741.44 亿元，同比增长 15.66%。" * 40, "url": f"https://x/a.pdf#page={i}", "_cite": i} for i in range(1, 21)]}
    s = context.clip_tool(res, 3000)
    j = json.loads(s)
    assert len(s) <= 3000 and j["_truncated"]
    assert j["source"] == "巨潮" and j["symbol"] == "600519.SS"
    kept = [p for p in j["passages"] if isinstance(p, dict)]
    assert kept and all(p["_cite"] and p["url"].startswith("https://x/a.pdf#page=") for p in kept)
    assert context.clip_tool({"a": 1}, 100) == '{"a": 1}'
    lst = context.clip_tool([{"x": "y" * 500, "_cite": 3}] * 30, 800)
    assert json.loads(lst)["items"][0]["_cite"] == 3


def test_facts_keep_numbers_decisions_and_sources():
    rows = [{"role": "user", "text": "我决定腾讯跌破 455 就止损，目标价 620", "time": "2026-10-29 10:00"},
            {"role": "assistant", "text": "腾讯收盘 512.4[1]，涨 2.35%[1]。\n\n—— 来源 ——\n[1] 行情 · 0700.HK · Yahoo Finance · 截至 2026-10-28 · https://finance.yahoo.com/quote/0700.HK"},
            {"role": "assistant", "text": "大概能涨到 999 吧。"}]
    f = context.facts_from(rows)
    assert any("455" in x and "620" in x for x in f)
    assert any("512.4" in x and "Yahoo Finance" in x and "截至 2026-10-28" in x and "http" not in x for x in f)
    assert not any("999" in x for x in f)  # 没出处的推算数字不进纪要


# ---------------------------------------------------------------- 压缩触发与纪要恢复
def make(session=None, budget_extra=700):
    from stock_agent import evals as ev
    from stock_agent.memory import Memory
    fx = ev.load_cases()["fixtures"]
    a = ev.make_agent(fx, Memory(), ev.MockLLM(), [])
    if session:
        from stock_agent.agent import Agent
        a2 = Agent(Memory(), channel="web", session=session)
        a2.llm = ev.MockLLM()
        a = a2
    a.ctx_budget = context.est_tokens(a._sys_base) + budget_extra
    return a


def test_compression_triggers_and_keeps_early_decision():
    a = make()
    a.ask("记一下：我决定腾讯 0700.HK 跌破 455 就止损，目标价 620。")
    for q in ("腾讯现在多少钱？", "茅台最近有什么公告？", "腾讯为什么涨？", "茅台的市盈率多少？"):
        a.ask(q)
    d = a.mem.digest(a.session)
    assert d and "455" in d["text"] and "620" in d["text"] and d["upto_id"] > 0
    assert "## 会话纪要" in a.messages[0]["content"]
    assert context.total(a.messages) <= a.budget() + 2500  # 当前这一轮原文总会保留
    assert not any("跌破 455" in (m.get("content") or "") for m in a.messages[1:])  # 原文已滚出上下文
    ans = a.ask("我最早定的腾讯止损价和目标价是多少？")
    assert "455" in ans and "620" in ans and "（未核实）" not in ans
    assert a.last_meta["archived"] > 0


def test_resume_uses_digest_instead_of_truncation():
    from stock_agent.agent import RESUME_MSGS
    from stock_agent.memory import Memory
    m = Memory()
    sid = "web-long"
    m.log(sid, "user", "我决定茅台 1300 以下加仓")
    m.log(sid, "assistant", "好的，记下了：茅台 1300 以下加仓。")
    for i in range(30):
        m.log(sid, "user", f"第 {i} 个问题：随便聊聊行情")
        m.log(sid, "assistant", f"第 {i} 个回答。")
    a = make(session=sid, budget_extra=100000)
    raw = [x for x in a.messages[1:] if x.get("_id") is not None]
    assert len(raw) <= RESUME_MSGS
    d = m.digest(sid)
    assert d and "1300" in d["text"] and "1300" in a.messages[0]["content"]
    assert raw[0]["role"] == "user"
    # 再次恢复：直接读纪要 + 纪要之后的原文，不重复摘要
    b = make(session=sid, budget_extra=100000)
    assert m.digest(sid)["upto_id"] == d["upto_id"] and "1300" in b.messages[0]["content"]


def test_history_api_has_ids_and_digest_deleted_with_session():
    from stock_agent.memory import Memory
    m = Memory()
    i = m.log("web-x", "user", "hi there")
    assert m.history("web-x")[0]["id"] == i
    m.save_digest("web-x", i, "纪要", ["用户：x"], 1)
    assert m.digest("web-x")["facts"] == ["用户：x"]
    m.delete_session("web-x")
    assert m.digest("web-x") is None


def test_citation_survives_compression():
    """压缩后复述早先的数字：纪要作为来源登记，角标和来源清单照常。"""
    from stock_agent import cite
    a = make()
    for q in ("腾讯现在多少钱？", "茅台最近有什么公告？", "腾讯为什么涨？", "茅台的市盈率多少？", "英伟达 RSI 超买了吗？"):
        a.ask(q)
    assert a.digest and "512.4" in a.digest["text"] and "行情 · 0700.HK" in a.digest["text"]
    ans = a.ask("我们最早查的腾讯收盘价是多少？")
    assert "512.4[" in ans and cite.parse_sources(ans)
    assert any(s["name"] == "Stoa 会话纪要" for s in a.last_meta["sources"])
    assert cite.verify(ans, a.sources)["ok"]


# ---------------------------------------------------------------- 工具轮数与预算收尾
class LoopLLM:
    """一直要求调工具的模型：必须被轮数上限优雅收尾。"""
    ready = True

    def __init__(self):
        self.final_calls = 0

    def __repr__(self):
        return "loop"

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        if tools is None:
            self.final_calls += 1
            return {"role": "assistant", "content": "根据已查到的结果：腾讯收盘 512.4。仅供参考，不构成投资建议。", "tool_calls": None}
        return {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{len(messages)}", "type": "function",
                                                                      "function": {"name": "get_quotes", "arguments": json.dumps({"symbols": ["0700.HK"]})}}]}


def test_tool_rounds_capped_with_graceful_finish(monkeypatch):
    from stock_agent import evals as ev
    from stock_agent.memory import Memory
    monkeypatch.setenv("AGENT_MAX_ROUNDS", "3")
    log = []
    llm = LoopLLM()
    a = ev.make_agent(ev.load_cases()["fixtures"], Memory(), llm, log)
    ans = a.ask("腾讯现在多少钱？")
    # 同一轮里参数相同的只读调用复用结果：模型连调 3 次，工具实际只跑 1 次
    assert sum(x["tool"] == "get_quotes" for x in log) == 1 and llm.final_calls == 1
    assert "512.4" in ans and "工具调用次数过多" not in ans
    assert a.last_meta["verify"]["stopped"].startswith("工具调用已达上限")
    assert not any(m["role"] == "user" and m["content"].startswith("[系统：") for m in a.messages)  # 收尾提示不留在上下文
    assert a.max_rounds() == 3
    monkeypatch.delenv("AGENT_MAX_ROUNDS")
    assert a.max_rounds() == 12


def test_budget_exhaustion_finishes_early():
    from stock_agent import evals as ev
    from stock_agent.memory import Memory
    log = []
    llm = LoopLLM()
    a = ev.make_agent(ev.load_cases()["fixtures"], Memory(), llm, log)
    a.ctx_budget = context.total(a.messages) + 80  # 一轮工具结果就会超
    ans = a.ask("腾讯现在多少钱？")
    assert llm.final_calls == 1 and len(log) <= 2
    assert a.last_meta["verify"]["stopped"] == "上下文预算已用完" and "512.4" in ans
