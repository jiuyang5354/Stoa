"""回测：未来数据屏蔽（最关键）、打分、基线、端到端。全部离线。"""
import json
import math

import pandas as pd
import pytest

from stock_agent import backtest as bt


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


def ramp(n=200, start="2025-01-01", step=1.0):
    idx = pd.bdate_range(start, periods=n)
    return pd.Series([100 + i * step for i in range(n)], index=idx)


# ---------------------------------------------------------------- 未来数据屏蔽
def test_asof_truncates_strictly():
    s = ramp()
    pit = bt.PointInTime({"X": s})
    cut = s.index[120]
    got = pit.asof("X", cut)
    assert got.index.max() == cut
    assert len(got) == 121
    assert (got.index <= cut).all()
    # 截断日之后的任何一天、任何一个价格都拿不到
    future_vals = set(s.loc[s.index > cut].tolist())
    assert not future_vals & set(got.tolist())


def test_asof_returns_copy_not_view():
    s = ramp()
    pit = bt.PointInTime({"X": s})
    got = pit.asof("X", s.index[50])
    got.iloc[-1] = -999.0
    got.loc[s.index[60]] = 12345.0  # 往副本里塞未来日期也不影响原始数据
    assert pit.asof("X", s.index[50]).iloc[-1] == s.iloc[50]
    assert 12345.0 not in pit.asof("X", s.index[70]).tolist()


def test_asof_on_non_trading_day():
    s = ramp()
    sat = s.index[30] + pd.Timedelta(days=(5 - s.index[30].weekday()) % 7 or 7)
    got = pd.Series(bt.PointInTime({"X": s}).asof("X", sat))
    assert got.index.max() <= sat
    assert got.index.max() == s.index[s.index <= sat].max()


def test_snapshot_rejects_future_rows():
    s = ramp()
    with pytest.raises(AssertionError):
        bt.Snapshot("X", "美股", s.index[10], s.iloc[:20], 5)


def test_agent_only_ever_sees_past():
    """把每次交给 Agent 的快照记下来：最后一天必须等于决策日，且不含任何决策日之后的行。"""
    s = ramp(260)
    pit = bt.PointInTime({"AAA": s})
    seen = []

    def spy(snap):
        seen.append((snap.date, snap.closes.index.max(), len(snap.closes)))
        return {"view": "看多", "confidence": 60, "reason": ""}

    preds = bt.predict(pit, spy, s.index[0], s.index[-1], 5, 5)
    assert preds and len(seen) == len(preds)
    for d, mx, n in seen:
        assert mx == d
        assert n == s.index.get_loc(d) + 1


def test_leak_detector_agent_cannot_beat_chance_on_random_walk():
    """一个「想偷看」的 Agent：只能用传进来的数据。若有泄漏它会在随机游走上接近 100% 命中。"""
    import random
    rng = random.Random(3)
    idx = pd.bdate_range("2024-01-01", periods=600)
    p, vals = 100.0, []
    for _ in idx:
        p *= math.exp(rng.gauss(0, .02))
        vals.append(p)
    s = pd.Series(vals, index=idx)
    pit = bt.PointInTime({"RW": s})

    def peeker(snap):  # 若能看到未来，下一行就是答案
        nxt = s.index.searchsorted(snap.date) + 5
        visible = snap.closes
        assert visible.index.max() == snap.date
        guess = visible.iloc[-1] > visible.iloc[-2]
        return {"view": "看多" if guess else "看空", "confidence": 60, "reason": ""}

    rows = bt.attach_returns(bt.predict(pit, peeker, idx[100], idx[-30], 1, 5), pit, [5], 1)
    m = bt.metrics(rows, [5])
    assert 35 < m["horizons"]["5"]["hit_dir%"] < 65


def test_llm_payload_has_no_future_and_is_blind():
    s = ramp(200)
    snap = bt.Snapshot("NVDA", "美股", s.index[150], s.iloc[:151], 5)
    pl = bt.snapshot_payload(snap, blind=True)
    txt = json.dumps(pl, ensure_ascii=False)
    assert "NVDA" not in txt and f"{s.index[150]:%Y}" not in txt
    assert pl["最近60个交易日收盘"][-1] == 100.0  # 归一化
    raw = bt.snapshot_payload(snap, blind=False)
    assert max(raw["最近60个交易日收盘"]) <= s.iloc[150]  # 单调上升序列：看不到之后更高的价


# ---------------------------------------------------------------- 打分
def test_hit_rules():
    assert bt.hit("看多", 0.1, 1.5) and not bt.hit("看多", -0.1, 1.5)
    assert bt.hit("看空", -2, 1.5) and not bt.hit("看空", 0.0, 1.5)
    assert bt.hit("中性", 1.4, 1.5) and not bt.hit("中性", -1.6, 1.5)
    assert math.isclose(bt.band_for(20, 1.5), 3.0)


