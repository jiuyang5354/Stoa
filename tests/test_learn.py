"""v0.36：失败按层落地（learn.py）——点踩 → 归层 → 候选回归题 + 候选数据规则 → 待确认 → 批准生效 / 拒绝不再提 / 撤销；
失败账本复发统计；Inquisitio 同一 SAW 两次即停。全部离线。"""
import json
import threading

import pytest

from stock_agent import answercheck as ac


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    from stock_agent import learn
    learn._LAST_MODEL[0] = 0.0
    return tmp_path


MT = {"name": "贵州茅台", "date": "2026-10-28", "close": 1354.0, "chg_pct": -0.84, "chg_5d": -2.31, "chg_20d": -5.47, "ma20": 1398.6,
      "ma60": 1421.3, "rsi": 38.4, "high_52w": 1688.0, "low_52w": 1301.5, "signals": [], "source": "测试"}
FX = {"quotes": {"600519.SS": MT}}
SLANG_REPLY = "贵州茅台现价 1354.0，回调到 52 周低点附近，估值不贵，可以上车。"


class ScriptLLM:
    """先查行情，再按给定原文作答（模拟真实模型换了种说法：「可以上车」）。"""
    ready = True

    def __init__(self, text=SLANG_REPLY, tool="get_quotes", args=None):
        self.text, self.tool, self.args, self.calls = text, tool, args or {"symbols": ["600519.SS"]}, 0

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        if messages[-1]["role"] != "tool" and tools:
            return {"role": "assistant", "content": "先查行情。", "tool_calls": [
                {"id": f"c{self.calls}", "type": "function", "function": {"name": self.tool, "arguments": json.dumps(self.args)}}]}
        return {"role": "assistant", "content": self.text}


def _agent(llm, fx=FX):
    from stock_agent.evals import make_agent
    from stock_agent.memory import Memory
    log = []
    a = make_agent(fx, Memory(), llm, log)
    a.tools = [t for t in a.tools if t["name"] in ("get_quotes", "get_fund_flow")]
    return a


def _approve_all():
    from stock_agent import ledger
    out = []
    for a in ledger.approvals("pending"):
        if a["kind"] == "learn":
            out.append(ledger.approval_decide(a["id"], True))
    return out


def _learn_pending():
    from stock_agent import ledger
    return [a for a in ledger.approvals("pending") if a["kind"] == "learn"]


# ================================================================ 端到端：「可以上车」
def test_slang_bull_end_to_end(home):
    from stock_agent import evals, learn
    a = _agent(ScriptLLM())
    q = "茅台现在能买吗？"
    r1 = a.ask(q)
    assert "可以上车" in r1 and "失效条件" not in r1  # 旧词表认不出「可以上车」，没补失效条件
    assert ac.direction("茅台可以上车。") is None

    res = learn.feedback(a.mem, a.session, "说可以上车却没讲什么时候作废", turn=1)
    assert res["layer"] == "check" and res["method"] == "rules"
    assert len(res["candidates"]) == 2 and any("可以上车" in c for c in res["candidates"])
    pend = _learn_pending()
    assert len(pend) == 2

    # 候选题：在当前（未批准）代码上必须失败
    cand = [c for c in learn.listing()["pending"] if c["kind"] == "case"][0]
    case = learn.candidate(cand["id"])["payload"]
    assert case["failed_on"]["checks"] == ["invalidation"]
    assert learn.run_replay_case(case)["score"] < 1
    assert learn.approved_cases() == []

    # 批准 → 词表生效、题进正式集
    out = _approve_all()
    assert all(o["status"] == "approved" for o in out)
    assert ac.direction("茅台可以上车。") == 1
    assert ac.direction("现在不可以上车。") is None  # 否定不算
    assert learn.run_replay_case(case)["score"] == 1
    assert [c["id"] for c in learn.approved_cases()] == [case["id"]]
    ev = evals.run("mock", only=learn.CATEGORY, save=False)
    assert ev["summary"]["cases"] == 1 and ev["summary"]["total"] == 100.0

    # 同样的回答再来一次：answercheck 现在会补失效条件（价位取本轮行情）
    r2 = _agent(ScriptLLM()).ask(q)
    assert "失效条件" in r2 and "1301.5" in r2

    # 账本：一次失败、已修
    st = learn.stats()
    assert st["layers"]["check"]["n"] == 1 and st["layers"]["check"]["fixed"] == 1 and st["recurred"] == 0

    # 撤销：词表回到原样，题退出正式集
    for c in learn.listing()["active"]:
        assert learn.revoke(c["id"])["ok"]
    assert ac.direction("茅台可以上车。") is None
    assert learn.approved_cases() == []
    assert learn.listing()["failures"][0]["outcome"] == "revoked"


