"""评测集：题库格式、打分规则、mock 管线。"""
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