def test_forward_and_metrics_known_values():
    s = ramp(100, step=1.0)  # 每天 +1
    pit = bt.PointInTime({"X": s})
    d = s.index[70]
    assert pit.forward("X", d, 5) == (s.index[75], float(s.iloc[75]))
    assert pit.forward("X", s.index[-2], 5) is None
    preds = [{"symbol": "X", "market": "美股", "date": f"{d:%Y-%m-%d}", "close": float(s.iloc[70]), "view": "看多", "confidence": 75, "reason": ""},
             {"symbol": "X", "market": "美股", "date": f"{s.index[75]:%Y-%m-%d}", "close": float(s.iloc[75]), "view": "看空", "confidence": 55, "reason": ""}]
    rows = bt.attach_returns(preds, pit, [5], 5)
    assert math.isclose(rows[0]["ret5"], (175 / 170 - 1) * 100, rel_tol=1e-4)
    m = bt.metrics(rows, [5])
    h = m["horizons"]["5"]
    assert h["hit_dir%"] == 50.0 and h["n_dir"] == 2
    cal = {c["bucket"]: c for c in h["calibration"]}
    assert cal["70-80"]["hit%"] == 100.0 and cal["50-60"]["hit%"] == 0.0
    ls = (1 + (175 / 170 - 1)) * (1 - (180 / 175 - 1)) - 1
    assert math.isclose(m["portfolio"]["long_short%"], ls * 100, abs_tol=1e-2)
    bh = (180 / 170) - 1
    assert math.isclose(m["portfolio"]["buy_hold%"], bh * 100, abs_tol=1e-2)


def test_neutral_counts_in_hit_all_not_dir():
    s = ramp(100, step=0.0)  # 横盘
    pit = bt.PointInTime({"X": s})
    preds = [{"symbol": "X", "market": "A股", "date": f"{s.index[70]:%Y-%m-%d}", "close": 100.0, "view": "中性", "confidence": 50, "reason": ""}]
    m = bt.metrics(bt.attach_returns(preds, pit, [5], 5), [5])
    assert m["horizons"]["5"]["hit_dir%"] is None and m["horizons"]["5"]["hit_all%"] == 100.0
    assert m["portfolio"]["long_short%"] == 0.0


def test_sleeves_do_not_double_count():
    a = [("2025-01-03", 10.0), ("2025-01-10", 21.0)]
    b = [("2025-01-06", -10.0), ("2025-01-13", 0.0)]
    out = bt.sleeves([a, b])
    assert out[-1] == ("2025-01-13", 10.5)


def test_parse_view():
    assert bt.parse_view('好的 {"view": "看涨", "confidence": 140, "reason": "x"}') == {"view": "看多", "confidence": 100, "reason": "x"}
    with pytest.raises(ValueError):
        bt.parse_view('{"view": "大涨"}')


# ---------------------------------------------------------------- 基线
def test_baselines():
    up = ramp(100, step=1.0)
    down = ramp(100, step=-0.5)
    snap_up = bt.Snapshot("X", "美股", up.index[-1], up, 5)
    snap_dn = bt.Snapshot("X", "美股", down.index[-1], down, 5)
    assert bt.momentum_baseline(snap_up)["view"] == "看多"
    assert bt.momentum_baseline(snap_dn)["view"] == "看空"
    assert bt.rule_agent(snap_up)["view"] == "看多" and bt.rule_agent(snap_dn)["view"] == "看空"
    assert bt.long_baseline(snap_dn)["view"] == "看多"
    r = bt.random_baseline(7)
    assert r(snap_up) == r(snap_up)  # 可复现
    views = [r(bt.Snapshot("X", "美股", d, up.loc[:d], 5))["view"] for d in up.index[60:]]
    assert 0.25 < views.count("看多") / len(views) < 0.75


def test_market():
    assert bt.market("600519.SS") == "A股" and bt.market("000858.SZ") == "A股"
    assert bt.market("0700.HK") == "港股" and bt.market("^HSI") == "港股"
    assert bt.market("AAPL") == "美股"


# ---------------------------------------------------------------- LLM 缓存 / 端到端
class FakeLLM:
    ready = True

    def __init__(self):
        self.n = 0

    def __repr__(self):
        return "fake:1"

    def chat(self, messages, tools=None, temperature=0, timeout=0):
        self.n += 1
        assert tools is None  # 回测模式不给任何工具
        body = json.loads(messages[-1]["content"])
        last = body["最近60个交易日收盘"]
        return {"content": json.dumps({"view": "看多" if last[-1] >= last[-20] else "看空", "confidence": 65, "reason": "t"})}


