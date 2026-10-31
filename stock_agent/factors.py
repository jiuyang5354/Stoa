"""因子 IC 检验（借鉴 qlib 的 IC / Rank IC / ICIR 思路，只用标准库 + pandas）。

每个决策日做一次横截面：因子值与「之后 h 个交易日收益」的 Spearman 秩相关 = 当日 Rank IC。
汇总：IC 均值、标准差、ICIR = 均值/标准差、年化 ICIR = ICIR × √(每年决策次数)、IC>0 占比、
Newey–West t（相邻决策日的未来窗口重叠 → 滞后 ⌈h/step⌉−1）、移动块自助 95% 区间。
qlib 的经验门槛：|IC| ≥ 0.02–0.03 且 ICIR ≥ 0.3（日频）才算有点用；这里只当参考，样本小时看 p 和区间。

用法：backtest --mode factors（默认 12 只 / --universe csi300），结果存 data/backtests/<id>.json（mode=factors）；
择时回测的 metrics 里也把模型的「方向×置信度」当一个因子算 IC（见 backtest.metrics）。
"""
from __future__ import annotations

import math

import pandas as pd

from . import rank


def _ret(c: pd.Series, n: int) -> float | None:
    return float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n and c.iloc[-1 - n] else None


def _rsi(c: pd.Series, n: int = 14) -> float | None:
    d = c.diff().tail(n)
    if len(d) < n:
        return None
    up, dn = d.clip(lower=0).mean(), (-d.clip(upper=0)).mean()
    return 100.0 if dn == 0 else float(100 - 100 / (1 + up / dn))


def _vol(c: pd.Series, n: int = 20) -> float | None:
    r = c.pct_change().tail(n)
    return float(r.std()) if len(r.dropna()) >= n - 1 else None


# 因子：值越大 = 预期越好（反转、低波动等已取负号）
FACTORS = {
    "mom20": ("20 日动量", lambda c: _ret(c, 20)),
    "mom60": ("60 日动量", lambda c: _ret(c, 60)),
    "mom120_20": ("120 日动量（跳过近 20 日）", lambda c: (float(c.iloc[-21] / c.iloc[-121] - 1) if len(c) > 121 else None)),
    "rev5": ("5 日反转", lambda c: (-_ret(c, 5) if _ret(c, 5) is not None else None)),
    "lowvol20": ("20 日低波动", lambda c: (-_vol(c) if _vol(c) is not None else None)),
    "ma20_gap": ("距 20 日线", lambda c: float(c.iloc[-1] / c.tail(20).mean() - 1) if len(c) >= 20 else None),
    "rsi14_rev": ("RSI 反转（超卖得分高）", lambda c: (-_rsi(c) if _rsi(c) is not None else None)),
    "high52": ("52 周高点接近度", lambda c: float(c.iloc[-1] / c.tail(250).max()) if len(c) >= 120 else None),
}


def daily_ic(scores: dict, fwd: dict) -> float | None:
    """一天的 Rank IC：scores / fwd 都是 {symbol: 值}；少于 3 只或因子全相同返回 None。"""
    ks = [s for s in scores if scores[s] is not None and fwd.get(s) is not None]
    if len(ks) < 3:
        return None
    return rank.spearman([scores[s] for s in ks], [fwd[s] for s in ks])


def ic_stats(ics: list, horizon: int, step: int, per_year: float | None = None) -> dict:
    """IC 序列汇总（qlib 口径 + 重叠窗口校正）。"""
    x = [v for v in ics if v is not None]
    lag = max(0, math.ceil(horizon / max(1, step)) - 1)
    s = rank.summarize(x, lag=lag, block=lag + 1)
    if s.get("n", 0) < 3:
        return {"n": s.get("n", 0)}
    per_year = per_year or 252 / max(1, step)
    icir = s.get("ir")
    return {"n": s["n"], "ic": s["mean"], "ic_std": s["std"], "icir": icir,
            "icir_ann": round(icir * math.sqrt(per_year), 2) if icir is not None else None,
            "pos%": s["pos%"], "t_nw": s["t_nw"], "p_nw": s["p_nw"], "ci95": s["ci95"], "nw_lag": lag,
            "verdict": verdict(s["mean"], icir, s["p_nw"])}


def verdict(ic, icir, p) -> str:
    if ic is None:
        return "样本不足"
    strong = abs(ic) >= 0.03 and (icir or 0) * (1 if ic > 0 else -1) >= 0.3
    sig = p is not None and p < 0.05
    if strong and sig:
        return "有效（显著）" if ic > 0 else "反向有效（显著）"
    if strong:
        return "IC 达标但不显著"
    return "无效"


