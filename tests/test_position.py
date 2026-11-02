"""v0.40 position_limit：确定性仓位上限（mock 日线，不联网）。"""
import math

import pytest

from stock_agent import cite, position as P, toolsel
from stock_agent.agent import TOOLS

CONF = {k: (dict(v) if isinstance(v, dict) else v) for k, v in P.DEFAULTS.items()}


def bars(n=30, close=100.0, step=0.0, rng=2.0, vol=1_000_000):
    """每天收盘 close+i*step，高低 ±rng/2，成交量 vol 股。"""
    out = []
    for i in range(n):
        c = close + i * step
        out.append({"t": f"2026-10-{i + 1:02d}", "o": c, "h": c + rng / 2, "l": c - rng / 2, "c": c, "v": vol})
    return out


def test_atr_and_vol_and_adv():
    b = bars()
    assert P.atr(b) == pytest.approx(2.0)
    assert P.vol20(b) == pytest.approx(0.0)
    assert P.adv20(b) == pytest.approx(100.0 * 1_000_000)
    assert P.adv20(b, 100) == pytest.approx(100.0 * 1_000_000 * 100)  # 腾讯 A股成交量单位是手


def test_three_constraints_min_wins_and_a_share_lot():
    # 资金 100 万、稳健：单票 20% = 20 万；ADV 1 亿 × 1% = 100 万；止损 = 100 − 2×2 = 96，每股亏 4 → 1 万 / 4 = 2500 股 = 25 万
    r = P.compute("600000.SS", 1_000_000, "稳健", bars=bars(), meta={"source": "测试源"}, conf=CONF)
    names = {c["name"]: c for c in r["constraints"]}
    assert names["单票上限"]["amount"] == 200000 and names["流动性"]["amount"] == 1000000 and names["风险预算"]["amount"] == 250000
    assert r["binding"] == "单票上限" and r["final"] == {"shares": 2000, "amount": 200000.0, "pct": 20.0}
    assert r["stop_ref"] == 96 and r["max_loss"] == {"amount": 8000.0, "pct_of_capital": 0.8}
    assert r["cash_left"] == 800000 and r["lot"] == 100 and "不是买入建议" in r["note"]


def test_conservative_risk_budget_binds_and_rounds_down_to_lot():
    # 保守：单票 10% = 10 万；ATR 8，止损 100 − 1.5×8 = 88，每股亏 12 → 0.5% × 100 万 / 12 = 416.7 股 → 按手 400
    r = P.compute("000001.SZ", 1_000_000, "保守", bars=bars(rng=8), conf=CONF)
    assert r["binding"] == "风险预算" and r["final"]["shares"] == 400
    assert r["max_loss"]["amount"] == pytest.approx(4800)


def test_user_stop_pct_overrides_atr():
    r = P.compute("AAPL", 100_000, "积极", stop_pct=10, bars=bars(), conf=CONF)
    assert r["stop_ref"] == 90 and r["stop_method"].startswith("用户给的止损")
    # 积极：单票 30% = 3 万；风险预算 2% × 10 万 / 10 × 100 = 2 万 → 风险预算起作用；美股 1 股取整
    assert r["binding"] == "风险预算" and r["final"]["shares"] == 200 and r["lot"] == 1


def test_liquidity_binds_for_thin_stock():
    r = P.compute("600000.SS", 10_000_000, "积极", bars=bars(vol=10_000), conf=CONF)  # ADV 100 万 × 1% = 1 万
    assert r["binding"] == "流动性" and r["final"]["shares"] == 100
    liq = [c for c in r["constraints"] if c["name"] == "流动性"][0]
    assert liq["amount"] == 10000


def test_hk_lot_unknown_is_explained():
    r = P.compute("0700.HK", 500_000, bars=bars(), conf=CONF)
    assert r["lot"] == 1 and "每手股数" in r["lot_note"] and r["currency"] == "HKD"


def test_one_lot_above_cap_says_so():
    r = P.compute("600519.SS", 50_000, "稳健", bars=bars(close=1400.0, rng=30), conf=CONF)
    assert r["final"]["shares"] == 0 and "买不了一手" in r["final"]["note"]


def test_held_shares_reduce_room():
    r = P.compute("600000.SS", 1_000_000, "稳健", held=1500, bars=bars(), conf=CONF)
    assert r["final"]["shares"] == 500 and r["held"] == 1500


def test_no_capital_gives_liquidity_only():
    r = P.compute("NVDA", None, bars=bars(), conf=CONF)
    assert [c["name"] for c in r["constraints"]] == ["流动性"] and "给出可用资金" in r["note_capital"]
    assert r["final"]["pct"] is None


def test_not_enough_bars_and_risk_level_parsing():
    assert "不足" in P.compute("AAPL", 1e5, bars=bars(n=5), conf=CONF)["error"]
    assert P.level("aggressive") == "积极" and P.level("乱写") == "稳健" and P.level("保守") == "保守"