def test_llm_agent_cache_and_cap(tmp_path):
    closes = bt.synthetic_prices(["AAA", "0700.HK"], "2025-01-01", "2025-06-30")
    fake = FakeLLM()
    r1 = bt.run(symbols=list(closes), start="2025-01-01", end="2025-06-30", agent="llm", llm=fake, closes=closes, concurrency=3, max_calls=1000)
    n1 = fake.n
    assert n1 > 0 and r1["llm"]["calls"] == n1 and r1["llm"]["cache_hits"] == 0
    r2 = bt.run(symbols=list(closes), start="2025-01-01", end="2025-06-30", agent="llm", llm=fake, closes=closes, concurrency=3)
    assert fake.n == n1 and r2["llm"]["cache_hits"] == n1  # 同一时点同一标的同一模型不重复调用
    fake2 = FakeLLM()
    fake2.__class__.__repr__ = lambda self: "fake:2"
    r3 = bt.run(symbols=list(closes), start="2025-01-01", end="2025-06-30", agent="llm", llm=fake2, closes=closes, max_calls=3, concurrency=1)
    assert r3["llm"]["calls"] == 3 and r3["llm"]["skipped_cap"] > 0


def test_end_to_end_synthetic_writes_json():
    res = bt.run(symbols=["AAA", "600000.SS", "0005.HK"], start="2025-01-01", end="2025-12-31", synthetic=True)
    assert res["params"]["data"] == "synthetic"
    for k in ("agent", "random", "always_long", "momentum20"):
        assert set(res["results"][k]) >= {"全部", "A股", "港股", "美股"}
    saved = bt.load(res["id"])
    assert saved and saved["headline"]["n"] > 0
    assert bt.listing()[0]["id"] == res["id"]


# ---- 第二轮：事后校准不偷看未来、显著性
def _row(d, conf, view, ret, exit_):
    return {"symbol": "X", "market": "美股", "date": d, "close": 100, "view": view, "confidence": conf, "ret5": ret, "exit5": exit_}


def test_calibrate_uses_only_resolved_past():
    from stock_agent import backtest as bt
    rows = [_row(f"2026-01-{i:02d}", 70, "看多", 1.0, f"2026-01-{i + 5:02d}") for i in range(1, 11)]
    a = bt.calibrate(rows, [5])
    # 第 1–5 天决策时还没有任何结果揭晓 → 先验 50
    assert all(r["pcal5"] == 50 and r["pcal5_n"] == 0 for r in a[:5])
    # 第 6 天：只有第 1 天（01-06 揭晓）可用
    assert a[5]["pcal5_n"] == 1 and a[5]["pcal5"] > 50
    # 改动「未来」结果，不影响之前的校准值
    rows2 = [dict(r) for r in rows]
    for r in rows2[5:]:
        r["ret5"] = -5.0
    b = bt.calibrate(rows2, [5])
    assert [r["pcal5"] for r in a[:6]] == [r["pcal5"] for r in b[:6]]
    assert rows[0].get("pcal5") is None  # 不改原列表


def test_calibrate_learns_overconfidence():
    from stock_agent import backtest as bt
    rows = []
    for i in range(80):  # 自报 75 但只对一半，校准后应接近 50
        d = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        rows.append(_row(d, 75, "看多", 1.0 if i % 2 else -1.0, d))
    a = bt.calibrate(rows, [5])
    assert abs(a[-1]["pcal5"] - 50) < 6
    st = bt._calib_stats(a, 5, 1.5)
    assert st["brier_cal"] < st["brier_raw"]


def test_binom_and_boot():
    from stock_agent import backtest as bt
    assert bt._binom_p(5, 10) > .5 and bt._binom_p(60, 100) < .05
    ci = bt._boot_ci([(f"d{i % 30}", i % 2 == 0) for i in range(300)])
    assert ci[0] < 50 < ci[1]


def test_run_ids_same_second_do_not_collide(monkeypatch):
    class Fixed(bt.datetime):
        @classmethod
        def now(cls, tz=None):
            return bt.datetime(2026, 10, 30, 1, 56, 26, tzinfo=tz)
    monkeypatch.setattr(bt, "datetime", Fixed)
    a, b = bt.new_run_id("debate"), bt.new_run_id("debate")
    assert a == "20261030-015626-debate" and b == "20261030-015626-debate-2"
    assert json.loads((bt.bt_dir() / f"{b}.json").read_text(encoding="utf-8"))["status"] == "running"
