import json
import random

import pandas as pd

from stock_agent import factors


def test_daily_ic_perfect_and_inverse():
    sc = {"a": 1, "b": 2, "c": 3, "d": 4}
    assert abs(factors.daily_ic(sc, {"a": .1, "b": .2, "c": .3, "d": .4}) - 1) < 1e-9
    assert abs(factors.daily_ic(sc, {"a": .4, "b": .3, "c": .2, "d": .1}) + 1) < 1e-9
    assert factors.daily_ic({"a": 1, "b": 2}, {"a": 1, "b": 2}) is None
    assert factors.daily_ic({"a": 1, "b": 1, "c": 1}, {"a": 1, "b": 2, "c": 3}) is None  # 因子全一样


def test_ic_stats_icir_and_verdict():
    rnd = random.Random(1)
    ics = [0.08 + rnd.gauss(0, 0.05) for _ in range(60)]
    s = factors.ic_stats(ics, horizon=5, step=5)
    assert s["n"] == 60 and 0.05 < s["ic"] < 0.11
    assert s["icir"] > 1 and s["icir_ann"] > s["icir"]
    assert s["nw_lag"] == 0 and s["p_nw"] < 0.01 and s["verdict"] == "有效（显著）"
    noise = factors.ic_stats([rnd.gauss(0, 0.1) for _ in range(40)], 20, 5)
    assert noise["nw_lag"] == 3 and noise["verdict"] in ("无效", "IC 达标但不显著")
    assert factors.ic_stats([0.1], 5, 5) == {"n": 1}


def _planted(n_sym=12, days=420, seed=3):
    """造一个「20 日动量真的有效」的横截面：每只股票有持续的漂移，近期涨得多的后面也涨得多。"""
    rnd = random.Random(seed)
    idx = pd.bdate_range("2024-01-01", periods=days)
    out = {}
    for i in range(n_sym):
        drift = (i - n_sym / 2) * 0.0008
        p, v = 100.0, []
        for _ in idx:
            p *= 1 + drift + rnd.gauss(0, 0.004)
            v.append(p)
        out[f"S{i:02d}"] = pd.Series(v, index=idx)
    return out, idx


def test_audit_finds_planted_momentum():
    closes, idx = _planted()
    res = factors.audit(closes, idx[150], idx[-40], step=5, horizons=[20])
    f = res["factors"]
    assert res["n_days"] > 20 and res["n_symbols"] == 12
    assert f["mom60"]["20"]["ic"] > 0.5 and f["mom60"]["20"]["verdict"].startswith("有效")
    assert f["rev5"]["20"]["ic"] < f["mom60"]["20"]["ic"]
    assert "mom20~mom60" in res["corr"] and res["corr"]["mom20~mom60"] > 0.5
    assert len(f["mom20"]["20"]["_series"]) == res["n_days"]


def test_audit_extra_model_scores():
    closes, idx = _planted()
    res = factors.audit(closes, idx[150], idx[-40], step=10, horizons=[5], factors={},
                        extra={"model": lambda day, cl: {s: float(c.iloc[-1] / c.iloc[-41]) for s, c in cl.items()}})
    assert res["factors"]["model"]["5"]["ic"] > 0.3


def test_backtest_metrics_has_model_ic():
    from stock_agent import backtest
    rows = []
    for d in range(30):
        for i, s in enumerate("ABCDEF"):
            good = i < 3
            rows.append({"symbol": s, "date": f"2025-01-{d:02d}", "view": "看多" if good else "看空", "confidence": 60,
                         "ret5": (2 if good else -2) + (d % 3) * 0.1 * i, "ret_step": 0.1})
    m = backtest.metrics(rows, [5], step=5)
    ic = m["horizons"]["5"]["ic"]
    assert ic["n"] == 30 and ic["ic"] > 0.5


def test_run_factors_synthetic_writes_and_lists(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    from stock_agent import backtest, probatio
    res = factors.run_factors(symbols=["600519.SS", "000858.SZ", "601318.SS", "300750.SZ", "0700.HK", "9988.HK", "NVDA", "AAPL"],
                              start="2025-01-01", end="2025-09-01", synthetic=True, horizons=(5, 20))
    j = json.loads((backtest.bt_dir() / f"{res['id']}.json").read_text(encoding="utf-8"))
    assert j["mode"] == "factors" and j["results"]["n_days"] > 10
    row = next(r for r in backtest.listing() if r["id"] == res["id"])
    assert row["mode"] == "factors" and row["headline"]["best"] in factors.FACTORS
    assert probatio.trials(backtest.all_runs())["n"] == 0  # 因子检验不算模型的试错
    factors.print_factors(res)
