"""v0.23：公司事件/筹码/情绪工具、A股交易规则、内置技能、同轮工具并行与复用。"""
import json
import time

from stock_agent import eventdata as ev, skills


def test_a_limit_rules():
    assert ev.a_limit_pct("600519") == 10 and ev.a_limit_pct("300750") == 20 and ev.a_limit_pct("688981") == 20
    assert ev.a_limit_pct("920982") == 30 and ev.a_limit_pct("600001", "*ST某某") == 5
    r = ev.a_rules("300750.SZ", 200.0)
    assert r["next_limit_up"] == 240.0 and r["next_limit_down"] == 160.0 and r["t_plus"] == 1
    assert ev.a_rules("AAPL", 200.0) is None and ev.a_rules("688981.SS", 50)["lot"] == 200


def test_a_events_flags(monkeypatch):
    rows = {
        "RPT_LIFT_STAGE": [{"FREE_DATE": "2099-01-05 00:00:00", "FREE_SHARES_TYPE": "首发原股东限售股份", "CURRENT_FREE_SHARES": 5000, "FREE_RATIO": 0.035}],
        "RPT_SHARE_HOLDER_INCREASE": [{"NOTICE_DATE": "2099-01-01", "HOLDER_NAME": "某股东", "DIRECTION": "减持", "CHANGE_NUM_SYMBOL": -10}],
        "RPT_CSDC_LIST": [{"TRADE_DATE": "2099-01-01", "PLEDGE_RATIO": 25.5, "PLEDGE_DEAL_NUM": 3, "REPURCHASE_BALANCE": 100}],
    }
    monkeypatch.setattr(ev, "_dc", lambda report, *a, **k: rows.get(report, []))
    out = ev.a_events("300750")
    assert out["symbol"] == "300750.SZ" and not out["warnings"]
    assert any("解禁占总股本 3.5%" in f for f in out["flags"]) and any("减持" in f for f in out["flags"]) and any("质押" in f for f in out["flags"])
    assert ev.a_events("AAPL").get("error")


def test_builtin_skills_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    names = {s["name"]: s for s in skills.list_skills()}
    assert {"stock-checkup", "event-minesweep", "market-recap", "strategy-backtest", "screener", "earnings-season"} <= set(names)
    assert names["stock-checkup"]["builtin"] and "get_corporate_events" in skills.view("stock-checkup")["content"]
    assert "内置" in skills.delete("screener")
    skills.save("screener", "我的筛选", "1. 只看自选")
    assert skills.view("screener")["content"] == "1. 只看自选"
    assert "恢复为内置" in skills.delete("screener") and "股票池" in skills.view("screener")["content"]
    assert "screener*" in skills.index()


def test_parallel_tools_and_reuse(monkeypatch):
    from stock_agent.agent import Agent
    a = Agent.__new__(Agent)
    a._turn_cache, a._steps, a.on_event = {}, [], None
    seen = []

    def slow(name, args):
        seen.append(name)
        time.sleep(.4)
        return {"ok": name}
    a._tool = slow
    calls = [{"id": f"c{i}", "function": {"name": n, "arguments": json.dumps({"symbol": "NVDA"})}}
             for i, n in enumerate(["get_quotes", "get_news", "get_financials", "get_quotes"])]
    t = time.time()
    res = a._run_calls(calls)
    assert time.time() - t < 1.2  # 三个不同的只读调用并行（顺序跑要 1.6 秒）
    assert [r["ok"] for r in res] == ["get_quotes", "get_news", "get_financials", "get_quotes"]
    assert len(a._steps) == 4 and sum(s["cached"] for s in a._steps) <= 1
    res2 = a._run_calls([calls[0]])
    assert a._steps[-1]["cached"] and res2[0] == {"ok": "get_quotes"}
