"""Probatio 的「别自欺」统计：把回测和判断账本的数字打个折再说。

三件事（方法参考 Bailey & López de Prado 的 Deflated Sharpe Ratio，及 QuantJourney Backtester 的 walk-forward 统计层，Apache-2.0）：
1. 重叠窗口：每 5 天判断一次、看 20 天结果，相邻样本有 3/4 的日子重合，彼此不独立。按有效样本数 n·step/h 重算 p，
   另给只取不重叠样本（每隔 ⌈h/step⌉ 个取一个）的二项 p。
2. 试过很多方案：单轮、辩论、加资金流、排序……从 N 个里挑最好的那个报 p，本身就是多重检验。
   给 Holm 校正后的 p，和最好那条多空曲线的 Deflated Sharpe（扣掉试错次数后，夏普为真的概率）。
3. 判断账本：没有挑选，不用 DSR；给准确率的 Wilson 95% 区间，看它和 50% 分不分得开。
只用标准库，不依赖 scipy。
"""
from __future__ import annotations

import math

EULER = 0.5772156649015329


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def phi_inv(p: float) -> float:
    """标准正态分位数（二分，够用且无依赖）。"""
    p = min(max(p, 1e-12), 1 - 1e-12)
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """比例的 Wilson 区间（%），小样本比 ±1.96·√(p(1-p)/n) 靠谱。"""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round((c - h) * 100, 1), round((c + h) * 100, 1)]


def overlap_factor(horizon: int, step: int) -> float:
    return max(1.0, horizon / max(step, 1))


def p_hit_eff(k: int, n: int, horizon: int, step: int) -> float | None:
    """重叠窗口下的单边 p：样本数按 n/(h/step) 折算后用正态近似。"""
    if not n:
        return None
    ne = n / overlap_factor(horizon, step)
    z = (k / n - 0.5) / math.sqrt(0.25 / ne)
    return round(1 - phi(z), 4)


def binom_p(k: int, n: int) -> float | None:
    if not n:
        return None
    return round(sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n, 4)


def nonoverlap(rows: list[dict], horizon: int, step: int, key=lambda r: (r.get("symbol"), r.get("date"))) -> list[dict]:
    """每只标的按日期排序，每隔 ⌈h/step⌉ 个取一个，保证结果窗口互不重叠。"""
    gap = math.ceil(overlap_factor(horizon, step))
    by = {}
    for r in sorted(rows, key=key):
        by.setdefault(r.get("symbol"), []).append(r)
    return [r for rs in by.values() for r in rs[::gap]]


