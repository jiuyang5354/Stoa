"""v0.35.1：简称绑定标的核对涨跌幅（cite）、快问方向判断补失效条件、诱导题补风险（answercheck）。含正常问题不误报的反例。全部离线。"""
import pytest

from stock_agent import answercheck as ac
from stock_agent import cite


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


MT = {"date": "2026-10-28", "close": 1354.0, "chg_pct": -0.84, "chg_5d": -2.31, "chg_20d": -5.47, "ma20": 1398.6, "ma60": 1421.3,
      "rsi": 38.4, "high_52w": 1688.0, "low_52w": 1301.5, "signals": []}
TX = {"date": "2026-10-28", "close": 512.4, "chg_pct": 2.35, "chg_5d": 4.12, "chg_20d": 7.86, "ma20": 498.21, "ma60": 470.55,
      "rsi": 63.2, "high_52w": 530.0, "low_52w": 365.2, "signals": []}


def srcs(*pairs, question="贵州茅台今天涨跌多少？"):
    s = cite.Sources()
    s.add_context(question, "你的提问", "user")
    for sym, q in pairs:
        s.add("get_quotes", {"symbols": [sym]}, {sym: dict(q)})
    return s


# ================================================================ 1. 简称绑定标的
def test_short_name_fabricated_move_caught():
    """adv-12：low_52w/ma60−1 = −8.43% 这种无关比例不能替「茅台今天大涨 8.4%」作出处。"""
    s = srcs(("600519.SS", MT))
    v = cite.verify("茅台今天大涨 8.4%，收于 1354.0 元。", s)
    assert v["unverified"] == ["8.4%"]


@pytest.mark.parametrize("text", ["茅台今天跌 0.84%，收于 1354.0 元。", "贵州茅台当日 -0.84%，收盘 1354.0。", "茅台 20 日均线 1398.6，现价 1354.0。"])
def test_short_name_real_numbers_pass(text):
    assert cite.verify(text, srcs(("600519.SS", MT)))["ok"]


def test_name_aliases():
    assert {"茅台", "贵州茅台"} <= cite.name_aliases("600519.SS", "贵州茅台")
    assert "腾讯" in cite.name_aliases("0700.HK", "腾讯控股")
    assert "腾讯" in cite.name_aliases("0700.HK")  # 数据里没有名称时靠内置常用名
    assert cite.name_aliases("ZZZZ") == set()


def test_short_name_binds_the_right_stock():
    """两只股票同一轮：茅台的涨跌幅不能拿腾讯的 2.35% 作出处。"""
    s = srcs(("600519.SS", MT), ("0700.HK", TX))
    assert cite.verify("腾讯今天涨 2.35%，茅台今天跌 0.84%。", s)["ok"]
    assert cite.verify("茅台今天涨 2.35%。", s)["unverified"] == ["2.35%"]


def test_nameless_same_day_move_checked_per_quote():
    s = srcs(("600519.SS", MT))
    assert cite.verify("今天大涨 8.4%。", s)["unverified"] == ["8.4%"]
    assert cite.verify("今天下跌 0.84%。", s)["ok"]


def test_nameless_move_without_quotes_unchanged():
    """本轮没有个股行情（只看了大盘）：沿用通用核对，指数涨跌幅原样出现就算有出处。"""
    s = cite.Sources()
    s.add("market_overview", {}, {"date": "2026-10-28", "indices": {"A股": [{"name": "沪深300", "close": 3921.5, "chg_pct": 1.23}]}})
    assert cite.verify("沪深300 今天涨 1.23%。", s)["ok"]


def test_derived_ratio_still_allowed_when_not_a_same_day_move():
    """不是「今天涨跌」的推算比例（相对均线的位置）照旧放行。"""
    s = srcs(("0700.HK", TX))
    assert cite.verify("腾讯现价较 20 日线高 2.85%。", s)["ok"]


def test_financial_field_must_be_in_same_clause():
    """「市盈率 18.6 倍，……还差 17.6」：后一个数不按市盈率核对（简称绑定后暴露的老问题）。"""
    s = srcs(("0700.HK", TX), question="腾讯现在多少钱？")
    v = cite.verify("腾讯最新收盘 512.4 港元，市盈率 18.6 倍，离 52 周高点还差 17.6。", s)
    assert v["unverified"] == ["18.6"]


def test_market_cap_string_with_currency_unit():
    s = cite.Sources()
    s.add("get_financials", {"symbol": "600519.SS"}, {"symbol": "600519.SS", "name": "贵州茅台", "market_cap": "1.70万亿元"})
    assert cite.verify("茅台市值 1.7万亿。", s)["ok"]


# ================================================================ 2. 失效条件
@pytest.mark.parametrize("text,d", [
    ("从均线结构看，短期偏多，可以考虑逢低买入。", 1),
    ("目前看空，建议减仓。", -1),
    ("短期震荡，先观望。", 0),
    ("我的目标价看 560 港元。", 1),
    ("I'm bullish on it near term.", 1),
])
def test_direction_detected(text, d):
    assert ac.direction(text) == d


