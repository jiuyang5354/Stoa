"""v0.38.1 回归题：盲评 v2 与同模型评测里暴露的问题。14 条在 v0.38.0（3b23f0b）上失败；另 3 条是防误伤的对照（编造数字照样抓、你自己的记录照样引用、整轮真到点照样按上限收尾）。

1. 单次模型请求超时（默认 120 秒，含重试）被当成整轮 900 秒超时，回答只剩「本轮到了 900 秒上限」（Vals/FSC 8 题，盲评 Q4/Q6/Q16）。
2. calc 算出的合计被标「未核实」（盲评 Q2：腾讯五日南向合计 -3.54 亿）。
3. 出处挂错：新浪的行业板块数据挂到东财涨停页（Q7）；网页抓取日期写成「截至」并被当成交易日（Q7）；
   「你的记忆」被挂成债券例题现金流的来源（Q30）；按收盘价算的 52 周低点被写成「52 周低点」（Q18）；
   没有任何回测却写「择时判断经回测不具统计显著性」（Q22）。
"""
import json
import threading
import time

import pytest

from stock_agent import cite


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_RETRY_BASE", "0.01")
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_FALLBACK", "AGENT_TURN_TIMEOUT", "STOA_MOCK_LLM", "LLM_TIMEOUT_RETRIES"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


# ---------------------------------------------------------------- 1. 单次请求超时 ≠ 整轮超时
class FakeLLM:
    def __init__(self, name):
        self.name, self.ready, self.model = name, True, "m"

    def __repr__(self):
        return self.name


def _router(*names):
    from stock_agent.providers import Router
    r = Router.__new__(Router)
    r.chain = [FakeLLM(n) for n in names]
    return r


def _scripted(monkeypatch, script):
    """controlled_chat 替身：按顺序执行 script 里的动作（'timeout' 抛 TimeoutError，dict 原样返回）。"""
    from stock_agent import providers
    calls = []

    def fake(llm, messages, tools, temperature, timeout, stop, on_event=None):
        calls.append((llm.name, round(timeout)))
        act = script[min(len(calls) - 1, len(script) - 1)]
        if act == "timeout":
            raise TimeoutError("模型请求超时（含重试与备用模型等待）")
        return act(messages, tools) if callable(act) else act
    monkeypatch.setattr(providers, "controlled_chat", fake)
    return calls


def test_router_request_timeout_switches_to_fallback(monkeypatch):
    calls = _scripted(monkeypatch, ["timeout", {"role": "assistant", "content": "ok"}])
    r = _router("primary", "backup")
    r.deadline = time.monotonic() + 900
    out = r.chat([{"role": "user", "content": "hi"}], None, stop=threading.Event())
    assert out["content"] == "ok"
    assert [c[0] for c in calls] == ["primary", "backup"]


def test_router_turn_deadline_is_turn_timeout(monkeypatch):
    from stock_agent.providers import TurnTimeout
    _scripted(monkeypatch, ["timeout"])
    r = _router("primary")
    r.deadline = time.monotonic() - 1  # 整轮已经到点
    with pytest.raises(TurnTimeout):
        r.chat([{"role": "user", "content": "hi"}], None, stop=threading.Event())


Q = {"600519.SS": {"name": "贵州茅台", "date": "2026-10-30", "close": 1354.0, "chg_pct": -0.84, "ma20": 1398.6, "source": "测试"}}


def _agent(router):
    from stock_agent.agent import Agent
    from stock_agent.memory import Memory

    class A(Agent):
        def run_tool(self, name, args):
            return dict(Q)
    a = A(Memory(), channel="web", session="web-v0381")
    a.llm = router
    a.tools = [t for t in a.tools if t["name"] == "get_quotes"]
    return a


