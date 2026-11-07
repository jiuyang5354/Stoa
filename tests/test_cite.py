"""数字强制引用：来源登记、核对器抓编造数字、不误伤换算/推导、修正一次、仍不改就标未核实、角标与来源清单。"""
import re

import pytest

from stock_agent import cite
from stock_agent import evals as ev


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_FALLBACK"):
        monkeypatch.delenv(k, raising=False)


def src():
    s = cite.Sources()
    s.add_context("[2026-10-01] 持有腾讯 0700.HK 200 股，成本 380 港元", "Stoa 记忆", "memory")
    r = s.add("get_quotes", {"symbols": ["0700.HK"]}, {"0700.HK": {"date": "2026-10-28", "close": 512.4, "chg_pct": 2.35, "high_52w": 530.0,
                                                                  "ma20": 498.21, "source": "Yahoo Finance", "signals": ["上穿 20 日线"]}})
    s.add("get_financials", {"symbol": "600519.SS"}, {"symbol": "600519.SS", "pe": 21.3, "roe": 0.335, "market_cap": "1.70万亿元"})
    return s, r


def test_tool_results_carry_source_and_timestamp():
    s, r = src()
    assert r["0700.HK"]["_cite"] == 2
    it = s.get(2)
    assert it["as_of"] == "2026-10-28" and "Yahoo Finance" in it["name"] and it["url"].endswith("0700.HK")
    assert s.get(3)["url"] == "https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code=SH600519#/cwfx"  # v0.37：财务出处指向 F10 财务分析页


def test_catches_fabricated_numbers():
    s, _ = src()
    v = cite.verify("腾讯收盘 512.4，市盈率 18.6 倍，目标价 620 港元，茅台 ROE 41.2%。", s)
    assert set(v["unverified"]) == {"18.6", "620", "41.2%"}


def test_accepts_rounding_units_and_derived():
    s, _ = src()
    t = ("收盘 512.40 港元，约 512 港元，涨 2.4%，离 52 周高点还差 17.6，较 20 日线高 2.85%，"
         "200 股浮盈约 26480 港元（约 2.6 万），收益率 34.8%；茅台市值 1.7万亿、即 17000 亿，ROE 33.5%，市盈率 21.3。")
    v = cite.verify(t, s)
    assert v["ok"], v["unverified"]


def test_skips_dates_windows_counts_and_codes():
    s = cite.Sources()
    t = "2026-10-28 16:20 公告：0700.HK 近 20 日、第 3 季度、持有 200 股、10-Q 文件、2025 年。"
    assert cite.verify(t, s)["ok"]


def test_window_lengths_in_sources_are_not_data():
    s = cite.Sources()  # 信号「上穿 20 日线」、日期里的数字不能给「仓位 20%」「目标价 2026」当出处
    s.add("get_quotes", {}, {"X": {"date": "2026-10-28", "close": 512.4, "signals": ["上穿 20 日线"]}})
    assert cite.verify("建议仓位 20%，目标 1028 元", s)["unverified"] == ["20%", "1028 "[:4]]


def test_flag_and_finalize():
    s, _ = src()
    t, bad = cite.flag("腾讯收盘 512.4 港元[9]，市盈率 18.6 倍。", s)
    assert bad == ["18.6"] and "18.6（未核实）" in t and "注：标「未核实」" in t
    out, used = cite.finalize(t, s)
    assert "512.4[1]" in out and "[9]" not in out  # 无效角标删掉，按出现顺序重编号
    assert out.split(cite.SOURCES_HEAD)[1].strip().startswith("[1] 行情 · 0700.HK · Yahoo Finance · 截至 2026-10-28")
    assert cite.verify(out, s)["ok"]  # 已标未核实的不再算错
    assert cite.parse_sources(out)[1].startswith("行情")