def holm(pvals: list[float]) -> list[float]:
    """Holm–Bonferroni 校正后的 p，顺序与输入一致。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    out, run = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * pvals[i]))
        out[i] = round(run, 4)
    return out


def moments(x: list[float]) -> tuple[float, float, float, float]:
    """均值、标准差、偏度、峰度（非超额，正态 = 3）。"""
    n = len(x)
    mu = sum(x) / n
    var = sum((v - mu) ** 2 for v in x) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    if not sd:
        return mu, 0.0, 0.0, 3.0
    sk = sum(((v - mu) / sd) ** 3 for v in x) / n
    ku = sum(((v - mu) / sd) ** 4 for v in x) / n
    return mu, sd, sk, ku


def sharpe(x: list[float]) -> float | None:
    """每期夏普（不年化）。"""
    if len(x) < 3:
        return None
    mu, sd, _, _ = moments(x)
    return mu / sd if sd else None


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """N 次独立试错、夏普真值都是 0 时，最好那次的期望夏普（False Strategy Theorem）。"""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER) * phi_inv(1 - 1 / n_trials) + EULER * phi_inv(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(sr: float, t: int, n_trials: int, var_sr: float, skew: float = 0.0, kurt: float = 3.0) -> float | None:
    """DSR = P(真夏普 > 试错 N 次的期望最大值)。sr、var_sr 都是每期口径，t 是期数。"""
    if t < 3:
        return None
    sr0 = expected_max_sharpe(n_trials, var_sr)
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if den <= 0:
        return None
    return round(phi((sr - sr0) * math.sqrt(t - 1) / math.sqrt(den)), 4)


def returns_from_curve(curve: list) -> list[float]:
    """回测曲线是累计收益%，还原成每期收益（小数）。"""
    lv = [1 + c[1] / 100 for c in curve if c and c[1] is not None]
    return [b / a - 1 for a, b in zip([1.0] + lv[:-1], lv) if a]


def _run_stat(j: dict) -> dict | None:
    """一次回测的主周期 p 与多空每期收益。"""
    if j.get("status") != "done":
        return None
    p = j.get("params") or {}
    h0 = str((p.get("horizons") or [5])[0])
    if j.get("mode") == "rank":
        a = (j.get("results") or {}).get("agent", {}).get(h0, {})
        pv, stat = (a.get("ic") or {}).get("p_nw"), (a.get("ic") or {}).get("mean")
        rets = [v / 100 for v in ((a.get("_series") or {}).get("ls") or []) if v is not None]
        label = f"排序 IC {stat:+.3f}" if stat is not None else "排序"
    else:
        a = ((j.get("results") or {}).get("agent") or {}).get("全部") or {}
        hz = (a.get("horizons") or {}).get(h0) or {}
        pv, stat = hz.get("p_binom"), hz.get("hit_dir%")
        rets = returns_from_curve(a.get("curve") or [])
        label = f"方向胜率 {stat}%" if stat is not None else "择时"
    if pv is None:
        return None
    ovl = overlap_factor(int(h0), int(p.get("step") or 5))
    if j.get("mode") != "rank":
        ovl = 1.0  # 择时曲线是按期滚动的组合净值，已按期折算
    return {"id": j.get("id"), "agent": p.get("agent"), "mode": j.get("mode", "timing"), "p": pv, "label": label,
            "sr": sharpe(rets), "t": max(3, int(len(rets) / ovl)), "rets": rets}


def trials(runs: list[dict], include_baseline: bool = False) -> dict:
    """把做过的回测当成一次多重检验：最好的 p 校正后还剩多少，最好的多空曲线 DSR 多少。"""
    def real(j):
        p = j.get("params") or {}
        return not (p.get("synthetic") or p.get("llm_kind") == "mock" or "synthetic" in str(p.get("data", "")))
    st, seen = [], set()
    for j in runs:
        s = _run_stat(j) if real(j) else None
        if not s or (not include_baseline and s["agent"] == "baseline"):
            continue
        key = (s["mode"], s["label"], s["p"])  # 同参数重跑一遍不算新的试错
        if key not in seen:
            seen.add(key)
            st.append(s)
    if not st:
        return {"n": 0}
    adj = holm([s["p"] for s in st])
    for s, a in zip(st, adj):
        s["p_holm"] = a
    best = min(st, key=lambda s: s["p"])
    out = {"n": len(st), "best": {k: best[k] for k in ("id", "label", "p", "p_holm")},
           "any_significant": any(a < 0.05 for a in adj)}
    out["dsr"] = []
    for mode in ("timing", "rank"):  # 两种模式每期口径不同，夏普只在同一模式里比
        srs = [s for s in st if s["mode"] == mode and s["sr"] is not None and s["t"] >= 10]
        if len(srs) < 2:
            continue
        _, sd, _, _ = moments([s["sr"] for s in srs])
        top = max(srs, key=lambda s: s["sr"])
        _, _, sk, ku = moments(top["rets"])
        out["dsr"].append({"mode": mode, "id": top["id"], "sr": round(top["sr"], 3), "t": top["t"], "trials": len(srs),
                           "sr0": round(expected_max_sharpe(len(srs), sd * sd), 3),
                           "dsr": deflated_sharpe(top["sr"], top["t"], len(srs), sd * sd, sk, ku)})
    out["verdict"] = verdict(out)
    return out


def verdict(t: dict) -> str:
    if not t.get("n"):
        return "还没有可比较的回测。"
    b = t["best"]
    s = f"试过 {t['n']} 个方案，最好的是 {b['label']}（p={b['p']}），按试错次数校正后 p={b['p_holm']}"
    s += "，仍显著。" if b["p_holm"] < 0.05 else "，不再显著。"
    for d in t.get("dsr") or []:
        if d["dsr"] is None:
            continue
        s += f"{'择时' if d['mode'] == 'timing' else '排序'}里最好的多空曲线每期夏普 {d['sr']}，{d['trials']} 次试错光靠运气就能到 {d['sr0']}，DSR {d['dsr']:.0%}" + \
             ("（≥95% 才算站得住）。" if d["dsr"] < 0.95 else "，站得住。")
    return s
