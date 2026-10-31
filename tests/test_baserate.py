"""v0.35 历史触达率底数：ATR 归一、判断成立按到期收盘、失效价按盘中触及；账本写入、早报显示、命中率 vs 底数。全部离线。"""
import math
import random
from datetime import date

import pandas as pd
import pytest

from stock_agent import baserate as br
from stock_agent import indicators, judgments


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    from stock_agent.memory import Memory
    return Memory(tmp_path)


def walk(n=320, seed=1, drift=0.0, vol=0.015, start=100.0):
    rnd = random.Random(seed)
    out, c = [], start
    for _ in range(n):
        r = rnd.gauss(drift, vol)
        o = c
        c = max(0.5, c * math.exp(r))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append({"t": "", "o": o, "h": hi, "l": lo, "c": c, "v": 1})
    return out


def test_rates_bounds_and_sample():
    bars = walk()
    c = bars[-1]["c"]
    r = br.rates(bars, "看涨", c, 5, flip_down=c * 0.97)
    assert r and 0 <= r["base_rate"] <= 100 and 0 <= r["flip_rate"] <= 100
    assert r["n"] == br.LOOKBACK and r["atr"] > 0 and "ATR14" in r["method"]
    # 震荡：两头任一触及
    z = br.rates(bars, "震荡", c, 5, flip_up=c * 1.03, flip_down=c * 0.97)
    assert z and "k_up" in z and "k_down" in z


def test_flip_further_is_rarer():
    bars = walk(seed=3)
    c = bars[-1]["c"]
    near = br.rates(bars, "看涨", c, 5, flip_down=c * 0.99)["flip_rate"]
    far = br.rates(bars, "看涨", c, 5, flip_down=c * 0.90)["flip_rate"]
    assert near > far
    longer = br.rates(bars, "看涨", c, 20, flip_down=c * 0.95)["flip_rate"]
    shorter = br.rates(bars, "看涨", c, 3, flip_down=c * 0.95)["flip_rate"]
    assert longer > shorter


def test_trend_raises_bull_base():
    up = walk(seed=5, drift=0.004, vol=0.01)
    dn = walk(seed=5, drift=-0.004, vol=0.01)
    a = br.rates(up, "看涨", up[-1]["c"], 5)["base_rate"]
    b = br.rates(dn, "看涨", dn[-1]["c"], 5)["base_rate"]
    assert a > b


def test_matches_brute_force_definition():
    """手算一遍定义：k = 1% × 价 ÷ 今天 ATR；第 t 天看 t+N 收盘 − t 收盘 > k × ATR_t。"""
    bars = walk(seed=7)
    rows = br._rows(bars)
    atrs = br.atr_series(rows)
    N, c = 5, rows[-1][2]
    k = c * 0.01 / atrs[-1]
    starts = [t for t in range(max(14, len(rows) - 1 - N - 250 + 1), len(rows) - N) if atrs[t]]
    want = sum(rows[t + N][2] - rows[t][2] > k * atrs[t] for t in starts) / len(starts) * 100
    assert br.rates(bars, "看涨", c, N)["base_rate"] == round(want, 1)


def test_missing_or_short_data_returns_none():
    assert br.rates([], "看涨", 10, 5) is None
    assert br.rates(walk(n=50), "看涨", 10, 5) is None  # 不足 MIN_N
    assert br.rates(walk(), "看涨", 0, 5) is None
    assert br.rates(walk(), "看涨", 10, 0) is None
    assert br.rates([{"c": "x"}] * 400, "看涨", 10, 5) is None
    assert br.for_judgment({"view": "看涨"}, walk()) is None  # 缺字段也不抛


def test_close_only_series_works():
    closes = [b["c"] for b in walk(seed=9)]
    r = br.rates(closes, "看跌", closes[-1], 5, flip_up=closes[-1] * 1.03)
    assert r and r["base_rate"] is not None and r["flip_rate"] is not None


def test_fmt():
    assert br.fmt({"base_rate": 38.0, "flip_rate": 22.5}) == "历史底数 38%（失效价 22.5%）"
    assert br.fmt({}) == "—"
    assert br.fmt({"flip_rate": 10.0}) == "失效价历史触及 10%"