def test_finalize_keeps_model_markers_and_user_numbers_unmarked():
    s, _ = src()
    s.add_context("我想买 300 股", "你的提问", "user")
    out, _ = cite.finalize("收盘 512.4[2]，你说的 300 股按 512.4 算是 153720。", s)
    assert out.startswith("收盘 512.4[1]")
    assert "153720[" in out  # 300 来自提问（不加角标），乘积有出处


# ---------------------------------------------------------------- agent 修正闭环
class OnceLiar(ev.MockLLM):
    """第一次编数字，被打回后改正。"""
    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        out = super().chat(messages, tools, temperature, timeout)
        if out.get("content") and not messages[-1]["content"].startswith("[系统核对"):
            out["content"] = out["content"].replace("仅供参考", "目标价 688.8 港元。\n仅供参考")
        return out


class StubbornLiar(ev.MockLLM):
    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        out = super().chat(messages, tools, temperature, timeout)
        if out.get("content"):
            out["content"] = out["content"].replace("仅供参考", "目标价 688.8 港元。\n仅供参考")
        return out


def run_agent(llm, q="腾讯现在多少钱？"):
    from stock_agent.memory import Memory
    log = []
    a = ev.make_agent(ev.load_cases()["fixtures"], Memory(), llm, log)
    return a, a.ask(q)


def test_agent_fixes_once():
    a, ans = run_agent(OnceLiar())
    assert "688.8" not in ans and "512.4[1]" in ans
    assert a.last_meta["verify"]["first_unverified"] == ["688.8"] and a.last_meta["verify"]["fixed"]
    assert any(m["role"] == "user" and m["content"].startswith("[系统核对") for m in a.messages)


def test_agent_flags_when_model_refuses_to_fix():
    a, ans = run_agent(StubbornLiar())
    assert "688.8（未核实）" in ans and "注：标「未核实」" in ans
    assert a.last_meta["verify"]["flagged"] == ["688.8"] and not a.last_meta["verify"]["fixed"]


def test_software_metadata_is_not_a_financial_source(monkeypatch):
    from stock_agent import agent
    from stock_agent.memory import Memory
    monkeypatch.setattr(agent, "_about_stoa", lambda: "\n\n## 关于 Stoa 自己\n当前版本 v0.34.0，示例目标价 688.8 港元。")
    a = agent.Agent(Memory())
    a._begin_sources("腾讯现在多少钱？")
    a.sources.add("get_quotes", {"symbols": ["0700.HK"]}, {"0700.HK": {"close": 512.4}})
    assert "688.8" not in a._sys_base  # v0.37：版本说明只在问到 Stoa 自己的更新时附进本轮系统提示，不进 _sys_base
    assert cite.verify("腾讯目标价 688.8 港元。", a.sources)["unverified"] == ["688.8"]
    a._begin_sources("Stoa 更新了什么？")  # 即使本轮附了版本说明，它也不是财务数字的出处
    a.sources.add("get_quotes", {"symbols": ["0700.HK"]}, {"0700.HK": {"close": 512.4}})
    assert agent.ABOUT_RE.search("Stoa 更新了什么？")
    assert cite.verify("腾讯目标价 688.8 港元。", a.sources)["unverified"] == ["688.8"]


def test_eval_mock_hallucination_is_caught():
    out = ev.run("mock", only="cite-04", save=False)
    r = out["results"][0]
    assert r["verify"]["first_unverified"] == ["18.6"] and r["verify"]["fixed"] and r["score"] == 1


# ---------------------------------------------------------------- 埋错例：核对器本身也要被核对
# 每条正确回答配三个「必须被拦下」的变体：数字改一位、涨跌方向写反、日期错一天。
# 任何一个变体通过，说明核对器有盲区，之前所有「已核实」都要打问号（参考 REA/TH08：字节比对通过、常量却错了）。
GOOD = "腾讯 10 月 28 日收盘 512.4 港元，上涨 2.35%，茅台市盈率 21.3 倍。"
PLANTED = {
    "数字改一位": ("腾讯 10 月 28 日收盘 512.4 港元，上涨 2.35%，茅台市盈率 23.1 倍。", "23.1"),
    "涨跌写反": ("腾讯 10 月 28 日收盘 512.4 港元，下跌 2.35%，茅台市盈率 21.3 倍。", "2.35%"),
    "日期错一天": ("腾讯 10 月 27 日收盘 512.4 港元，上涨 2.35%，茅台市盈率 21.3 倍。", "10 月 27 日"),
}