def _tool_then_answer(messages, tools):
    if tools and messages[-1]["role"] != "tool":
        return {"role": "assistant", "content": "查行情。", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_quotes", "arguments": json.dumps({"symbols": ["600519.SS"]})}}]}
    return {"role": "assistant", "content": "贵州茅台最近收盘 1354.0 元，跌 0.84%。"}


def test_single_request_timeout_retries_instead_of_ending_turn(monkeypatch):
    """第一次请求 120 秒超时：整轮还有 900 秒，应当重试并给出完整回答（v0.38.0 直接回「本轮到了 900 秒上限」）。"""
    calls = _scripted(monkeypatch, ["timeout", _tool_then_answer, _tool_then_answer])
    a = _agent(_router("primary"))
    r = a.ask("茅台现在多少")
    assert "秒上限" not in r and "未完成" not in r
    assert "1354.0" in r
    assert len(calls) == 3 and not a.last_meta.get("timeout")


def test_timeout_mid_turn_keeps_going(monkeypatch):
    """拿到工具结果后的那次请求超时：重试后继续作答，不丢已拿到的结果。"""
    _scripted(monkeypatch, [_tool_then_answer, "timeout", _tool_then_answer])
    a = _agent(_router("primary"))
    r = a.ask("茅台现在多少")
    assert "1354.0" in r and "秒上限" not in r


def test_repeated_request_timeouts_report_real_cause_and_partial_results(monkeypatch):
    """请求一直超时、重试用完：用已有结果收尾，写真实原因和用时，不冒充「900 秒上限」，也不空答。"""
    monkeypatch.setenv("LLM_TIMEOUT_RETRIES", "2")
    calls = _scripted(monkeypatch, [_tool_then_answer, "timeout"])
    a = _agent(_router("primary"))
    r = a.ask("茅台现在多少")
    assert "900 秒上限" not in r
    assert "连续 3 次超时" in r and "本轮用时" in r
    assert "1354" in r and "查行情" in r  # 已拿到的工具结果、模型的中间说明都在
    assert len(calls) == 4  # 1 次正常 + 1 次超时 + 2 次重试
    assert a.last_meta["timeout"] and a.last_meta["partial"]


def test_real_turn_deadline_still_finishes_with_results(monkeypatch):
    """整轮硬上限真的到了才按上限收尾（用已拿到的结果，不空答）。"""
    from stock_agent import providers
    r = _router("primary")
    state = {"n": 0}

    def fake(llm, messages, tools, temperature, timeout, stop, on_event=None):
        state["n"] += 1
        if state["n"] == 1:
            return _tool_then_answer(messages, tools)
        r.deadline = time.monotonic() - 1  # 时钟走到整轮末尾
        raise TimeoutError("deadline")
    monkeypatch.setattr(providers, "controlled_chat", fake)
    a = _agent(r)
    out = a.ask("茅台现在多少")
    assert "900 秒上限" in out and "1354" in out and "连续" not in out
    assert state["n"] == 2  # 到了整轮上限不再重试


# ---------------------------------------------------------------- 2. calc 合计不再误标未核实（盲评 Q2）
def _q2_sources():
    s = cite.Sources()
    s.add("get_quotes", {"symbols": ["0700.HK"]}, {"0700.HK": {"name": "腾讯控股", "date": "2026-10-30", "close": 383.6, "chg_pct": 1.59,
                                                              "ma20": 406.61, "source": "腾讯证券"}})
    s.add("get_southbound", {"symbol": "0700.HK"}, {"symbol": "0700.HK", "source": "东方财富数据中心（港股通）", "as_of": "2026-10-30",
                                                    "unit": "亿港元", "daily": [{"date": "2026-10-26", "net_buy_yi": -1.75}, {"date": "2026-10-27", "net_buy_yi": -1.2},
                                                                              {"date": "2026-10-28", "net_buy_yi": 0.02}, {"date": "2026-10-29", "net_buy_yi": 1.46},
                                                                              {"date": "2026-10-30", "net_buy_yi": -2.07}]})
    s.add("calc", {"expressions": {"total": "a+b+c+d+e"}}, {"expressions": {"total": "a+b+c+d+e"},
                                                          "inputs": {"a": -1.75, "b": -1.2, "c": 0.02, "d": 1.46, "e": -2.07}, "results": {"total": -3.54}})
    return s


def test_calc_total_in_symbol_sentence_is_verified():
    s = _q2_sources()
    t = "腾讯五日南向净买入合计 -3.54 亿港元，即净卖出。"
    v = cite.verify(t, s)
    assert v["ok"], v["unverified"]
    out, _ = cite.flag(t, s)
    assert "未核实" not in out


def test_symbol_sentence_still_catches_made_up_number():
    s = _q2_sources()
    v = cite.verify("腾讯五日南向净买入合计 -5.12 亿港元。", s)
    assert v["unverified"] == ["-5.12 亿"]


# ---------------------------------------------------------------- 3a. 板块数据挂到实际取数的源（盲评 Q7）
MOOD = {"source": "东方财富 · 涨停板行情中心 + 新浪财经板块资金流", "url": "https://quote.eastmoney.com/ztb/", "date": "2026-10-30", "as_of": "2026-10-30",
        "limit_up": 61, "broken": 20, "limit_down": 5,
        "industry_inflow": [{"name": "酿酒行业", "chg_pct": 3.49, "main_net_yi": 51.38}],
        "industry_outflow": [{"name": "电子信息", "chg_pct": -1.2, "main_net_yi": -71.33}],
        "industry_top_gainers": [{"name": "酿酒行业", "chg_pct": 3.49, "main_net_yi": 51.38}],
        "industry_flow_source": "新浪财经 · 净流入（流入−流出成交额，口径不同于东方财富主力净流入）",
        "sources": [{"name": "东方财富 · 涨停/炸板/跌停池", "url": "https://quote.eastmoney.com/ztb/", "as_of": "2026-10-30"},
                    {"name": "新浪财经 · 行业板块资金流（净流入）", "url": "https://vip.stock.finance.sina.com.cn/moneyflow/#sczjlx", "as_of": "2026-10-30"}]}


def test_sector_numbers_cite_the_sector_source():
    s = cite.Sources()
    s.add("get_market_mood", {}, json.loads(json.dumps(MOOD)))
    out, used = cite.finalize("酿酒行业涨 3.49%，净流入 51.38 亿元；电子信息净流出 71.33 亿元。今天涨停 61 家。", s)
    by_n = {u["n"]: u for u in used}
    import re
    n_sector = int(re.search(r"51\.38 亿元\[(\d+)\]", out).group(1))
    n_zt = int(re.search(r"61 家\[(\d+)\]|61\[(\d+)\]", out).group(1) or 0) if re.search(r"61 家\[(\d+)\]", out) else None
    assert "sina" in by_n[n_sector]["url"] and "ztb" not in by_n[n_sector]["url"]
    assert "新浪" in by_n[n_sector]["name"]
    if n_zt:
        assert "ztb" in by_n[n_zt]["url"]


# ---------------------------------------------------------------- 3b. 抓取日期不是交易日（Q7）
def test_fetched_page_says_fetched_not_as_of(monkeypatch):
    monkeypatch.setattr(cite, "today", lambda: "2026-11-01")
    s = cite.Sources()
    s.add("fetch_url", {"url": "https://data.10jqka.com.cn/funds/hyzjl/"}, {"title": "行业资金流向排行", "text": "电子信息 净额 -71.33 亿"})
    out, used = cite.finalize("同花顺行业资金榜显示电子信息净额 -71.33 亿。", s)
    assert "抓取于 2026-11-01" in out and "截至 2026-11-01" not in out
    assert used[0]["fetched"]
    s.add("get_financials", {"symbol": "000300.SS"}, {"symbol": "000300.SS", "pe": 13.4})  # 财务数据没给日期：同样是抓取日
    out2, _ = cite.finalize("沪深300 市盈率 13.4 倍。", s)
    assert "抓取于 2026-11-01" in out2 and "截至 2026-11-01" not in out2


def test_fetch_date_written_as_latest_trading_day_is_flagged(monkeypatch):
    monkeypatch.setattr(cite, "today", lambda: "2026-11-01")
    s = cite.Sources()
    s.add("get_quotes", {}, {"0700.HK": {"name": "腾讯控股", "date": "2026-10-30", "close": 383.6, "source": "腾讯证券"}})
    s.add("fetch_url", {"url": "https://example.com/x"}, {"title": "x", "text": "y 1.0"})
    v = cite.verify("最新交易日 2026-11-01，收盘 383.6 港元。", s)
    assert "2026-11-01" in v["unverified"], v
    ok = cite.verify("今天是 2026年11月1日（周日），A股休市，最近交易日为 2026-10-30，收盘 383.6 港元。", s)
    assert ok["ok"], ok["unverified"]


# ---------------------------------------------------------------- 3c. 记忆不是例题的出处（Q30）
def test_memory_is_not_source_for_textbook_numbers():
    s = cite.Sources()
    s.add_context("[2026-10-20] 看过一只票息 5% 的债，价格 105 元附近；持仓 100 股", "你在 Stoa 的记忆与判断记录（非市场数据）", "memory")
    s.add("calc", {"expressions": {"price": "c/(1+y)+(F+c)/(1+y)**2"}},
          {"expressions": {"price": "c/(1+y)+(F+c)/(1+y)**2"}, "inputs": {"F": 100, "c": 5, "y": 0.05}, "results": {"price": 100.0}})
    out, used = cite.finalize("以下为教学假设。面值 100 元，票息 5 元，第二年收到本金和利息共 105 元，价格为 100 元。", s)
    assert not any("记忆" in u["name"] for u in used), [u["name"] for u in used]
    assert cite.verify(out, s)["ok"]


def test_memory_still_cites_users_own_records():
    s = cite.Sources()
    s.add_context("[2026-10-20] 持有腾讯 0700.HK 200 股，成本 381.5 港元", "你在 Stoa 的记忆与判断记录（非市场数据）", "memory")
    out, used = cite.finalize("你记录的持仓成本是 381.5 港元。", s)
    assert any("记忆" in u["name"] for u in used)


# ---------------------------------------------------------------- 3d. 52 周低点按收盘价算，要写明（Q18）
def test_closing_52w_low_is_not_an_intraday_low():
    s = cite.Sources()
    s.add("get_quotes", {}, {"0700.HK": {"name": "腾讯控股", "date": "2026-10-30", "close": 383.6, "low_52w": 377.6, "high_52w": 683.0,
                                         "ma20": 406.61, "source": "腾讯证券"}})
    out, _ = cite.finalize("腾讯的 52 周低点 377.6 港元，52 周高点 683.0 港元。", s)
    assert "52 周收盘低点 377.6" in out and "52 周收盘高点 683.0" in out
    out2, _ = cite.flag("若跌破 52 周低点 370.0 再看。", s)  # 不是收盘口径的值：不改写
    assert "52 周低点 370.0" in out2


def test_invalidation_line_says_closing_low():
    from stock_agent import answercheck
    line = answercheck.invalidation_line(1, {"close": 383.6, "low_52w": 377.6}, "0700.HK")
    assert "52 周收盘低点 377.6" in line


# ---------------------------------------------------------------- 3e. 没跑回测不说「经回测」（Q22）
def test_no_backtest_claim_without_backtest():
    from stock_agent.agent import post_check
    s = cite.Sources()
    s.add("fetch_url", {"url": "https://www.kitco.com/x"}, {"title": "Gold falls", "text": "gold 3879.90"})
    reply = "现在不建议追。\n\n仅供参考，不构成投资建议。择时判断经回测不具统计显著性。"
    out, _, _ = post_check("黄金现在还能不能追？", reply, s)
    assert "经回测" not in out and "仅供参考" in out


def test_backtest_claim_kept_when_backtest_ran():
    s = cite.Sources()
    s.add("trade_replay", {"symbol": "GC=F"}, {"symbol": "GC=F", "paths": 10000, "p_target_first": 0.41})
    out, found = cite.unsupported_claims("择时判断经回测不具统计显著性。", s)
    assert "经回测" in out and not found