def test_config_overrides(monkeypatch):
    import stock_agent.agent as A
    monkeypatch.setattr(A, "load_cfg", lambda: {"risk": {"max_weight": {"稳健": 0.05}, "adv_pct": 0.02}})
    c = P.cfg()
    assert c["max_weight"]["稳健"] == 0.05 and c["max_weight"]["保守"] == 0.10 and c["adv_pct"] == 0.02


def test_registered_as_recomputable_source_and_numbers_verify():
    r = P.compute("600000.SS", 1_000_000, "稳健", bars=bars(), meta={"source": "Yahoo Finance", "url": "https://finance.yahoo.com/quote/600000.SS/history"}, conf=CONF)
    s = cite.Sources()
    out = s.add("position_limit", {"symbol": "600000.SS", "capital": 1_000_000}, r)
    it = s.get(out["_cite"])
    assert it["name"].startswith("仓位上限测算 · 600000.SS") and it["url"].endswith("/history") and it["as_of"] == r["as_of"]
    text = "按稳健偏好，单票上限 200000 元，止损参考 96，该止损下最多亏 8000 元。"
    v = cite.verify(text, s)
    assert v["ok"] and not v["unverified"]
    assert "345678" in cite.verify("单票上限 345678 元。", s)["unverified"]  # 编的上限抓得出来


def test_tool_wired_and_selected_only_with_capital_words():
    assert "position_limit" in {t["name"] for t in TOOLS}
    assert "position_limit" in toolsel.DOMAIN_INDEX
    pick = lambda q: {t["name"] for t in toolsel.select(q, TOOLS)[0]}
    assert "position_limit" in pick("我有 20 万，茅台能买多少？")
    assert "position_limit" in pick("腾讯仓位上限多少合适")
    assert "position_limit" not in pick("茅台今天涨了多少？")
    spec = [t for t in TOOLS if t["name"] == "position_limit"][0]
    assert len(spec["description"]) < 100  # 工具描述保持短


# ---------------- v0.40.1：随机不变量 + 脏参数 ----------------

def _rand_bars(rnd, n=30):
    c = rnd.uniform(1, 3000)
    out = []
    for i in range(n):
        c = max(0.2, c * (1 + rnd.gauss(0, rnd.uniform(0.005, 0.06))))
        rng = c * rnd.uniform(0.001, 0.05)
        out.append({"t": f"2026-09-{i + 1:02d}", "o": c, "h": c + rng, "l": c - rng, "c": c, "v": rnd.uniform(0, 2e7)})
    return out


def test_invariants_hold_on_random_market_conditions():
    """5000 组随机行情/资金/风险档位：六条不变量一条都不许破。"""
    import random
    rnd = random.Random(20261102)
    for _ in range(5000):
        sym = rnd.choice(["600000.SS", "000001.SZ", "0700.HK", "NVDA", "688111.SS"])
        cap = rnd.choice([None, 10_000, 100_000, 1_000_000, 50_000_000])
        risk = rnd.choice(["保守", "稳健", "积极"])
        r = P.compute(sym, cap, risk, rnd.choice([None, 3, 8, 15]), rnd.choice([None, 100, 5000]),
                      bars=_rand_bars(rnd), meta={"source": "测试"}, conf=CONF)
        if "error" in r:
            continue
        lot, sh, amt = r["lot"], r["final"]["shares"], r["final"]["amount"]
        w = CONF["max_weight"][r["risk"]]
        assert sh >= 0 and sh % lot == 0, (sym, sh, lot)
        assert r["binding"] in {c["name"] for c in r["constraints"]}
        if cap:
            assert amt <= cap * w + 1e-6, (sym, amt, cap * w)
            assert r["cash_left"] >= -1e-6
            if "max_loss" in r:
                assert r["max_loss"]["amount"] <= cap * CONF["risk_per_trade"][r["risk"]] + 1e-6
        if r["adv20"]:
            assert amt <= r["adv20"] * CONF["adv_pct"] + 1e-6, (sym, amt, r["adv20"])


@pytest.mark.parametrize("kw", [
    {"capital": float("inf")}, {"capital": float("nan")}, {"held": float("inf")},
    {"capital": "100000"}, {"capital": -5}, {"stop_pct": float("nan")}, {"stop_pct": -3},
])
def test_dirty_parameters_never_crash(kw):
    """模型可能传 Infinity / NaN / 字符串：只能当成没给，不能抛异常。"""
    r = P.compute("600000.SS", kw.get("capital"), "稳健", kw.get("stop_pct"), kw.get("held"),
                  bars=bars(), meta={"source": "测试"}, conf=CONF)
    assert "error" not in r, r
    assert r["final"]["shares"] >= 0 and r["final"]["shares"] % r["lot"] == 0


def test_numeric_string_capital_is_used():
    assert P.pos_num("100,000") == 100000 and P.pos_num("abc") is None and P.pos_num(0) is None
    a = P.compute("600000.SS", "100000", bars=bars(), meta={"source": "测试"}, conf=CONF)
    b = P.compute("600000.SS", 100000, bars=bars(), meta={"source": "测试"}, conf=CONF)
    assert a["final"] == b["final"] and a["binding"] == b["binding"]
