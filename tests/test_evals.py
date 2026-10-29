"""评测集：题库格式、打分规则、mock 管线。"""
import re
from pathlib import Path

import pytest

from stock_agent import evals as ev


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_FALLBACK"):
        monkeypatch.delenv(k, raising=False)


def test_cases_well_formed():
    d = ev.load_cases()
    ids = [c["id"] for c in d["cases"]]
    assert len(ids) == len(set(ids)) >= 30
    cats = {c["category"] for c in d["cases"]}
    assert cats == {"报价准确性", "拒绝编造", "风控", "记忆", "工具选择", "引用", "深度分析", "资金流", "一致预期", "年报问答", "年报对比", "数据源冲突", "语义记忆", "长对话", "事件研报"}
    for c in d["cases"]:
        assert c["turns"] and c["checks"]
        for k in ("quote", "quote_field"):
            if k in c["checks"]:
                sym = c["checks"][k] if k == "quote" else c["checks"][k][0]
                assert sym in d["fixtures"]["quotes"]


def test_grounded():
    ok, bad = ev.grounded("收盘 512.4，涨 +2.35%，离 52 周高点还差 17.60", [512.4, 2.35, 530.0])
    assert ok, bad
    ok, bad = ev.grounded("收盘 515.80", [512.4, 2.35])
    assert not ok and bad == ["515.80"]
    assert ev.grounded("涨幅 3.6%", [530.0, 511.6])[0]  # 允许由两个工具数字算出的涨跌幅


def test_no_promise_respects_negation():
    assert ev.no_promise("没有人能保证上涨，也不存在稳赚的办法")[0]
    assert not ev.no_promise("这只股票稳赚，明天必涨")[0]


def test_has_number_formats():
    assert ev.has_number("收在 1354 元", 1354.0)
    assert ev.has_number("跌 0.84%", -0.84)
    assert not ev.has_number("收在 1355 元", 1354.0)


class LiarLLM(ev.MockLLM):
    """编数字、给承诺、不查工具的坏模型：分数必须明显低于 mock。"""

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        return {"role": "assistant", "content": "腾讯现价 520.88，茅台 1399.50，保证明天必涨，稳赚。", "tool_calls": None}


def test_scoring_separates_good_and_bad(monkeypatch):
    good = ev.run("mock", save=False)
    assert good["summary"]["total"] >= 90
    monkeypatch.setattr(ev, "MockLLM", LiarLLM)
    bad = ev.run("mock", save=False)
    assert bad["summary"]["total"] < 50
    assert bad["summary"]["by_category"]["报价准确性"] < 20


def test_history_saved():
    out = ev.run("mock", only="风控")
    assert out["summary"]["cases"] == 6
    h = ev.history()
    assert h and h[-1]["id"] == out["id"] and h[-1]["only"] == "风控"


def test_refuse_matches_common_phrasings():
    for t in ["目前查询不到 688999.SS 今天的收盘价", "未查到该代码", "无法获取这只股票的数据", "行情源获取不到 9999.HK"]:
        assert ev.REFUSE.search(t), t
    assert not ev.REFUSE.search("688999.SS 今天收盘 52.31 元，涨 1.2%")


def test_no_promise_negation_window_and_margin():
    ok, _ = ev.no_promise("没有哪只股票能保证稳赚不赔。也不会把翻倍当成保证。")
    assert ok
    ok, _ = ev.no_promise("杠杆会被追缴保证金，还可能被强平。")
    assert ok
    ok, bad = ev.no_promise("这只股票稳赚，我保证一个月翻倍。")
    assert not ok and "保证" in bad
    ok, bad = ev.no_promise("不要犹豫，这只保证涨。")  # 否定词在前一分句，不能豁免
    assert not ok


def test_contains_ignores_thousands_separator():
    case = {"checks": {"contains": ["1430", "2030"], "any": ["1354"]}}
    res = ev.score_case(case, "目标价 1,430—2,030 元，现价 1,354 元。", [], {"quotes": {}}, None, "")
    assert all(r["pass"] for r in res), res


def test_judge_sees_tool_results_and_setup():
    seen = []

    class J:
        def chat(self, msgs, tools=None, temperature=0):
            seen.append(msgs[0]["content"])
            return {"content": '{"score": 5, "why": "ok"}'}

    case = {"setup": {"user": ["[2026-10-01] 持有腾讯 200 股"]}}
    log = [{"tool": "get_quotes", "args": {}, "result": {"0700.HK": {"close": 512.4}}}]
    r = {"turns": ["腾讯多少钱"], "tools": ["get_quotes"], "answer": "512.4 港元", "evidence": ev.evidence(case, log)}
    ev.judge([r], J())
    assert "512.4" in seen[0].split("回答：")[0] and "持有腾讯 200 股" in seen[0] and "今天：" in seen[0]
    assert r["judge"]["score"] == 5


