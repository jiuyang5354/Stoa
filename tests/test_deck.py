"""v0.37 P3：write_file(format="pptx") 8 页投研模板——8 页齐、数字与输入一致（python-pptx 读回）、缺页不编数、从本机辩论纪要/账本补齐。"""
import json

import pytest

pytest.importorskip("pptx")
from pptx import Presentation  # noqa: E402

from stock_agent import deck as dk  # noqa: E402
from stock_agent import exports  # noqa: E402


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


DECK = {"symbol": "000333.SZ", "name": "美的集团", "date": "2026-10-30", "title": "美的集团（000333.SZ）投研简报",
 "conclusion": {"view": "看多", "confidence": 62, "horizon_days": 20, "reasons": ["前三季度营业总收入 3690.87 亿元，同比 +1.20%，增速放缓但规模稳", "归母净利润 413.93 亿元，同比 +9.27%，净利率升至 11.30%", "扣非净利润 287.88 亿元低于上年同期，非经常收益占比上升需跟踪"]},
 "price": {"source": "腾讯证券 · 2026-10-30 收盘；东方财富 F10", "metrics": [["现价（元）", 78.35, "2026-10-30 收盘"], ["52 周区间", "62.10–86.20"], ["市盈率 TTM", 13.42, "近 5 年 38% 分位"], ["股息率", "4.6%"], ["一致预期目标价", "92.50", "27 家机构均值"], ["市净率", 2.61]],
           "history": [["2026-08-29", 72.1], ["2026-09-12", 74.6], ["2026-09-26", 76.0], ["2026-10-17", 75.2], ["2026-10-30", 78.35]]},
 "financials": {"source": "东方财富 F10 原始报表（年报口径，亿元）", "columns": ["指标", "2023", "2024", "2025"], "rows": [["营业总收入", 3737.10, 4090.84, 4585.02], ["营收同比%", 8.10, 9.47, 12.08], ["归母净利润", 337.20, 385.37, 431.20], ["毛利率%", 26.48, 26.41, 25.97]]},
 "waterfall": {"period": "2026 前三季度（累计）", "items": [["营业总收入", 369087000000], ["营业成本", -275102000000], ["销售费用", -32150000000], ["管理费用", -11820000000], ["研发费用", -13050000000], ["其他与税", -1550000000], ["归母净利润", 35415000000]], "last_total": True},
 "debate": {"bull": ["海外收入占比提升，B 端业务增速高于 C 端", "分红率超 60%，股息率 4.6%"], "bear": ["扣非净利润同比下滑，利润靠公允价值变动", "地产链需求弱，内销承压"], "verdict": "中期看多，但仓位不宜重", "risk": "风控：通过；单票仓位上限 10%"},
 "risks": [{"level": "风控", "text": "非经常性收益占比升至 30% 以上"}, {"level": "事件", "text": "11 月中旬限售股解禁"}, {"level": "空方", "text": "原材料价格反弹压缩毛利"}],
 "peers": {"source": "sweep_filings · 2025 年报 · 经营情况讨论与分析", "columns": ["代码", "名称", "年份", "营收", "毛利率", "渠道库存表述", "页码链接", "缺失"],
           "rows": [["000333.SZ", "美的集团", 2025, 4585.0, 25.97, "「渠道库存处于健康水平」", "p.23", ""], ["000651.SZ", "格力电器", 2025, 1980.4, 30.1, "「终端库存较去年下降」", "p.31", ""], ["600690.SS", "海尔智家", 2025, 2860.2, 28.4, "", "p.19", "表述"]], "coverage": {"requested": 3, "fetched": 3, "failed": []}},
 "invalidation": {"conditions": ["收盘跌破 60 日均线 73.80 元", "四季度扣非净利润同比降幅超过 15%", "海外收入增速转负"], "review_date": "2026-11-27"},
 "sources": ["东方财富 F10 财务分析 https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code=SZ000333#/cwfx", "巨潮资讯 2026 年三季报 第 3 页"]}


def _deck():
    return json.loads(json.dumps(DECK))