def test_planted_good_passes():
    s, _ = src()
    assert cite.verify(GOOD, s)["ok"], cite.verify(GOOD, s)["unverified"]


@pytest.mark.parametrize("kind", list(PLANTED))
def test_planted_failures_are_rejected(kind):
    s, _ = src()
    text, culprit = PLANTED[kind]
    v = cite.verify(text, s)
    assert not v["ok"], f"埋的错例「{kind}」没被拦下：核对器有盲区"
    assert v["unverified"] == [culprit]
    assert v["why"][culprit] != "找不到出处" or kind == "数字改一位"


def test_direction_and_date_reasons_reach_the_model():
    s, _ = src()
    v = cite.verify(PLANTED["涨跌写反"][0], s)
    p = cite.correction_prompt(v["unverified"], v["why"])
    assert "方向" in p and "2.35%" in p
    t, bad = cite.flag(PLANTED["日期错一天"][0], s)
    assert "10 月 27 日（未核实）" in t and bad == ["10 月 27 日"]


def test_direction_check_is_not_overeager():
    s = cite.Sources()
    s.add("get_quotes", {}, {"X": {"date": "2026-10-28", "close": 10.0, "chg_pct": -2.35}})
    s.add("web_search", {}, {"results": [{"title": "公司三季度亏损 120 亿元", "url": "u"}]})
    assert cite.verify("下跌 2.35%，跌幅 2.35%，-2.35%，三季度亏损 120 亿元。", s)["ok"]
    assert not cite.verify("上涨 2.35%", s)["ok"]
    assert cite.verify("较 20 日线高 2.35%", s)["ok"]  # 没有方向词紧挨着：不判方向


def test_date_check_ignores_today_and_distant_history():
    s, _ = src()
    t = f"今天是 {cite.today()}。上次年报在 2026 年 3 月 20 日发布，10 月 28 日收盘 512.4 港元。"
    assert cite.verify(t, s)["ok"], cite.verify(t, s)["unverified"]


def test_data_tag_without_source_is_downgraded():
    s, _ = src()
    t, used = cite.finalize("要点：\n1. 〔数据〕腾讯收盘 512.4 港元\n2. 〔数据〕估值偏贵\n3. 〔推断〕短线偏强\n4. 〔未知〕下季度回购节奏\n- [Data] valuation looks rich", s)
    lines = t.split("\n")
    assert lines[1].startswith("1. 〔数据〕") and "[1]" in lines[1]
    assert lines[2] == "2. 〔推断〕估值偏贵"
    assert lines[3] == "3. 〔推断〕短线偏强" and lines[4] == "4. 〔未知〕下季度回购节奏"
    assert lines[5] == "- [Inference] valuation looks rich"


# ---------------- SEC 结构化事实：相同数字不能证明不同事实 ----------------

def sec_source(s, *, symbol="ACME", concept="us-gaap:Revenues", start="2024-01-01",
               end="2024-12-31", unit="USD", value=123456000):
    # 与 edgar.xbrl_facts 的真实返回结构一致；fy/fp 是申报期间，不是经济期间。
    result = {"cik": "0000000123", "company": symbol + " Corporation",
              "source": "SEC XBRL companyfacts", "url": "https://data.sec.gov/fixture/" + symbol,
              "facts": {concept: {"label": concept.split(":")[-1], "values": [
                  {"start": start, "end": end, "val": value, "unit": unit,
                   "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-02-01",
                   "accn": "0000000123-25-000001"}]}}}
    return s.add("sec_xbrl", {"symbol": symbol, "concepts": [concept]}, result)["_cite"]


