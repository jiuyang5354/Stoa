from datetime import date

import pandas as pd
import pytest

from stock_agent import indicators, judgments, macrocal, report


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    from stock_agent.memory import Memory
    return Memory(tmp_path)


def _series(vals, end="2026-10-30"):
    idx = pd.bdate_range(end=end, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


def _snap(rows):
    return {"indices": {"A股": [{"symbol": "000001.SS", "name": "上证指数", "data": rows.get("000001.SS", {})}]},
            "watchlist": [{"symbol": s, "name": n, "data": d} for s, (n, d) in rows.items() if s != "000001.SS"]}


UP = indicators.analyze(_series([100 + i * 0.5 for i in range(80)]), {})
DOWN = indicators.analyze(_series([140 - i * 0.5 for i in range(80)]), {})
FLAT = indicators.analyze(_series([100 + (1 if i % 2 else -1) for i in range(80)]), {})


def test_judge_views_and_flip():
    j = judgments.judge("NVDA", "英伟达", UP, today=date(2026, 10, 30), days=5)
    assert j["view"] == "看涨" and j["flip_down"] and j["flip_down"] < j["price"] and j["flip_up"] is None
    assert "跌破" in j["flip"] and j["due"] == "2026-11-06" and "美东" in j["expires"]
    assert 50 <= j["confidence"] <= judgments.CONF_CAP
    d = judgments.judge("600519.SS", "茅台", DOWN, today=date(2026, 10, 30))
    assert d["view"] == "看跌" and d["flip_up"] > d["price"] and "站上" in d["flip"] and "15:00 北京时间" in d["expires"]
    f = judgments.judge("0700.HK", "腾讯", FLAT, today=date(2026, 10, 30))
    assert f["view"] == "震荡" and f["flip_up"] > f["price"] > f["flip_down"]
    assert judgments.judge("X", "x", {}) is None


def test_volume_profile_pulls_to_range():
    vp = {"state": "平衡", "regime": "震荡", "val": UP["close"] - 3, "vah": UP["close"] + 1, "poc": UP["close"] - 1}
    near = dict(UP, ma20=UP["close"] * 0.99, ma60=UP["close"] * 0.97)
    j = judgments.judge("NVDA", "英伟达", near, vp, today=date(2026, 10, 30))
    assert j["view"] == "震荡" and "成交分布" in j["reason"]


def test_morning_records_and_carries(mem):
    snap = _snap({"NVDA": ("英伟达", UP), "600519.SS": ("茅台", DOWN)})
    js = judgments.morning(mem, snap, today=date(2026, 10, 30))
    assert len(js) == 2 and not any(j["carried"] for j in js)
    calls = mem.calls_where("source='morning'")
    assert {c["view"] for c in calls} == {"看涨", "看跌"} and all(c["flip"] and c["expires"] for c in calls)
    js2 = judgments.morning(mem, snap, today=date(2026, 11, 2))  # 下一个交易日：同方向沿用，不重复记
    assert all(j["carried"] for j in js2) and len(mem.calls_where("source='morning'")) == 2
    # 方向变了：旧的标「翻转」，记新的
    js3 = judgments.morning(mem, _snap({"NVDA": ("英伟达", DOWN), "600519.SS": ("茅台", DOWN)}), today=date(2026, 11, 3))
    nv = [c for c in mem.calls_where("symbol='NVDA'")]
    assert len(nv) == 2 and nv[0]["review"] == "翻转" and nv[1]["view"] == "看跌"
    assert [j for j in js3 if j["symbol"] == "NVDA"][0]["carried"] is False


def test_no_watchlist_uses_indices(mem):
    js = judgments.morning(mem, {"indices": {"A股": [{"symbol": "000001.SS", "name": "上证指数", "data": UP}]}, "watchlist": []},
                           today=date(2026, 10, 30))
    assert js[0]["symbol"] == "000001.SS"


def test_evening_review_outcomes(mem):
    today = date(2026, 10, 30)
    snap = _snap({"NVDA": ("英伟达", dict(UP, date="2026-10-29")), "600519.SS": ("茅台", dict(DOWN, date="2026-10-29")),
                  "0700.HK": ("腾讯", dict(FLAT, date="2026-10-29")), "AAPL": ("苹果", dict(UP, date="2026-10-29"))})
    js = {j["symbol"]: j for j in judgments.morning(mem, snap, today=today)}
    ev = _snap({
        "NVDA": ("英伟达", dict(UP, close=js["NVDA"]["flip_down"] - 1, date="2026-10-30")),  # 跌破翻转价 → 失效
        "600519.SS": ("茅台", dict(DOWN, close=js["600519.SS"]["price"] * 0.99, date="2026-10-30")),  # 看跌且下跌 → 站住
        "0700.HK": ("腾讯", dict(FLAT, close=js["0700.HK"]["price"] * 1.012, date="2026-10-30")),  # 震荡偏离 1.2% 未破 → 承压
        "AAPL": ("苹果", dict(UP, date="2026-10-29")),  # 美股还没开盘：没有新收盘 → 待定
    })
    rs = {r["symbol"]: r for r in judgments.review(mem, ev, today)}
    assert rs["NVDA"]["result"] == "失效" and "跌破" in rs["NVDA"]["note"]
    assert rs["600519.SS"]["result"] == "站住"
    assert rs["0700.HK"]["result"] == "承压"
    assert rs["AAPL"]["result"] == "待定"
    assert mem.calls_where("symbol='NVDA'")[0]["review"] == "失效"
    md = "\n".join(judgments.render_review(list(rs.values())))
    assert "判断复盘" in md and "失效 1" in md and "站住 1" in md
    # 失效的判断第二天不再沿用：早报重新给
    js2 = {j["symbol"]: j for j in judgments.morning(mem, snap, today=date(2026, 11, 2))}
    assert js2["NVDA"]["carried"] is False and js2["600519.SS"]["carried"] is True


def test_render_morning_and_report_sections():
    j = judgments.judge("NVDA", "英伟达", UP, today=date(2026, 10, 30))
    lines = judgments.render_morning([{**j, "carried": False}])
    md = report.render({"indices": {}, "watchlist": []}, "morning", None, lines, late="原定 08:30，补发于 10:05")
    assert "今日判断" in md and "失效时间" in md and "翻转条件" in md
    assert md.startswith("# 股市早报（补发）") and "补发：原定 08:30" in md


def test_macro_calendar_official_and_rules():
    evs = macrocal.events(date(2026, 11, 5), date(2026, 11, 10))
    names = {(e["kind"], e["date"]) for e in evs}
    assert ("us_nfp", "2026-11-06") in names and ("us_cpi", "2026-11-10") in names
    nfp = next(e for e in evs if e["kind"] == "us_nfp")
    assert nfp["time_bj"] == "11-06 21:30" and not nfp["approx"] and "bls.gov" in nfp["source"]
    fomc = macrocal.events(date(2026, 12, 9), date(2026, 12, 10))
    f = next(e for e in fomc if e["kind"] == "fomc")
    assert f["date"] == "2026-12-10" and f["time_bj"].endswith("03:00")  # 美东 14:00 = 北京次日 03:00（冬令时）
    pmi = [e for e in macrocal.events(date(2026, 11, 30), date(2026, 11, 30)) if e["kind"] == "cn_pmi"]
    assert pmi and pmi[0]["approx"]
    ov = macrocal.events(date(2026, 11, 9), date(2026, 11, 11), [{"date": "2026-11-11", "kind": "us_cpi", "time": "21:30", "name": "美国 CPI（改期）"}])
    cpi = [e for e in ov if e["kind"] == "us_cpi"]
    assert len(cpi) == 1 and cpi[0]["name"] == "美国 CPI（改期）"
    assert macrocal.lookahead(date(2026, 11, 6)) == date(2026, 11, 9)


def test_macro_scenarios_three_cases_and_reaction():
    ev = macrocal.events(date(2026, 11, 10), date(2026, 11, 10))[0]
    idx = pd.bdate_range("2025-01-01", "2026-11-09")
    import random
    rnd = random.Random(2)
    vals, p = [], 100.0
    for d in idx:
        p *= 1 + rnd.gauss(0, 0.01)
        vals.append(p)
    closes = {"NVDA": pd.Series(vals, index=idx), "600519.SS": pd.Series(vals, index=idx)}
    sc = macrocal.scenarios(ev, [{"symbol": "NVDA", "name": "英伟达"}, {"symbol": "600519.SS", "name": "茅台"}], closes, date(2026, 11, 10))
    assert [s["label"] for s in sc["scenarios"]] == ["偏热", "符合", "偏冷"]
    hot = sc["scenarios"][0]
    assert hot["markets"]["美股"]["dir"] == "偏利空" and hot["holdings"][0]["dir"] == "偏利空"
    assert sc["reaction"] and sc["reaction"][0]["n"] >= 4
    md = "\n".join(macrocal.render([sc]))
    assert "重大数据前瞻" in md and "偏冷" in md and "bls.gov" in md
    f = macrocal.scenarios({**ev, "kind": "fomc", "name": "FOMC"}, [], {}, date(2026, 11, 10))
    assert f["scenarios"][0]["label"].startswith("偏热")


def test_session_sections_morning_and_evening(mem, monkeypatch):
    from stock_agent import __main__ as M, volprofile
    monkeypatch.setattr(volprofile, "fetch_bars", lambda *a, **k: (_ for _ in ()).throw(LookupError("offline")))
    monkeypatch.setattr(M.data, "fetch_closes", lambda syms, *a, **k: {})
    snap = _snap({"NVDA": ("英伟达", UP)})
    out = M.session_sections("morning", snap, {}, mem, {})
    assert any("今日判断" in l for l in out) and snap["judgments"][0]["view"] == "看涨"
    out2 = M.session_sections("evening", snap, {}, mem, {})
    assert any("判断复盘" in l for l in out2)