def test_reject_records_and_never_proposes_again(home):
    from stock_agent import ledger, learn
    a = _agent(ScriptLLM())
    a.ask("茅台能买吗")
    learn.feedback(a.mem, a.session, "", turn=1)
    for p in _learn_pending():
        assert ledger.approval_decide(p["id"], False)["status"] == "rejected"
    assert {c["status"] for c in learn.listing()["pending"]} == set()
    assert ac.direction("茅台可以上车。") is None  # 拒绝不生效
    a.ask("茅台能买吗")
    res = learn.feedback(a.mem, a.session, "", turn=2)
    assert res["candidates"] == [] and "拒绝过" in res["note"]
    assert _learn_pending() == []
    assert learn.listing()["failures"][0]["outcome"] == "suppressed"
    assert learn.stats()["layers"]["check"]["rejected"] == 2


def test_recurrence_after_fix_is_counted(home):
    from stock_agent import learn
    a = _agent(ScriptLLM())
    a.ask("茅台能买吗")
    res = learn.feedback(a.mem, a.session, "", turn=1)
    _approve_all()
    # 修过之后同一签名的失败又被点踩（这里直接复用旧现场模拟）
    tr = learn.last_trace(session=a.session)
    tr2 = dict(tr, raw_reply=tr["raw_reply"])
    with learn.override({}):  # 模拟规则没起作用的环境下再次被点踩
        res2 = learn.feedback(a.mem, a.session, "", trace=tr2)
    assert res2["recurred"] is True and res2["candidates"] == []
    st = learn.stats()
    assert st["recurred"] == 1 and st["repeats"][0]["n"] == 2
    assert res["layer"] == res2["layer"] == "check"


def test_candidate_case_must_fail_on_current_code(home):
    """回答里的数字都对得上：「数字不对」生成的 verify_clean 回放题在当前代码上不会失败 → 不算有效，丢弃。"""
    from stock_agent import learn
    a = _agent(ScriptLLM(text="贵州茅台收于 1354.0，20 日均线 1398.6。"))
    a.ask("茅台收盘多少")
    res = learn.feedback(a.mem, a.session, "数字不对", turn=1)
    assert res["layer"] == "check"
    assert "没有失败" in res["note"]
    kinds = [c["kind"] for c in learn.listing()["pending"]]
    assert kinds == ["rule"]  # 只有规则（待办），没有回归题


def test_lure_slang_to_lexicon(home):
    from stock_agent import learn
    q = "我想用借呗的钱买茅台，行吗？"
    assert "leverage" not in ac.lures(q)
    a = _agent(ScriptLLM(text="贵州茅台现价 1354.0，基本面稳健，长期看问题不大，失效条件：跌破 52 周低点 1301.5 则重新评估。"))
    r = a.ask(q)
    assert "杠杆" not in r
    res = learn.feedback(a.mem, a.session, "", turn=1)
    assert res["layer"] == "check" and any("借呗" in c for c in res["candidates"])
    _approve_all()
    assert "leverage" in ac.lures(q)
    r2 = _agent(ScriptLLM(text="贵州茅台现价 1354.0，基本面稳健，长期看问题不大，失效条件：跌破 52 周低点 1301.5 则重新评估。")).ask(q)
    assert "杠杆" in r2 or "保证金" in r2


def test_alias_rule_binds_short_name(home):
    from stock_agent import cite, learn
    a = _agent(ScriptLLM(text="茅子今天大涨 8.4%，收于 1354.0。"))
    a.ask("茅子今天怎么样")
    assert "茅子" not in cite.name_aliases("600519.SS", "贵州茅台")
    res = learn.feedback(a.mem, a.session, "茅子就是贵州茅台，8.4% 不对", turn=1)
    assert res["layer"] == "check" and any("茅子" in c for c in res["candidates"]), res
    _approve_all()
    assert "茅子" in cite.name_aliases("600519.SS", "贵州茅台")


def test_harness_layer_is_todo_only(home):
    from stock_agent import learn
    a = _agent(ScriptLLM(text="腾讯的资金流数据暂时取不到。", tool="get_fund_flow", args={"symbol": "0700.HK"}))
    a.ask("腾讯今天主力资金流入多少")
    res = learn.feedback(a.mem, a.session, "没数据", turn=1)
    assert res["layer"] == "harness"
    c = learn.listing()["pending"]
    assert [x["kind"] for x in c] == ["rule"] and c[0]["title"].startswith("待办")
    _approve_all()
    assert learn.terms() == {}  # 待办入库不改任何检查


def test_debate_and_expression_layers(home):
    from stock_agent import learn
    tr = {"id": None, "session": "web-x", "question": "深度分析茅台", "raw_reply": "倾向：中性", "reply": "倾向：中性", "log": [], "contexts": [],
          "meta": {"debate": "d1"}}
    assert learn.classify(tr, "空方根本没说话，一边倒")["layer"] == "debate"
    tr2 = dict(tr, meta={})
    assert learn.classify(tr2, "太啰嗦了")["layer"] == "expression"
    assert learn.classify(tr2, "你记错了我的成本")["layer"] == "memory"


