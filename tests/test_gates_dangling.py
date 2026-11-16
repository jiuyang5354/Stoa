"""gates.dangling_action：模型只写「我查一下」却没真的调工具，要打回一次。"""
from stock_agent import gates


def test_promise_to_look_without_tool_call_is_caught():
    # 2026-11-16 评测 refuse-05 的真实输出：宣布要查日线，然后本轮直接结束
    reply = "行情接口没给成交量，我查一下日线历史数据。"
    nudge = gates.dangling_action("腾讯昨天的成交量是多少股？", reply, {"get_quotes"})
    assert nudge and "现在就调用" in nudge


def test_promise_after_some_tools_is_still_caught():
    reply = "实时行情没返回成交量。让我去看一下历史日线。"
    assert gates.dangling_action("腾讯昨天的成交量是多少股？", reply, {"get_quotes"}) is not None


def test_full_answer_with_numbers_is_not_caught():
    reply = ("腾讯（0700.HK）上一个交易日 2026-11-13 的成交量是 31,493,353 股[1]，"
             "收盘 386.6[1] 港元。\n\n—— 来源 ——\n[1] 历史行情 · 0700.HK · 截至 2026-11-16")
    assert gates.dangling_action("腾讯昨天的成交量是多少股？", reply, {"price_history"}) is None


def test_past_tense_lookup_is_not_a_promise():
    reply = "我查过了，日线接口没有这一天的数据。"
    assert gates.dangling_action("腾讯昨天的成交量是多少股？", reply, {"price_history"}) is None


def test_refusal_with_cannot_lookup_is_not_a_promise():
    reply = "查不到 999999.HK 这只票的行情。"
    assert gates.dangling_action("999999.HK 最近怎么样", reply, {"get_quotes"}) is None


def test_long_answer_ending_with_a_follow_up_offer_is_not_caught():
    reply = ("营收 1126.3 亿元[1]，净利润 337.6 亿元[1]，经营现金流 507.4 亿元[1]，"
             "净现金状态，资产负债表很干净。需要的话我可以再查一下分季度数据。")
    assert gates.dangling_action("网易财报如何", reply, {"get_financials"}) is None