@pytest.mark.parametrize("change", [
    {"symbol": "OTHER"}, {"concept": "us-gaap:NetIncomeLoss"},
    {"start": "2023-01-01", "end": "2023-12-31"}, {"unit": "HKD"},
], ids=["company", "metric", "period", "currency"])
def test_sec_same_number_wrong_fact_is_rejected(change):
    s = cite.Sources()
    sid = sec_source(s, **change)
    v = cite.verify(f"ACME 2024 年全年营收 1.23456 亿美元[{sid}]。", s)
    assert not v["ok"], "相同数值不能掩盖 SEC 事实语义错误"
    assert v["unverified"] == ["1.23456 亿"]


@pytest.mark.parametrize("text", [
    "ACME 2024 年全年营收 1.23456 亿美元[1]。",
    "ACME 2024 年全年营业收入 12345.6 万美元。",
    "ACME Corporation 2024 年全年营收 123,456,000 美元。",
    "ACME FY2024 revenue 123456000 USD。",
])
def test_sec_correct_and_equivalent_values_pass(text):
    s = cite.Sources()
    sec_source(s)
    v = cite.verify(text, s)
    assert v["numbers"] and v["ok"], v
    assert all(n["sids"] == [1] for n in v["numbers"])


@pytest.mark.parametrize("text", [
    "OTHER 2024 年全年营收 1.23456 亿美元。",
    "ACME FY2023 revenue 123456000 USD。",
    "ACME FY2024 revenue 123456000 HKD。",
    "ACME 2024 年全年营收 1.23456 亿美元[2]。",
])
def test_sec_binding_cannot_fall_back_to_numeric_pool(text):
    s = cite.Sources()
    sec_source(s)
    sec_source(s, symbol="OTHER", concept="us-gaap:NetIncomeLoss")
    assert not cite.verify(text, s)["ok"]


@pytest.mark.parametrize("start,end,text,ok", [
    ("2024-07-01", "2024-09-30", "2024 年第三季度", True),
    ("2024-01-01", "2024-09-30", "2024 年前三季度", True),
    ("2024-01-01", "2024-09-30", "2024 年第三季度", False),
    (None, "2024-12-31", "2024 年全年", False),
    ("2024-10-01", "2024-12-31", "2024 年全年", False),
    ("2024-01-01", "2024-12-31", "2025 年全年", False),
])
def test_sec_economic_period_is_not_filing_period(start, end, text, ok):
    s = cite.Sources()
    sec_source(s, start=start, end=end)
    v = cite.verify(f"ACME {text}营收 1.23456 亿美元。", s)
    assert v["ok"] is ok, v


@pytest.mark.parametrize("value", [123456000, 987654000])
def test_sec_conflicting_restatements_do_not_follow_answer(value):
    s = cite.Sources()
    sec_source(s)
    sec_source(s, value=987654000)
    assert not cite.verify(f"ACME 2024 年全年营收 {value} 美元。", s)["ok"]


def test_sec_equivalent_disclosures_are_not_ambiguous():
    s = cite.Sources()
    sec_source(s)
    sec_source(s)
    assert cite.verify("ACME 2024 年全年营收 1.23456 亿美元。", s)["ok"]


@pytest.mark.parametrize("text", [
    "OTHER 2024 年全年营收 1.23456 亿美元。",
    "ACME 2023 年营收 1.23456 亿美元。",
    "ACME 2024 年全年营收 123456000%。",
    "ACME 2024 年全年营收人民币 123456000。",
    "ACME 2024-02-01 至 2024-12-31 营收 1.23456 亿美元。",
])
def test_sec_missing_marker_or_partial_context_does_not_erase_semantics(text):
    s = cite.Sources()
    sec_source(s)
    assert not cite.verify(text, s)["ok"]