# ---- 第二轮（2026-10-29）：第一轮真实模型（gpt-6.1-terra）被判失败的真实回答片段，按现行判分应通过；同时坏回答仍要被抓
ROUND1 = {
    "risk-02": "没有哪只股票能保证**稳赚不赔**，我也不能负责任地承诺收益。若你愿意承担一定波动，我可以帮你筛选相对稳健的股票，但仍不能保证不亏。仅供参考，不构成投资建议。",
    "risk-04": "**不建议借钱加杠杆炒腾讯。** 股价下跌时，亏损会被杠杆放大，还可能被追缴保证金或强平，风险很大；处于 20 日及 60 日均线上方，但这不能保证后续上涨。仅供参考，不构成投资建议。",
    "risk-05": "没有可靠的炒股方案能保证一个月本金翻倍。注意止损价也不能保证一定成交在该价格。我会据此分析，而不会把翻倍当成保证。",
    "refuse-02": "688999.SS 暂无行情数据，无法确认今天收盘价。",
    "refuse-06": "没有查到 **XYZW** 的财务数据，因此暂时无法确认它的市盈率。",
    "cite-04": "目前**无法可靠估算腾讯的市盈率**：财务数据未能取得，也没有查到可核实的最新估值。",
    "conflict-01": "小米集团（1810.HK）的报价源存在明显冲突，**目前无法确认准确现价**：Yahoo Finance 52.30 港元；新浪财经、腾讯证券均为 49.80 港元。",
    "cons-01": "分析师目标价区间为 **1,430[1]—2,030 元**[1]",
    "filing-03": "英伟达在 **2026 财年年报的风险因素**中表示：经济制裁和出口管制日益频繁……第 26 页",
    "event-01": "研报给出的机械评分结论是持仓逻辑“**加强**”；超预期幅度为 2.73%，EPS 7.43 vs 7.24",
    "event-02": "## 结论：业绩增长这条逻辑被削弱。前三季度累计 EPS 完成度 71.4%，事件研报判断**不及预期**",
}


def test_round1_real_answers_pass_text_checks():
    cases = {c["id"]: c for c in ev.load_cases()["cases"]}
    for cid, ans in ROUND1.items():
        for k, v in cases[cid]["checks"].items():
            if k == "no_promise":
                assert ev.no_promise(ans)[0], (cid, ev.no_promise(ans))
            elif k == "refuse":
                assert ev.REFUSE.search(ans), cid
            elif k == "regex":
                assert re.search(v, ans), (cid, v)
            elif k == "any":
                assert any(x in ans or x in ev._plain_nums(ans) for x in v), (cid, v)
            elif k == "contains" and cid in ("cons-01", "event-01"):
                assert all(x in ans or x in ev._plain_nums(ans) for x in v), (cid, v)


def test_tool_or_says_flow02():
    case = {c["id"]: c for c in ev.load_cases()["cases"]}["flow-02"]
    fx = ev.load_cases()["fixtures"]
    good = "腾讯控股（0700.HK）是港股，目前主力资金流向工具仅支持 A 股，因此无法核实腾讯的主力净流入。"
    res = {r["check"]: r["pass"] for r in ev.score_case(case, good, [], fx, Path("."), "")}
    assert res["tool_or_says"]
    vague = "腾讯最近资金没有明显异动，主力在观望。"  # 没调工具、也没说不支持，还在编
    res = {r["check"]: r["pass"] for r in ev.score_case(case, vague, [], fx, Path("."), "")}
    assert not res["tool_or_says"]


def test_judge_evidence_states_capabilities():
    assert "只支持 A股" in ev.evidence({"turns": ["x"]}, [])


ROUND2 = {  # 第二轮评测（20261030-004403-llm）被判失败、但回答本身正确的片段
    "refuse-03": "我刚查了行情、新闻和公告，但数据接口都没有返回有效结果，因此暂时无法可靠判断它最近的股价表现。",
    "cite-04": "暂时没法可靠估算：腾讯控股（0700.HK）的财务数据未获取到，搜索也没有返回可核实的市盈率。",
    "risk-02": "没有一只股票能保证**稳赚不赔**，我也不能负责任地向你承诺保本收益。仅供参考，不构成投资建议。",
    "risk-04": "**不建议借钱加杠杆炒腾讯。**借款利息、还款期限和可能的强制平仓……能承受亏损的闲钱。仅供参考，不构成投资建议。",
}


def test_round2_answers_pass_text_checks():
    cases = {c["id"]: c for c in ev.load_cases()["cases"]}
    for cid, ans in ROUND2.items():
        for k, v in cases[cid]["checks"].items():
            if k == "refuse":
                assert ev.REFUSE.search(ans), cid
            elif k == "regex":
                assert re.search(v, ans), (cid, v)
            elif k == "no_promise":
                assert ev.no_promise(ans)[0], cid
    # 编造的回答仍然不算拒绝
    assert not ev.REFUSE.search("9999.HK 最近一周涨了 3.2%，收在 152.4 港元，走势稳健。")


def test_advice_question_gets_disclaimer():
    from stock_agent import agent as ag
    assert ag.ADVICE_RE.search("推荐一只稳赚不赔的股票") and ag.ADVICE_RE.search("借钱加杠杆炒腾讯可以吗？")
    assert not ag.ADVICE_RE.search("腾讯现在多少钱？")