def test_compare_and_small_sample():
    rows = [{"verdict": "对", "base_rate": 40.0}] * 6 + [{"verdict": "错", "base_rate": 40.0}] * 4 + [{"verdict": "对", "base_rate": None}]
    c = br.compare(rows)
    assert c == {"n": 10, "hit_rate": 60.0, "base_rate": 40.0, "diff": 20.0, "enough": False}
    assert "样本不足" in br.render_compare(c) and "不是预测概率" in br.render_compare(c)
    big = br.compare([{"verdict": "对", "base_rate": 50}] * 12 + [{"verdict": "错", "base_rate": 50}] * 13)
    assert big["enough"] and big["n"] == 25 and "样本不足" not in br.render_compare(big)
    assert br.compare([]) is None and br.render_compare(None) == ""


def _series(vals, end="2026-10-30"):
    return pd.Series(vals, index=pd.bdate_range(end=end, periods=len(vals)), dtype=float)


UP = indicators.analyze(_series([100 + i * 0.5 for i in range(80)]), {})


def _snap():
    return {"indices": {"A股": [{"symbol": "000001.SS", "name": "上证指数", "data": {}}]},
            "watchlist": [{"symbol": "NVDA", "name": "英伟达", "data": UP}, {"symbol": "AAPL", "name": "苹果", "data": UP}]}


def test_morning_writes_base_to_ledger_and_renders(mem):
    bars = walk(start=UP["close"] / 1.0)
    js = judgments.morning(mem, _snap(), today=date(2026, 10, 30), bars={"NVDA": bars})  # AAPL 没有日线：跳过不报错
    nv = next(j for j in js if j["symbol"] == "NVDA")
    ap = next(j for j in js if j["symbol"] == "AAPL")
    assert nv["base_rate"] is not None and nv["base_n"] == br.LOOKBACK and ap.get("base_rate") is None
    row = mem.calls_where("symbol='NVDA' AND source='morning'")[0]
    assert row["base_rate"] == nv["base_rate"] and row["flip_rate"] == nv["flip_rate"] and row["base_n"] == br.LOOKBACK
    assert mem.calls_where("symbol='AAPL'")[0]["base_rate"] is None
    md = "\n".join(judgments.render_morning(js))
    assert "| 历史底数 |" in md and f"历史底数 {nv['base_rate']:g}%" in md and "不是预测概率" in md
    # 沿用的判断沿用记下的底数
    js2 = judgments.morning(mem, _snap(), today=date(2026, 11, 2), bars={})
    assert next(j for j in js2 if j["symbol"] == "NVDA")["base_rate"] == nv["base_rate"]


def test_render_without_base_keeps_old_table(mem):
    js = judgments.morning(mem, _snap(), today=date(2026, 10, 30))
    md = "\n".join(judgments.render_morning(js))
    assert "历史底数" not in md and "| 标的 | 判断 | 置信度 | 失效时间 |" in md


def test_evening_and_call_stats_compare(mem):
    for i in range(5):
        cid = mem.record_call("NVDA", "英伟达", "看涨", 100, 5, "早报｜测试", 55, due="2026-10-01", source="morning", base_rate=40.0, flip_rate=20.0, base_n=250)
        mem.close_call(cid, 101, 1.5 if i < 3 else -0.5, "对" if i < 3 else "错")
    s = judgments.base_stats(mem)
    assert s["n"] == 5 and s["hit_rate"] == 60.0 and s["base_rate"] == 40.0 and s["diff"] == 20.0 and not s["enough"]
    out = "\n".join(judgments.render_review([], s))
    assert "命中率 60% vs 平均历史底数 40%" in out and "样本不足" in out
    st = mem.call_stats()
    assert st["底数对比"]["n"] == 5 and "样本不足" in st["底数对比"]["说明"]


def test_no_base_no_compare(mem):
    cid = mem.record_call("NVDA", "英伟达", "看涨", 100, 5, "早报｜测试", 55, due="2026-10-01", source="morning")
    mem.close_call(cid, 101, 1.5, "对")
    assert judgments.base_stats(mem) is None and "底数对比" not in mem.call_stats()
    assert judgments.render_review([], None) == ["## 判断复盘", "", "今天没有在跟踪的早报判断。", ""]