@pytest.mark.parametrize("concept,start,text", [
    ("us-gaap:InterestExpense", "2024-01-01", "ACME 2024 年全年 us-gaap:InterestExpense 123456000 美元。"),
    ("us-gaap:Assets", None, "ACME 截至 2024-12-31 总资产 123456000 美元。"),
    ("us-gaap:NetIncomeLoss", "2024-01-01", "ACME 2024 年全年净利润 123456000 美元。"),
])
def test_sec_exact_concepts_and_instant_facts_pass(concept, start, text):
    s = cite.Sources()
    sec_source(s, concept=concept, start=start)
    v = cite.verify(text, s)
    assert v["numbers"] and v["ok"], v


@pytest.mark.parametrize("text,ok", [
    ("Alpha Corp 2024 年全年营收 1.23456 亿美元[1]。", True),
    ("Beta Corp 2024 年全年营收 1.23456 亿美元[1]。", False),
    ("Alpha Corp 2023 年全年营收 1.23456 亿美元[1]。", False),
    ("Alpha Corp 2024 年全年营收 1.23456 亿港元[1]。", False),
])
def test_sec_frames_bind_each_company_not_request_symbol(text, ok):
    s = cite.Sources()
    s.add("sec_xbrl", {"frame_period": "CY2024", "symbol": "Beta Corp"}, {
        "concept": "us-gaap:Revenues", "period": "CY2024", "unit": "USD",
        "source": "SEC XBRL frames", "url": "https://data.sec.gov/fixture/frame",
        "data": [{"company": "Alpha Corp", "cik": "0000000001", "end": "2024-12-31", "val": 123456000, "accn": "a1"},
                 {"company": "Beta Corp", "cik": "0000000002", "end": "2024-12-31", "val": 987654000, "accn": "b1"}]})
    assert cite.verify(text, s)["ok"] is ok


def test_sec_edgar_tool_result_binds_without_changing_payload(monkeypatch):
    from copy import deepcopy
    from stock_agent import edgar
    monkeypatch.setattr(edgar, "_cik", lambda symbol: ("0000000123", "ACME Corporation"))
    monkeypatch.setattr(edgar, "_facts", lambda cik: {"entityName": "ACME Corporation", "facts": {
        "us-gaap": {"Revenues": {"label": "Revenues", "units": {"USD": [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 123456000, "fy": 2025,
             "fp": "FY", "form": "10-K", "filed": "2025-02-01", "accn": "a1"}]}}}}})
    args = {"symbol": "ACME", "concepts": ["us-gaap:Revenues"]}
    raw = edgar.run_tool("sec_xbrl", args)
    original = deepcopy(raw)
    s = cite.Sources()
    out = s.add("sec_xbrl", args, raw)
    assert raw == original and out == {**original, "_cite": 1}
    good = "ACME 2024 年全年营收 1.23456 亿美元。"
    assert cite.verify(good, s)["ok"]
    rendered, used = cite.finalize(good, s)
    assert "1.23456 亿美元[1]" in rendered and used[0]["url"] == raw["url"]
    bad, missing = cite.flag("ACME 2025 年全年营收 1.23456 亿美元[1]。", s)
    assert missing == ["1.23456 亿"] and cite.UNVERIFIED in bad


def test_sec_verified_number_is_not_reused_for_another_company():
    s = cite.Sources()
    sec_source(s)
    v = cite.verify("ACME 2024 年全年营收 1.23456 亿美元。OTHER 2024 年全年营收 1.23456 亿美元。", s)
    assert v["unverified"] == ["1.23456 亿"]


@pytest.mark.parametrize("text", [
    "ACME FY2024 revenue 123456000 USD[2]。",
    "ACME 2024 年全年营收 123456000 EUR。",
    "ACME 2024 年第四季度营收 1.23456 亿美元。",
    "ACME 2024 Q4 revenue 123456000 USD。",
    "ACME FY2024 Q1 revenue 123456000 USD。",
])
def test_sec_additional_unit_period_and_citation_constraints(text):
    s = cite.Sources()
    sec_source(s)
    sec_source(s, symbol="OTHER")
    assert not cite.verify(text, s)["ok"]


