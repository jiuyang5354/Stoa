"""v0.37 盲评修复：出处写真实数据源、calc 与题设推导的数不再误标「未核实」（复现盲评 Q28、Q30）、
利润表单季/累计口径（Q21）、营业收入 vs 营业总收入（Q14）、行业板块资金 502 时的备用源（Q7）。"""
import json
from pathlib import Path

import pytest

from stock_agent import cite, quant

FIX = json.loads((Path(__file__).parent / "fixtures" / "blind_v1_q28_q30.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


def _src(question, calcs):
    s = cite.Sources()
    s.add_context(question, "你的提问", "user")
    for exprs, variables in calcs:
        s.add("calc", {"expressions": exprs, "variables": variables}, quant.calc(exprs, variables))
    return s


# ---------------------------------------------------------------- Q28 / Q30：calc 与题设
Q28_CALC = ({"position": "principal*leverage", "loss": "position*drop", "remaining": "position-loss",
             "equity": "remaining-(position-principal)", "loss_pct": "loss/principal*100"},
            {"principal": 10000, "leverage": 3, "drop": 0.2})


@pytest.mark.parametrize("sign", [1, -1])
def test_q28_calc_numbers_not_flagged(sign):
    exprs = dict(Q28_CALC[0])
    if sign < 0:  # 模型把亏损写成负数算，回答里写「亏损 6,000」：方向不该判反
        exprs["loss"] = "-position*drop"
        exprs["remaining"] = "position+loss"
        exprs["loss_pct"] = "loss/principal*100"
    s = _src(FIX["28"]["question"], [(exprs, Q28_CALC[1])])
    text, bad = cite.flag(FIX["28"]["answer"], s)
    assert bad == [] and cite.UNVERIFIED not in text
    calc_sid = next(it["id"] for it in s.items if it["tool"] == "calc")
    out, used = cite.finalize(text.replace("6,000 元", f"6,000 元[{calc_sid}]"), s)  # 模型自己标的角标保留，来源清单写算式
    assert used and all("calc" not in u["name"] for u in used)
    assert any(u["name"].startswith("计算：") and "principal*leverage" in u["name"] for u in used)


Q30_CALC = ({"price": "5/(1+y)+5/(1+y)**2+105/(1+y)**3",
             "mac": "(1*5/(1+y)+2*5/(1+y)**2+3*105/(1+y)**3)/price",
             "mod": "mac/(1+y)",
             "conv": "(1*2*5/(1+y)**3+2*3*5/(1+y)**4+3*4*105/(1+y)**5)/price",
             "p_up_est": "price*(1-mod*(y_up-y)+0.5*conv*(y_up-y)**2)",
             "p5": "5/(1+y_up)+5/(1+y_up)**2+105/(1+y_up)**3",
             "chg_up": "p5/price-1",
             "p_dn_est": "price*(1-mod*(y_dn-y)+0.5*conv*(y_dn-y)**2)",
             "p3": "5/(1+y_dn)+5/(1+y_dn)**2+105/(1+y_dn)**3",
             "chg_dn": "p3/price-1"},
            {"y": 0.04, "y_up": 0.05, "y_dn": 0.03})


def test_q30_bond_math_not_flagged():
    r = quant.calc(*Q30_CALC)
    assert r["inputs"] == {"y": 0.04, "y_up": 0.05, "y_dn": 0.03} and abs(r["results"]["price"] - 102.775091) < 1e-6
    s = _src(FIX["30"]["question"], [Q30_CALC])
    text, bad = cite.flag(FIX["30"]["answer"], s)
    assert bad == [], bad  # 0.04 / 0.01 / -2.751407 原来被标「未核实」
    assert "和本轮数据对不上" not in text


def test_fabricated_numbers_still_caught_next_to_calc():
    s = _src(FIX["28"]["question"], [Q28_CALC])
    v = cite.verify("按计算，亏损 6,000 元。另外贵州茅台现价 1555.5 元，市盈率 33.7 倍。", s)
    assert set(v["unverified"]) == {"1555.5", "33.7"}
    # 假设词只放行写明假设的那一行
    v = cite.verify("假设本金 12,345 元、收益率 4%：\n- 每年利息 493.8 元\n\n茅台今天收盘 1555.5 元。", s)
    assert "12,345" not in v["unverified"] and "1555.5" in v["unverified"]


def test_assumption_numbers_get_no_marker_and_question_pct_as_decimal():
    s = _src("票面利率 6%，面值 100，剩余 3 年", [])
    text, bad = cite.flag("按题设，票息为 100×0.06 = 6 元；假设到期收益率为 5.5%，即 0.055。", s)
    assert bad == []
    out, used = cite.finalize(text, s)
    assert "[" not in out.split(cite.SOURCES_HEAD)[0]


def test_calc_direction_still_checked_for_market_data():
    s = cite.Sources()
    s.add("get_quotes", {}, {"600519.SS": {"date": "2026-10-30", "close": 1392.05, "chg_pct": -2.81, "source": "腾讯证券", "name": "贵州茅台"}})
    v = cite.verify("贵州茅台今天上涨 2.81%。", s)
    assert v["unverified"]  # 行情的涨跌方向写反照样抓


# ---------------------------------------------------------------- 出处：真实数据源，不写内部名
INTERNAL = ("calc", "get_market_mood", "price_levels", "memory_links", "记忆图谱", "get_statements", "financial_bridge", "get_chips")


def test_source_names_are_real_providers():
    s = cite.Sources()
    s.add("get_market_mood", {}, {"source": "东方财富 · 涨停板行情中心", "url": "https://quote.eastmoney.com/ztb/", "date": "2026-10-30",
                                  "limit_up": 88, "as_of": "2026-10-30"})
    s.add("price_levels", {"symbol": "600519.SS"}, {"symbol": "600519.SS", "last": 1392.05, "supports": [{"price": 1350.2}], "as_of": "2026-10-30",
                                                     "source": "日线收盘价 · 腾讯证券"})
    s.add("memory_links", {"q": "茅台"}, {"links": ["持有 100 股，成本 1400"]})
    s.add("get_statements", {"symbol": "000333.SZ"}, {"symbol": "000333.SZ", "source": "东方财富 F10 原始报表（利润表/资产负债表/现金流量表）",
                                                       "periods": [{"period": "2026Q3", "income": {"invest_income": 2343084000.0}}]})
    s.add("get_chips", {"symbol": "600519.SS"}, {"symbol": "600519.SS", "source": "东方财富数据中心", "url": "https://data.eastmoney.com/rzrq/detail/600519.html",
                                                 "margin": [{"rz_balance_yi": 12.3}]})
    s.add("get_quotes", {}, {"600519.SS": {"date": "2026-10-30", "close": 1392.05, "source": "腾讯证券"}})
    s.add("get_financials", {}, {"symbol": "600519.SS", "pe": 20.1})
    names = [it["name"] for it in s.items]
    for n in names:
        assert not any(x in n for x in INTERNAL), n
    by = {it["tool"]: it for it in s.items}
    assert by["get_market_mood"]["url"] == "https://quote.eastmoney.com/ztb/" and "东方财富" in by["get_market_mood"]["name"]
    assert by["get_statements"]["url"].startswith("https://emweb.securities.eastmoney.com/pc_hsf10/") and "SZ000333" in by["get_statements"]["url"]
    assert by["get_quotes"]["url"] == "https://gu.qq.com/sh600519"
    assert "#/cwfx" in by["get_financials"]["url"]
    assert "腾讯证券" in by["price_levels"]["name"]


def test_unknown_mcp_tool_named_by_server():
    s = cite.Sources()
    s.add("mcp__wind__edb", {}, {"value": 3.25})
    assert s.items[-1]["name"].startswith("外部工具 · wind")


# ---------------------------------------------------------------- Q14：营业收入 vs 营业总收入
def _fin_src(rows):
    s = cite.Sources()
    s.add("get_financials", {"symbol": "600519.SS"}, {"symbol": "600519.SS", "name": "贵州茅台", "source": "东方财富 F10 + Yahoo Finance",
                                                      "main_indicators": rows})
    return s


MOUTAI_9M = {"报告期": "2026三季报", "营业总收入": "1332.84亿", "营业总收入同比%": 1.8179, "营业收入": "1309.71亿", "营业收入同比%": 1.9595,
             "归母净利润": "644.67亿", "净利同比%": -0.2465}


def test_q14_operating_revenue_not_confused_with_total():
    s = _fin_src([MOUTAI_9M])
    v = cite.verify("2026 年前三季度，贵州茅台营业收入同比增长 1.82%。", s)
    assert v["unverified"] == ["1.82%"] and "不符" in v["why"]["1.82%"]  # 盲评 Q14：1.82% 是营业总收入同比
    assert cite.verify("2026 年前三季度，贵州茅台营业收入同比增长 1.96%。", s)["ok"]
    assert cite.verify("2026 年前三季度，贵州茅台营业总收入同比增长 1.82%。", s)["ok"]
    assert cite.verify("2026 年前三季度，贵州茅台营业收入 1309.71 亿元。", s)["ok"]
    assert not cite.verify("2026 年前三季度，贵州茅台营业收入 1332.84 亿元。", s)["ok"]


def test_q14_falls_back_when_source_has_only_total_revenue():
    row = {k: v for k, v in MOUTAI_9M.items() if not k.startswith("营业收入")}
    s = _fin_src([row])
    assert cite.verify("2026 年前三季度，贵州茅台营业收入同比增长 1.82%。", s)["ok"]


def test_q14_financials_adds_operating_revenue(monkeypatch):
    from stock_agent import research

    class R:
        def __init__(self, data):
            self.data = data

        def json(self):
            return {"result": {"data": self.data}}
    inc = [{"REPORT_DATE": "2026-09-30 00:00:00", "TOTAL_OPERATE_INCOME": 133284e6, "OPERATE_INCOME": 130971e6},
           {"REPORT_DATE": "2025-09-30 00:00:00", "TOTAL_OPERATE_INCOME": 130904e6, "OPERATE_INCOME": 128454e6}]
    monkeypatch.setattr(research, "_get", lambda url, ttl=600, **kw: R(inc))
    rows = [{"报告期": "2026三季报", "营业总收入": "1332.84亿"}]
    research._add_operating_revenue(rows, [{"REPORT_DATE": "2026-09-30 00:00:00"}], "600519")
    assert rows[0]["营业收入"] == "1309.71亿" and abs(rows[0]["营业收入同比%"] - 1.9595) < 0.01


# ---------------------------------------------------------------- Q21：单季 vs 年初至今累计
def _em_fake(kind_rows):
    def f(secu, kind, n=24):
        return kind_rows.get(kind, [])
    return f


def test_q21_quarter_statements_carry_ytd(monkeypatch):
    from stock_agent import fundamentals as F
    from stock_agent import research
    inc = [{"REPORT_DATE": "2026-09-30 00:00:00", "INVEST_INCOME": 5383186000.0, "FAIRVALUE_CHANGE_INCOME": 10806041000.0, "TOTAL_OPERATE_INCOME": 369.09e9},
           {"REPORT_DATE": "2026-06-30 00:00:00", "INVEST_INCOME": 3040102000.0, "FAIRVALUE_CHANGE_INCOME": 4997108000.0, "TOTAL_OPERATE_INCOME": 261.05e9},
           {"REPORT_DATE": "2026-03-31 00:00:00", "INVEST_INCOME": 1041608000.0, "FAIRVALUE_CHANGE_INCOME": 653446000.0, "TOTAL_OPERATE_INCOME": 131.58e9}]
    monkeypatch.setattr(F, "_em_rows", _em_fake({"income": inc, "balance": [{"REPORT_DATE": r["REPORT_DATE"]} for r in inc]}))
    monkeypatch.setattr(research, "resolve", lambda s: {"market": "A", "code": "000333", "name": "美的集团"})
    q = F.compact(F.statements("000333.SZ", "quarter", 2))
    p = q["periods"][0]
    assert p["period"] == "2026Q3 单季" and "单季" in p["basis"]
    assert abs(p["income"]["invest_income"] - 2343084000.0) < 1
    assert p["ytd"]["period"] == "2026前三季度（累计）" and p["ytd"]["income"]["invest_income"] == 5383186000.0
    assert "ytd" in q["note"] and "前三季度" in q["note"] and q["url"].endswith("SZ000333#/cwfx")
    y = F.statements("000333.SZ", "ytd", 1)
    assert y["periods"][0]["period"] == "2026前三季度（累计）" and y["periods"][0]["income"]["fair_value_chg"] == 10806041000.0
    # 回答把单季值说成「前三季度」：按累计核对对不上（这里用通用核对：单季 23.43 亿只在单季口径里出现）
    s = cite.Sources()
    s.add("get_statements", {"symbol": "000333.SZ", "freq": "ytd"}, F.compact(y))
    assert cite.verify("美的集团前三季度投资收益约 53.83 亿元。", s)["ok"]
    assert not cite.verify("美的集团前三季度投资收益约 23.43 亿元。", s)["ok"]


# ---------------------------------------------------------------- Q7：板块资金 502 → 新浪
def test_q7_board_flow_falls_back_to_sina(monkeypatch):
    from stock_agent import eventdata as ed

    def boom(kind):
        raise RuntimeError("502 Server Error: Bad Gateway")
    sina = [{"name": "酿酒行业", "avg_changeratio": "0.034891", "netamount": "5137657900.24", "ts_name": "皇台酒业"},
            {"name": "交通运输", "avg_changeratio": "0.0220578", "netamount": "1594501012.8", "ts_name": "中远海控"},
            {"name": "生物制药", "avg_changeratio": "0.00192647", "netamount": "3009386100.55", "ts_name": "x"},
            {"name": "钢铁行业", "avg_changeratio": "-0.012", "netamount": "-900000000", "ts_name": "y"}]
    monkeypatch.setattr(ed, "BOARD_SOURCES", (("东方财富", boom, {"industry": "u1", "concept": "u1"}, "主力净流入"),
                                              ("新浪财经", ed._boards_sina, {"industry": "u2", "concept": "u2"}, "净流入（流入−流出）")))
    monkeypatch.setattr(ed, "_fetch", lambda url, params=None, headers=None, ttl=300, **kw: sina)
    r = ed.board_flow("industry", 2)
    assert r["source"] == "新浪财经" and r["fallback"] and "502" in r["warnings"][0]
    assert [x["name"] for x in r["top_gainers"]] == ["酿酒行业", "交通运输"] and r["top_gainers"][0]["chg_pct"] == 3.49
    assert [x["name"] for x in r["inflow"]] == ["酿酒行业", "生物制药"] and r["outflow"][0]["name"] == "钢铁行业"
    monkeypatch.setattr(ed, "BOARD_SOURCES", (("东方财富", boom, {"industry": "u1"}, "x"), ("新浪财经", boom, {"industry": "u2"}, "y")))
    r = ed.board_flow("industry")
    assert r.get("error") and len(r["warnings"]) == 2
