"""数字强制引用：来源登记、核对器抓编造数字、不误伤换算/推导、修正一次、仍不改就标未核实、角标与来源清单。"""
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