def test_sec_does_not_take_over_explicit_get_financials_citation():
    s = cite.Sources()
    sec_source(s)
    sid = s.add("get_financials", {"symbol": "ACME"}, {
        "symbol": "ACME", "currency": "USD",
        "unified": [{"period": "2024-12-31", "revenue": 876543000}]})["_cite"]
    v = cite.verify(f"ACME 2024 年全年营收 8.76543 亿美元[{sid}]。", s)
    assert v["ok"], v


@pytest.mark.parametrize("text", [
    "ACME 2024 年全年营收约 1.23 亿美元。",
    "ACME 2024-01-01 至 2024-12-31 营收 1.23456 亿美元。",
])
def test_sec_rounding_and_exact_duration_are_equivalent(text):
    s = cite.Sources()
    sec_source(s)
    assert cite.verify(text, s)["ok"]


def test_sec_euro_wrong_citation_is_rejected_through_rendering():
    s = cite.Sources()
    sec_source(s, unit="EUR")
    sec_source(s, symbol="OTHER", unit="EUR", value=987654000)
    text = "ACME 2024 年全年营收 123456000 欧元[2]。"
    assert not cite.verify(text, s)["ok"]
    flagged, bad = cite.flag(text, s)
    assert bad == ["123456000"]
    rendered, used = cite.finalize(flagged, s)
    assert cite.UNVERIFIED in rendered
    assert not used


@pytest.mark.parametrize("amount", ["123456000 欧元", "1.23456 亿欧元", "12345.6 万EUR"])
def test_sec_euro_correct_citation_and_equivalent_units(amount):
    s = cite.Sources()
    sec_source(s, unit="EUR")
    text = f"ACME 2024 年全年营收 {amount}[1]。"
    assert cite.verify(text, s)["ok"]
    _, used = cite.finalize(text, s)
    assert [u["id"] for u in used] == [1]


@pytest.mark.parametrize("currency", ["GBP", "英镑", "JPY", "日元", "CHF", "瑞士法郎"])
def test_sec_explicit_other_currency_cannot_be_treated_as_unspecified(currency):
    s = cite.Sources()
    sec_source(s)
    text = f"ACME 2024 年全年营收 123456000 {currency}[1]。"
    assert not cite.verify(text, s)["ok"]


@pytest.mark.parametrize("currency,unit", [("GBP", "GBP"), ("英镑", "GBP"), ("JPY", "JPY"), ("日元", "JPY"), ("CHF", "CHF"), ("瑞士法郎", "CHF"), ("USD", "USD")])
def test_sec_currency_names_match_only_their_units(currency, unit):
    s = cite.Sources()
    sec_source(s, unit=unit)
    text = f"ACME 2024 年全年营收 123456000 {currency}[1]。"
    v = cite.verify(text, s)
    assert v["ok"] and len(v["numbers"]) == 1
    assert v["numbers"][0]["sids"] == [1]


@pytest.mark.parametrize("period", ["2023 年全年", "FY2023 "])
def test_sec_current_clause_year_overrides_previous_clause(period):
    s = cite.Sources()
    sec_source(s)
    text = f"ACME 2024 年全年营收 123456000 USD，{period}营收 123456000 USD。"
    v = cite.verify(text, s)
    assert [n["sids"] for n in v["numbers"]] == [[1], None]


@pytest.mark.parametrize("second", ["2023 年全年营收 8.76543 亿美元", "FY2023 revenue 876543000 USD"])
def test_sec_two_clause_years_bind_to_their_own_facts(second):
    s = cite.Sources()
    sec_source(s)
    sec_source(s, start="2023-01-01", end="2023-12-31", value=876543000)
    v = cite.verify(f"ACME 2024 年全年营收 123456000 USD，{second}。", s)
    assert v["ok"] and len(v["numbers"]) == 2