def test_pptx_8_pages_numbers_match_input(tmp_path):
    r = exports.write("美的投研", "pptx", deck=_deck())
    assert r["ok"] and r["file"].endswith(".pptx") and r["info"].startswith("8 页") and "空页" not in r["info"]
    p = exports.resolve(r["file"])
    assert exports.ctype(p).endswith("presentationml.presentation")
    prs = Presentation(str(p))
    assert len(prs.slides) == 8
    assert [s.name for s in prs.slides][1:] == dk.TITLES[1:]
    chk = dk.verify(p, _deck())
    assert chk["ok"] and chk["waterfall_ok"] and not chk["missing"]
    text = "\n".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    cells = [c.text for s in prs.slides for sh in s.shapes if sh.has_table for row in sh.table.rows for c in row.cells]
    for v in ("看多", "置信度 62%", "78.35", "13.42", "2026-11-27", "中期看多，但仓位不宜重", "收盘跌破 60 日均线 73.80 元"):
        assert v in text, v
    for v in ("4,585.02", "385.37", "非经常性收益占比升至 30% 以上", "格力电器", "「终端库存较去年下降」", "2025"):
        assert v in cells, v
    assert "页码链接" not in cells and "2,025" not in cells
    # 瀑布：图表数据就是输入（亿元）
    charts = [sh.chart for s in prs.slides for sh in s.shapes if sh.has_chart]
    wf = next(c for c in charts if len(c.plots[0].series) == 4)
    cats = list(wf.plots[0].categories)
    assert cats[0] == "营业总收入" and cats[-1] == "归母净利润"
    tot = list(wf.plots[0].series[1].values)
    assert tot[0] == pytest.approx(3690.87) and tot[-1] == pytest.approx(354.15)
    assert list(wf.plots[0].series[3].values)[1] == pytest.approx(2751.02)  # 营业成本：减少


def test_verify_catches_mismatch(tmp_path):
    d = _deck()
    p = tmp_path / "x.pptx"
    built = dk.build(d, p)
    bad = json.loads(json.dumps(built["deck"]))
    bad["price"]["metrics"][0][1] = 81.2
    bad["waterfall"]["items"][1][1] = -2.0e11
    chk = dk.verify(p, bad)
    assert not chk["ok"] and "81.20" in chk["missing"] and any("瀑布" in m for m in chk["missing"])


def test_missing_sections_are_not_invented():
    r = exports.write("空白", "pptx", deck={"name": "某公司", "conclusion": {"view": "震荡", "confidence": 55, "reasons": ["只有一条依据"]}})
    assert r["ok"] and "8 页" in r["info"] and "空页" in r["info"]
    prs = Presentation(str(exports.resolve(r["file"])))
    assert len(prs.slides) == 8
    txt = [" ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in prs.slides]
    assert all(dk.EMPTY in t for t in txt[1:]) and dk.EMPTY not in txt[0]


def test_waterfall_tie_out_warning():
    d = _deck()
    d["waterfall"]["items"][-1][1] = 40000000000  # 末项和中间增减对不上
    r = exports.write("勾稽", "pptx", deck=d)
    assert r["ok"] and "利润瀑布首项加中间增减与末项差" in r["info"]


def test_autofill_from_debate_and_ledger():
    from stock_agent.debate import debates_dir
    from stock_agent.memory import Memory
    rec = {"id": "d1", "created": "2026-10-30 15:10", "mode": "live", "symbol": "600519.SS", "name": "贵州茅台",
           "verdict": {"view": "看多", "confidence": 64, "horizon_days": 20, "summary": "盈利质量高[2]", "key_points": ["ROE 24.47%[2]", "毛利率 89.58%[2]"],
                       "invalidation": "收盘跌破 1300 元"},
           "debate": [{"bull": {"status": "ok", "points": ["品牌壁垒"]}, "bear": {"status": "ok", "points": ["增速放缓"]}}],
           "risk": {"decision": "通过", "max_position_pct": 10}, "risks": [{"level": "空方", "text": "批价下行[3]"}]}
    (debates_dir() / "d1.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    Memory().record_call("600519.SS", "贵州茅台", "看涨", 1392.05, 20, "Agora 辩论 d1", due="2026-11-27")
    r = exports.write("茅台", "pptx", deck={"symbol": "600519.SS", "name": "贵州茅台"})
    assert r["ok"] and "由本机记录补入" in r["info"]
    prs = Presentation(str(exports.resolve(r["file"])))
    txt = "\n".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    cells = " ".join(c.text for s in prs.slides for sh in s.shapes if sh.has_table for row in sh.table.rows for c in row.cells)
    assert "看多" in txt and "ROE 24.47%" in txt and "[2]" not in txt and "品牌壁垒" in txt and "增速放缓" in txt
    assert "批价下行" in cells and "收盘跌破 1300 元" in txt and "2026-11-27" in txt


def test_agent_write_file_pptx_tool():
    from stock_agent import agent
    tool = next(t for t in agent.TOOLS if t["name"] == "write_file")
    assert "pptx" in tool["parameters"]["properties"]["format"]["enum"] and "deck" in tool["parameters"]["properties"]
    assert exports.write("x", "pptx")["error"].startswith("pptx 要传 deck")


def test_deck_intent_notes_only_when_asked():
    from stock_agent import toolsel
    assert "deck" in toolsel.intent_groups("把茅台做成一份投研PPT") and "deck" in toolsel.intent_groups("make slides for NVDA")
    assert "deck" not in toolsel.intent_groups("茅台今天涨了多少？")
    assert "waterfall" in toolsel.notes(["deck"])
