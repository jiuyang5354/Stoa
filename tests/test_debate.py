"""Agora 多角色辩论：mock 下全流程、各种降级、风控否决、agent 路由、回测模式无未来泄漏。全部离线。"""
import json
import re
import time

import pandas as pd
import pytest

from stock_agent import backtest as bt
from stock_agent import cite
from stock_agent import debate as db
from stock_agent import evals as ev


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_FALLBACK"):
        monkeypatch.delenv(k, raising=False)


FX = ev.load_cases()["fixtures"]


def tools_spy(log):
    def run(name, args):
        log.append(name)
        return ev.fixture_tool(FX, name, args)
    return run


def make(llm=None, symbol="0700.HK", name="腾讯", user=None, budget=None, rounds=1, log=None):
    src = cite.Sources()
    log = [] if log is None else log
    tools = tools_spy(log)
    return db.Debate(llm or db.MockRoleLLM(), symbol, name, "深度分析", tools=lambda n, a: src.add(n, a, tools(n, a)), rounds=rounds,
                     budget=budget or db.Budget(14, 60000, 10), user_entries=user or [], sources=src), log


def test_full_flow_mock():
    d, log = make()
    rec = d.run()
    assert {a["key"]: a["status"] for a in rec["analysts"]} == {"fundamental": "no_data", "technical": "ok", "news": "ok", "macro": "ok"}
    assert rec["debate"] and rec["debate"][0]["bull"]["status"] == "ok" and rec["debate"][0]["bear"]["status"] == "ok"
    assert rec["risk"]["decision"] == "通过"
    v = rec["verdict"]
    assert v["view"] in db.VIEWS and 50 <= v["confidence"] <= 95 and v["invalidation"]
    assert "record_call" in log  # 自动记账
    assert {"get_quotes", "get_financials", "get_news", "get_announcements", "market_overview", "web_search"} <= set(log)
    assert (db.debates_dir() / f"{rec['id']}.json").exists()
    assert db.load(rec["id"])["verdict"]["view"] == v["view"]
    assert rec["budget"]["calls"] <= rec["budget"]["max_calls"]
    a = rec["answer"]
    assert "结论：" in a and "失效条件" in a and "仅供参考" in a and f"[[debate:{rec['id']}]]" in a
    assert cite.verify(a, d.sources)["ok"]  # 最终稿没有无出处的数字


def test_analysts_run_in_parallel():
    llm = db.MockRoleLLM(delay={"technical": .4, "news": .4, "macro": .4, "fundamental": .4})
    d, _ = make(llm, symbol="600519.SS", name="贵州茅台")
    t0 = time.time()
    d.run(save=False)
    assert time.time() - t0 < 1.0 + .4 * 3  # 四位分析师 0.4s 并行（串行至少 1.6s），后面 bull/bear 并行 + risk + pm


def test_analyst_failure_degrades_to_rules():
    llm = db.MockRoleLLM(fail={"technical": RuntimeError("boom")})
    d, _ = make(llm)
    rec = d.run()
    t = next(a for a in rec["analysts"] if a["key"] == "technical")
    assert t["status"] == "degraded" and "boom" in t["error"]
    assert t["stance"] in db.VIEWS and t["evidence"]  # 规则摘要顶上
    assert rec["verdict"]["view"] in db.VIEWS
    assert "技术面（降级）" in rec["answer"]


def test_role_timeout_degrades():
    llm = db.MockRoleLLM(delay={"news": 2.0})
    d, _ = make(llm, budget=db.Budget(14, 60000, 0.5))
    t0 = time.time()
    rec = d.run()
    n = next(a for a in rec["analysts"] if a["key"] == "news")
    assert n["status"] == "degraded" and "TimeoutError" in n["error"]
    assert time.time() - t0 < 2.0
    assert rec["verdict"]["status"] == "ok"


def test_pm_failure_falls_back_to_vote():
    d, log = make(db.MockRoleLLM(fail={"pm": ValueError("bad json")}))
    rec = d.run()
    v = rec["verdict"]
    assert v["status"] == "degraded" and v["view"] == v["vote"]["view"]
    assert "record_call" in log  # 降级照样记账


def test_bull_bear_failure_does_not_kill_debate():
    d, _ = make(db.MockRoleLLM(fail={"bull": RuntimeError("x"), "bear": RuntimeError("y")}), rounds=3)
    rec = d.run()
    assert len(rec["debate"]) == 1  # 双方都失败就不再继续下一轮
    assert rec["verdict"]["view"] in db.VIEWS
    assert "多方：未能发言" in rec["answer"]