def test_model_called_once_and_throttled(home):
    from stock_agent import learn

    class J:
        calls = 0

        def chat(self, messages, tools=None, **kw):
            J.calls += 1
            return {"content": '{"layer":"memory","why":"没用上用户说过的成本","phrase":"","target":""}'}
    tr = {"id": None, "session": "web-x", "question": "我的仓位怎么办", "raw_reply": "看情况。", "reply": "看情况。", "log": [], "contexts": [], "meta": {}}
    got = learn.classify(tr, "完全答非所问", J())
    assert got["layer"] == "memory" and got["method"] == "model" and J.calls == 1
    got2 = learn.classify(tr, "还是答非所问", J())  # 间隔内不再调模型
    assert J.calls == 1 and got2["method"] == "default"
    assert learn.classify(tr, "", J())["method"] == "default" and J.calls == 1  # 没有原因不调模型


def test_web_turn_without_trace_and_bad_command(home, monkeypatch):
    """旧对话没有现场也能点踩；Telegram 的 /bad 标记该渠道上一条回答。"""
    from stock_agent import learn, serve
    from stock_agent.memory import Memory
    m = serve.MEM = Memory()
    m.log("web-old", "user", "茅台能买吗")
    m.log("web-old", "assistant", "可以上车。")
    res = learn.feedback(m, "web-old", "", turn=1)
    assert res["layer"] == "check" and res["candidates"]
    learn.record_trace("tg-s", "telegram:42", 1, 2, "腾讯资金流", "取不到。", "取不到。",
                       [{"tool": "get_fund_flow", "args": {"symbol": "0700.HK"}, "result": {"error": "只支持 A股"}}], [], {})
    monkeypatch.setattr(serve, "load_cfg", lambda: {})
    txt = serve.chat("telegram:42", "/bad 没查到")
    assert txt.startswith("已记下") and "数据/工具缺失" in txt
    assert "还没有" in serve.chat("telegram:999", "/bad")


def test_answercheck_unchanged_without_learn_db(home):
    from stock_agent import learn
    assert learn.terms() == {}
    assert not (home / "data" / "learn.db").exists()
    assert ac.direction("建议买入。") == 1


# ================================================================ Inquisitio：同一 SAW 两次即停
def _fast(monkeypatch, inquiry, step, n=3):
    monkeypatch.setattr(inquiry, "plan", lambda st, mem, llm: [inquiry._add_q(st, {"text": f"问题{i}"}, 0) for i in range(n)])
    monkeypatch.setattr(inquiry, "reflect", lambda st, llm: None)
    monkeypatch.setattr(inquiry, "synthesize", lambda st, *a, **k: st.update(report="r", one_line="x"))
    monkeypatch.setattr(inquiry, "research_step", step)
    monkeypatch.setattr(inquiry, "deepen", lambda st, llm: 0)


def test_same_saw_twice_stops_and_reports(home, monkeypatch):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    monkeypatch.setenv("STOA_MOCK_LLM", "1")
    n, waits, told = {"c": 0}, [], []
    def bad(st, q, *a, **k):
        n["c"] += 1
        raise RuntimeError(f"HTTP 429 rate limit (req {n['c']}a7f3c9e1d2)")
    _fast(monkeypatch, inquiry, bad)
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: waits.append(t) or False)
    monkeypatch.setattr(inquiry, "_announce", lambda st, head, text, push=True: told.append((head, text)))
    st = inquiry.run(inquiry.create("腾讯 0700.HK", 60)["id"], Memory())
    assert n["c"] == 2 and len(waits) == 1  # 第二次看到同一个 SAW 就停，不再试第三次
    assert st["status"] == "paused" and "SAW" in st["error"] and "EXPECTED" in st["error"] and "DEAD ENDS" in st["error"]
    fb = st["stalled"]
    assert fb["saw"] == st["feedback"][0]["saw"] and len(fb["dead_ends"]) == 2
    assert told and told[0][0] == "研究暂停"
    assert all(q["status"] == "todo" for q in st["questions"])


def test_different_saw_keeps_retrying_and_feeds_back(home, monkeypatch):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    monkeypatch.setenv("STOA_MOCK_LLM", "1")
    seen = []
    errs = [RuntimeError("HTTP 429 rate limit"), TimeoutError("read timed out")]
    def flaky(st, q, *a, **k):
        seen.append(q.get("feedback"))
        if errs:
            raise errs.pop(0)
        q.update(status="done", answer="ok", strength="中", finished="t")
    _fast(monkeypatch, inquiry, flaky, n=1)
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)
    monkeypatch.setattr(inquiry, "_announce", lambda *a, **k: None)
    st = inquiry.run(inquiry.create("腾讯 0700.HK", 60)["id"], Memory())
    assert st["status"] == "done"
    assert seen[0] is None and seen[1]["saw"].startswith("RuntimeError") and seen[2]["saw"].startswith("TimeoutError")
    assert "EXPECTED" in inquiry._feedback_text(seen[2]) and len(seen[2]["dead_ends"]) == 2
    assert st["last_saw"] is None and "feedback" not in st["questions"][0]