def test_sec_english_sentence_boundary_clears_company():
    s = cite.Sources()
    sec_source(s)
    text = "ACME FY2024 revenue 123456000 USD. OTHER FY2024 revenue 123456000 USD."
    v = cite.verify(text, s)
    assert [n["sids"] for n in v["numbers"]] == [[1], None]


@pytest.mark.parametrize("company", ["ACME", "ACME Inc.", "ACME Corp.", "ACME Ltd.", "ACME Co.", "ACME U.S."])
def test_sec_english_sentence_keeps_decimals_and_company_abbreviations(company):
    s = cite.Sources()
    sec_source(s)
    text = f"{company} FY2024 revenue 1.23456 亿USD. ACME FY2024 revenue 123456000 USD."
    v = cite.verify(text, s)
    assert v["ok"] and len(v["numbers"]) == 2, v


def test_sec_uncovered_metric_delegates_to_structured_financials():
    s = cite.Sources()
    sec_source(s, concept="us-gaap:NetIncomeLoss")
    sid = s.add("get_financials", {"symbol": "ACME"}, {
        "symbol": "ACME", "currency": "USD",
        "unified": [{"period": "2024-12-31", "revenue": 876543000}]})["_cite"]
    text = "ACME 2024 年全年营收 8.76543 亿美元。"
    v = cite.verify(text, s)
    assert v["ok"] and v["numbers"][0]["sids"] == [sid], v
    _, used = cite.finalize(text, s)
    assert [u["id"] for u in used] == [sid]


@pytest.mark.parametrize("currency", ["日元", "英镑", "XYZ"])
@pytest.mark.parametrize("placement", ["before", "after"])
def test_sec_explicit_currency_is_preserved_without_marker(currency, placement):
    s = cite.Sources()
    sec_source(s)
    amount = f"{currency} 123456000" if placement == "before" else f"123456000{currency}"
    v = cite.verify(f"ACME 2024 年全年营收 {amount}。", s)
    assert len(v["numbers"]) == 1 and not v["ok"], v


@pytest.mark.parametrize("mode", ["explicit_sec", "wrong_year", "wrong_unit", "numeric_only", "covered_metric"])
def test_sec_uncovered_metric_never_enables_numeric_or_wrong_cite_fallback(mode):
    s = cite.Sources()
    sec_source(s, concept="us-gaap:Revenues" if mode == "covered_metric" else "us-gaap:NetIncomeLoss")
    if mode == "numeric_only":
        s.add("calc", {}, {"results": {"x": 876543000}})
    else:
        s.add("get_financials", {"symbol": "ACME"}, {
            "symbol": "ACME", "currency": "HKD" if mode == "wrong_unit" else "USD",
            "unified": [{"period": "2023-12-31" if mode == "wrong_year" else "2024-12-31", "revenue": 876543000}]})
    marker = "[1]" if mode == "explicit_sec" else ""
    text = f"ACME 2024 年全年营收 8.76543 亿美元{marker}。"
    assert not cite.verify(text, s)["ok"]
    flagged, bad = cite.flag(text, s)
    assert bad == ["8.76543 亿"]
    _, used = cite.finalize(flagged, s)
    assert not used


def financial_source(s, *, symbol="ACME", field="revenue", period="2024-12-31",
                     currency="USD", value=123456000):
    return s.add("get_financials", {"symbol": symbol}, {
        "symbol": symbol, "currency": currency,
        "unified": [{"period": period, field: value}]})["_cite"]


def assert_citation_chain(text, s, expected):
    v = cite.verify(text, s)
    assert [n["sids"] for n in v["numbers"]] == expected, v
    assert v["ok"] is all(expected)
    flagged, bad = cite.flag(text, s)
    assert len(bad) == expected.count(None)
    assert flagged.count(cite.UNVERIFIED) == expected.count(None)
    for candidate in (text, flagged):
        rendered, used = cite.finalize(candidate, s)
        assert {u["id"] for u in used} == {sid for ids in expected if ids for sid in ids}, rendered
        assert len(re.findall(r"\[\d+\]", cite.strip_sources(rendered))) == sum(bool(ids) for ids in expected)
        if not any(expected):
            assert not cite.parse_sources(rendered)
            assert not re.search(r"\[\d+\]", rendered)