def test_multi_round_rebuttal():
    llm = db.MockRoleLLM()
    d, _ = make(llm, rounds=2)
    rec = d.run()
    assert len(rec["debate"]) == 2 and llm.calls.count("bull") == 2 and llm.calls.count("bear") == 2
    assert rec["debate"][1]["bull"]["rebuttal"]  # 第二轮在反驳对方


def test_budget_cap_degrades_not_fails():
    d, _ = make(budget=db.Budget(2, 60000, 10))
    rec = d.run()
    assert rec["budget"]["calls"] == 2 and rec["budget"]["refused"] >= 1
    assert rec["verdict"]["view"] in db.VIEWS  # 照样给结论
    assert rec["degraded"]


def test_token_cap():
    d, _ = make(budget=db.Budget(50, 10, 10))  # 10 token：一次都调不了
    rec = d.run()
    assert rec["budget"]["calls"] == 0
    assert rec["verdict"]["status"] == "degraded"


class AlwaysBull(db.MockRoleLLM):
    def reply(self, role, user):
        if role == "pm":
            return json.dumps({"view": "看多", "confidence": 85, "horizon_days": 20, "summary": "看多", "key_points": [], "invalidation": "跌破 188.43"})
        if role == "risk":
            return json.dumps({"decision": "通过", "max_position_pct": 50, "notes": []})  # 模型想放行，规则不许
        return super().reply(role, user)


def test_risk_veto_overrides_bullish_pm():
    d, _ = make(AlwaysBull(), symbol="NVDA", name="英伟达", user=["[2026-10-02] 持有英伟达 NVDA 300 股，占总仓位 55%"])
    rec = d.run()
    assert rec["risk"]["decision"] == "否决" and rec["risk"]["max_position_pct"] <= 20
    assert rec["verdict"]["view"] == "中性" and "否决" in rec["verdict"]["risk_override"]


def test_risk_downgrade_cuts_confidence():
    d, _ = make(AlwaysBull(), symbol="NVDA", name="英伟达", user=["[2026-10-02] 持有 NVDA，占仓位 30%"])
    rec = d.run()
    assert rec["risk"]["decision"] == "降级" and rec["verdict"]["raw_confidence"] == 85
    assert rec["verdict"]["confidence"] == 68 and rec["verdict"]["view"] == "看多"  # 50+(85−50)×0.5


def test_degrade_conf_shrinks_toward_50_not_floor(monkeypatch):
    monkeypatch.delenv("DEBATE_DEGRADE_SHRINK", raising=False)
    got = [db.degrade_conf(c) for c in (50, 52, 55, 58, 60, 65, 70, 80, 95)]
    assert got == [50, 51, 53, 54, 55, 58, 60, 65, 73]
    assert len(set(db.degrade_conf(c) for c in range(55, 61))) >= 3  # 旧版 55–60 全变 50，现在保留区分度
    assert all(db.degrade_conf(c) <= c for c in range(50, 96))  # 只降不升
    monkeypatch.setenv("DEBATE_DEGRADE_SHRINK", "0")
    assert db.degrade_conf(80) == 50
    assert db.degrade_conf(70, shrink=1.0) == 70


def test_degrade_to_no_conviction_turns_neutral(monkeypatch):
    monkeypatch.setenv("DEBATE_DEGRADE_SHRINK", "0")
    d, _ = make(AlwaysBull(), symbol="NVDA", name="英伟达", user=["[2026-10-02] 持有 NVDA，占仓位 30%"])
    rec = d.run()
    assert rec["verdict"]["view"] == "中性" and "中性" in rec["verdict"]["risk_override"]


def test_holdings_check_conservative_and_stop():
    r = db.holdings_check(["[2026-10-02] 风险偏好保守，不碰杠杆和期权"], "AAPL", set(), {"close": 343.83, "ma60": 318.9, "rsi": 72, "_cite": 3})
    assert r["max_position_pct"] == 10 and r["decision"] == "降级"
    assert "318.9[3]" in r["stop_loss"]


def test_resolve_and_trigger():
    assert db.wants_debate("深度分析一下腾讯") and db.wants_debate("我该不该买英伟达？") and not db.wants_debate("腾讯现在多少钱")
    assert db.resolve_symbol("深度分析一下腾讯") == "0700.HK"
    assert db.resolve_symbol("深度分析 600519.SH") == "600519.SS"
    assert db.resolve_symbol("深度分析 $TSLA") == "TSLA"
    assert db.resolve_symbol("该不该买 9988.hk") == "9988.HK"


# ---------------------------------------------------------------- agent 路由
def agent_with(llm, setup_user=None):
    from stock_agent.memory import Memory
    mem = Memory()
    for e in setup_user or []:
        mem.add("user", e)
    log = []
    return ev.make_agent(FX, mem, llm, log), log