def audit(closes: dict, start, end, step: int = 5, horizons=(5, 20), factors: dict | None = None, extra: dict | None = None) -> dict:
    """closes: {symbol: Series}。extra: {名字: fn(day, {sym: 截断 Series}) -> {sym: 分}}，如模型分数。"""
    from .backtest import PointInTime
    factors = factors or FACTORS
    pit = PointInTime(closes)
    xs = rank.cross_sections(pit, start, end, step, list(horizons))
    names = {k: v[0] for k, v in factors.items()}
    series = {k: {h: [] for h in horizons} for k in list(factors) + list(extra or {})}
    vals_by_day = {k: [] for k in series}
    for day, cl, fwd in xs:
        sc = {k: {s: fn(c) for s, c in cl.items()} for k, (_, fn) in factors.items()}
        for k, fn in (extra or {}).items():
            sc[k] = fn(day, cl)
        for k, v in sc.items():
            vals_by_day[k].append(v)
            for h in horizons:
                series[k][h].append(daily_ic(v, {s: f.get(h) for s, f in fwd.items()}))
    res = {}
    for k in series:
        res[k] = {"name": names.get(k, k), **{str(h): {**ic_stats(series[k][h], h, step), "_series": [round(v, 4) if v is not None else None for v in series[k][h]]}
                                             for h in horizons}}
    keys = list(series)
    corr = {}  # 因子两两相关（逐日横截面 Spearman 的均值）：看是不是同一个东西
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            cs = [daily_ic(va, vb) for va, vb in zip(vals_by_day[a], vals_by_day[b])]
            cs = [c for c in cs if c is not None]
            if cs:
                corr[f"{a}~{b}"] = round(sum(cs) / len(cs), 3)
    h0 = str(horizons[0])
    best = max((k for k in res if res[k][h0].get("ic") is not None), key=lambda k: abs(res[k][h0]["ic"]), default=None)
    return {"days": [d.strftime("%Y-%m-%d") for d, _, _ in xs], "n_days": len(xs),
            "n_symbols": len(closes), "factors": res, "corr": corr, "best": best,
            "method": f"每 {step} 个工作日一个横截面，Rank IC = Spearman(因子, 未来 h 日收益)；ICIR = 均值/标准差；"
                      f"t 用 Newey–West（滞后 ⌈h/step⌉−1），区间为移动块自助。门槛参考 qlib：|IC|≥0.03 且 ICIR≥0.3 且 p<0.05。"}


def run_factors(symbols=None, start=None, end=None, step=5, horizons=(5, 20), synthetic=False, universe=None, n=100, run_id=None, seed=7) -> dict:
    import json
    from datetime import datetime
    from . import backtest
    from .backtest import CN_TZ, DEFAULT_SYMBOLS, bt_dir, load_prices
    end = end or (pd.Timestamp.now().normalize() - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    start = start or (pd.Timestamp(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    if universe == "csi300":
        from . import universe as uv
        symbols = uv.csi300_sample(start, n=n, seed=seed)["symbols"]
    symbols = symbols or DEFAULT_SYMBOLS
    closes, src = load_prices(symbols, start, end, synthetic=synthetic, seed=seed)
    res = audit(closes, start, end, step=step, horizons=list(horizons))
    run_id = run_id or backtest.new_run_id("factors")
    out = {"id": run_id, "status": "done", "mode": "factors", "created": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
           "params": {"agent": "factors", "symbols": list(closes), "start": start, "end": end, "step": step, "horizons": list(horizons),
                      "data": src, "synthetic": synthetic, "universe": universe},
           "results": res}
    (bt_dir() / f"{run_id}.json").write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    return out


def print_factors(res: dict):
    r = res["results"]
    hs = [str(h) for h in res["params"]["horizons"]]
    print(f"因子 IC · {res['params']['start']} → {res['params']['end']} · {r['n_symbols']} 只 · {r['n_days']} 个横截面 · {res['params']['data']}")
    for h in hs:
        print(f"\n未来 {h} 日：")
        print(f"{'因子':<22}{'IC':>8}{'ICIR':>7}{'年化':>7}{'IC>0':>7}{'p_NW':>8}  结论")
        for k, v in sorted(r["factors"].items(), key=lambda kv: -abs(kv[1][h].get("ic") or 0)):
            s = v[h]
            if s.get("ic") is None:
                continue
            print(f"{v['name'][:20]:<22}{s['ic']:>+8.3f}{(s['icir'] or 0):>7.2f}{(s['icir_ann'] or 0):>7.2f}{s['pos%']:>6.0f}%{(s['p_nw'] if s['p_nw'] is not None else float('nan')):>8.3f}  {s['verdict']}")
    print("\n" + r["method"])