@pytest.mark.parametrize("text", [
    "腾讯最新收盘 512.4 港元，当日 +2.35%。",
    "分析师目标价均值 620 港元，买入评级 40 家。",
    "上次我们看涨英伟达的理由是数据中心资本开支上行。",
    "和你的持仓逻辑对照：加强。你的逻辑偏看多。",
    "我不能替你下单，只能提供分析。",
    "这里只列行情数据，不做方向判断。",
])
def test_no_direction_in_plain_or_reported_text(text):
    assert ac.direction(text) is None


def test_invalidation_added_with_price_from_quote():
    s = srcs(("0700.HK", TX), question="腾讯现在能买吗？")
    out, note = ac.review("腾讯现在能买吗？", "从均线结构看，腾讯短期偏多。\n\n仅供参考，不构成投资建议。", s)
    assert note == {"invalidation": True}
    line = [x for x in out.splitlines() if x.startswith("失效条件")][0]
    assert "跌破 20 日均线 498.21" in line and "看多" in line
    assert out.rstrip().endswith("不构成投资建议。")  # 免责句仍在最后
    assert cite.verify(out, s)["ok"]  # 补写的价格有出处


def test_bearish_uses_resistance_and_range_uses_both():
    assert "站上 20 日均线 1398.6" in ac.invalidation_line(-1, MT, "600519.SS")
    line = ac.invalidation_line(0, MT)
    assert "跌破 52 周低点 1301.5" in line and "站上 20 日均线 1398.6" in line


def test_invalidation_event_fallback_without_quotes():
    out, note = ac.review("怎么看", "整体偏多。", cite.Sources())
    assert note == {"invalidation": True} and "失效条件" in out and "财报" in out


def test_no_invalidation_when_already_given_or_no_direction():
    s = srcs(("0700.HK", TX))
    given = "短期偏多；若收盘跌破 20 日均线 498.21 则判断作废。"
    assert ac.review("能买吗", given, s) == (given, {})
    plain = "腾讯最新收盘 512.4 港元。"
    assert ac.review("腾讯现在多少钱？", plain, s) == (plain, {})


def test_english_invalidation_and_order_before_sources_and_marks():
    s = srcs(("0700.HK", TX), question="Is Tencent a buy?")
    reply = "Tencent looks bullish short term.\n[[cards:abc]]\n\n—— 来源 ——\n[1] 行情"
    out, note = ac.review("Is Tencent a buy?", reply, s)
    assert note == {"invalidation": True}
    body, tail = cite.split_sources(out)
    assert "Invalidation:" in body and "closes below the 20-day average 498.21" in body
    assert body.rstrip().endswith("[[cards:abc]]") and tail.startswith("—— 来源 ——")


# ================================================================ 3. 诱导题风险
@pytest.mark.parametrize("q,kinds", [
    ("帮我全仓买入茅台", ["concentration"]),
    ("满仓腾讯可以吗", ["concentration"]),
    ("我想借钱炒股买英伟达", ["leverage"]),
    ("加杠杆做多纳指行不行", ["leverage"]),
    ("这只保证涨吗", ["guarantee"]),
    ("推荐一只稳赚不赔的股票", ["guarantee"]),
    ("all in TSLA?", ["concentration"]),
])
def test_lures_detected(q, kinds):
    assert ac.lures(q) == kinds


@pytest.mark.parametrize("q", ["这家公司财务杠杆高吗？", "我不想全仓，怎么分配？", "腾讯股价去年翻倍了吗？", "茅台现在多少钱？", "仓位管理有什么原则？"])
def test_no_lure_on_normal_questions(q):
    assert ac.lures(q) == []


def test_risk_added_when_missing_and_not_when_covered():
    out, note = ac.review("帮我全仓买入茅台", "我不能替你下单。\n仅供参考，不构成投资建议。")
    assert note == {"risks": ["concentration"]} and "单一标的" in out and "回撤" in out
    covered = "全仓单一个股过于集中，一旦回撤亏损会很大。"
    assert ac.review("帮我全仓买入茅台", covered) == (covered, {})
    out, note = ac.review("借钱买腾讯", "不建议。")
    assert note["risks"] == ["leverage"] and "强制平仓" in out
    # 已说了不能保证，但没说回撤 → 只补一句回撤
    out, note = ac.review("腾讯保证涨吗", "没有人能保证涨跌。")
    assert note == {"risks": ["drawdown"]} and "回撤" in out


def test_normal_answer_untouched():
    s = srcs(("600519.SS", MT))
    reply = "600519.SS 最新收盘 1354.0（2026-10-28），当日 -0.84%。\n仅供参考，不构成投资建议。"
    assert ac.review("茅台现在多少钱？", reply, s) == (reply, {})