def test_agent_routes_deep_questions_to_debate():
    a, log = agent_with(ev.MockLLM())
    ans = a.ask("深度分析一下腾讯")
    assert "[[debate:" in ans and cite.SOURCES_HEAD in ans
    assert a.last_meta["debate"] and db.load(a.last_meta["debate"])
    assert "record_call" in [x["tool"] for x in log]


def test_agent_quick_question_stays_single():
    a, log = agent_with(ev.MockLLM())
    ans = a.ask("腾讯现在多少钱？")
    assert "[[debate:" not in ans and not a.last_meta["debate"]
    assert [x["tool"] for x in log] == ["get_quotes"]


def test_agent_deep_analysis_tool_path():
    """模型自己调用 deep_analysis 工具（没命中直达正则时）也能跑通，并带上纪要标记。"""
    class ToolCaller(ev.MockLLM):
        def plan(self, q):
            return [("deep_analysis", {"symbol": "0700.HK"})] if "研究" in q else super().plan(q)

        def answer(self, q, res, system):
            r = (res.get("deep_analysis") or [{}])[0]
            return r.get("answer", "无") if r else super().answer(q, res, system)

    a, log = agent_with(ToolCaller())
    ans = a.ask("帮我研究下腾讯")
    assert "deep_analysis" in [x["tool"] for x in log]
    assert "[[debate:" in ans and a.last_meta["debate"]


# ---------------------------------------------------------------- 回测：无未来泄漏
class Spy(db.MockRoleLLM):
    """记录每次调用的全部文本，用来检查有没有混进决策日之后的价格。"""

    def __init__(self):
        super().__init__()
        self.msgs, self.current = [], 0.0

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        self.msgs.append((self.current, " ".join(m["content"] for m in messages)))
        return super().chat(messages, tools, temperature, timeout)


def test_backtest_debate_no_future_leak():
    idx = pd.bdate_range("2025-01-01", periods=220)
    s = pd.Series([100.0 + i for i in range(220)], index=idx)  # 严格单调上升：任何大于当日收盘的价格都只能来自未来
    pit = bt.PointInTime({"X": s})
    spy = Spy()
    agent = db.DebateBTAgent(llm=spy, blind=False, rounds=2, max_calls=10 ** 6, cache=bt.LLMCache(bt.bt_dir() / "t.db"))

    def wrapped(snap):
        spy.current = float(snap.closes.iloc[-1])
        assert snap.closes.index.max() == snap.date
        return agent(snap)

    preds = bt.predict(pit, wrapped, idx[80], idx[200], 10, 5)
    assert preds and len(spy.msgs) >= len(preds) * 5
    for close, text in spy.msgs:
        text = re.sub(r"\[ROLE:\w+\]|\d{4}-\d{2}-\d{2}", "", text)
        nums = [float(x) for x in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text)]
        assert max(nums) <= close + 1e-6, (close, max(nums))
    for d, mx in agent.seen:
        assert mx == d


def test_backtest_debate_disables_other_roles_and_tools():
    idx = pd.bdate_range("2025-01-01", periods=120)
    s = pd.Series([100.0 + (i % 7) for i in range(120)], index=idx)
    snap = bt.Snapshot("X", "美股", idx[100], s.iloc[:101], 5)
    payload = bt.snapshot_payload(snap, True)
    d = db.Debate(db.MockRoleLLM(), "X", "某股票", tools=None, mode="backtest", snapshot_payload=payload, horizon=5)
    with pytest.raises(PermissionError):
        d.tool("get_news", {"symbol": "X"})
    rec = d.run(record=False, save=False)
    st = {a["key"]: a["status"] for a in rec["analysts"]}
    assert st == {"fundamental": "disabled", "technical": "ok", "news": "disabled", "macro": "disabled"}
    assert not list(db.debates_dir().glob("*.json"))  # 回测不落盘、不记账


def test_backtest_run_debate_mock_end_to_end():
    res = bt.run(symbols=["AAPL", "0700.HK"], start="2025-06-01", end="2025-12-01", step=10, horizons=[5],
                 agent="debate", synthetic=True, llm_kind="mock", save=False)
    assert res["params"]["agent"] == "debate" and res["llm"]["debates"] > 0 and res["llm"]["errors"] == 0
    assert res["results"]["agent"]["全部"]["horizons"]["5"]["n"] > 0


def test_answer_says_timing_not_significant():
    d, _ = make(AlwaysBull(), symbol="NVDA", name="英伟达")
    rec = d.run()
    assert "不具统计显著性" in rec["answer"] and "仅供参考" in rec["answer"]