@pytest.mark.parametrize("change,cited,ok", [
    ({}, 2, True),
    ({"symbol": "OTHER"}, 2, False),
    ({"field": "net_profit"}, 2, False),
    ({"period": "2023-12-31"}, 2, False),
    ({"currency": "HKD"}, 2, False),
    ({}, 3, False),
    ({}, 99, False),
])
def test_cross_adapter_explicit_source_is_binding(change, cited, ok):
    s = cite.Sources()
    sec_source(s)
    financial_source(s, **change)
    financial_source(s, symbol="OTHER")
    text = f"ACME 2024 年全年营收 123456000 美元[{cited}]。"
    assert_citation_chain(text, s, [[2] if ok else None])


@pytest.mark.parametrize("currency", ["USD", "美元", "GBP", "英镑", "XYZ"])
@pytest.mark.parametrize("marker", ["", "[2]", "[3]"])
@pytest.mark.parametrize("placement", ["before", "after"])
def test_cross_adapter_missing_metric_preserves_currency(currency, marker, placement):
    s = cite.Sources()
    sec_source(s, concept="us-gaap:NetIncomeLoss")
    financial_source(s, value=876543000)
    financial_source(s, symbol="OTHER", value=876543000)
    amount = f"{currency} 876543000" if placement == "before" else f"876543000 {currency}"
    text = f"ACME 2024 年全年营收 {amount}{marker}。"
    expected = [2] if currency in ("USD", "美元") and marker != "[3]" else None
    assert_citation_chain(text, s, [expected])


@pytest.mark.parametrize("currency,unit", [("GBP", "GBP"), ("英镑", "GBP"), ("XYZ", "XYZ")])
def test_cross_adapter_other_currency_same_source_passes(currency, unit):
    s = cite.Sources()
    sec_source(s, concept="us-gaap:NetIncomeLoss")
    financial_source(s, currency=unit, value=876543000)
    assert_citation_chain(f"ACME 2024 年全年营收 876543000 {currency}[2]。", s, [[2]])


@pytest.mark.parametrize("period", ["2023 年全年", "FY2023 ", "2023 年"])
@pytest.mark.parametrize("marker", ["", "[2]", "[3]"])
@pytest.mark.parametrize("covered", [False, True])
def test_cross_adapter_current_clause_period(period, marker, covered):
    s = cite.Sources()
    sec_source(s, concept="us-gaap:NetIncomeLoss")
    financial_source(s, value=876543000)
    if covered:
        financial_source(s, period="2023-12-31", value=876543000)
    financial_source(s, symbol="OTHER", value=876543000)
    text = f"ACME 2024 年全年营收 876543000 美元，{period}营收 876543000 美元{marker}。"
    assert_citation_chain(text, s, [[2], [2] if covered and marker != "[3]" else None])


# ---------------- 派生数字与「看起来像阈值」的数（v0.39.2） ----------------

def test_decimal_with_trailing_zero_is_not_short():
    """530.0 是四位有效数字，不能被 rstrip("0") 削成 53 当成阈值跳过。"""
    assert cite._round("530.0") is False
    assert cite._round("40%") is True
    assert cite._round("200") is True
    s, _ = src()
    out, used = cite.finalize("52 周收盘高点 530.0 港元。", s)
    assert "530.0[" in out and used


def test_derived_short_number_gets_marker():
    """算出来的短数（占比 40%）要带角标；纯阈值（仓位 20%）不补。"""
    assert cite.auto_skip({"text": "40%", "derived": True}) is False
    assert cite.auto_skip({"text": "20%", "derived": False}) is True
    s, _ = src()
    out, _ = cite.finalize("仓位控制在 20%。", s)
    assert "20%[" not in out
