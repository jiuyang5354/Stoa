"""历史触达率底数（v0.35）：给早报判断的「判断成立」和「失效价」各配一个历史比例，作为没有任何判断力时的参照。

做法（只用该标的过去约 250 个交易日的日线，不调模型、不联网以外的取数）：
  1. 幅度按 ATR(14) 归一：今天这条判断要求的幅度 = k 个 ATR（k = 幅度 ÷ 今天的 ATR）；
  2. 回看过去每个交易日 t：同样 k 个「当时的」ATR，在之后 N 个交易日内出现了没有；
  3. 出现的天数 ÷ 有效天数 = 底数。
  - 判断成立底数 base_rate：口径与账本结算一致（scorecard.verdict）——看涨 = N 天后收盘较判断价涨超 1%，看跌 = 跌超 1%，
    震荡 = |涨跌| ≤ 3%；所以按「第 N 天收盘」算，不按盘中触及（否则底数会系统性偏高，命中率对比就不公平）。
  - 失效价底数 flip_rate：失效价（翻转价）在 N 天内被盘中最高 / 最低价触及的比例；震荡判断上下两头任一触及即算。
没有最高最低价时用收盘价的日变动代替真实波幅（收益率归一）。样本不足（有效天数 < MIN_N）或数据缺失时返回 None，不报错。
底数不是预测概率：它只说明「这样的幅度在这只股票上过去有多常见」，用来和 Stoa 的命中率比，看判断有没有超出底数。
"""
from __future__ import annotations

LOOKBACK = 250   # 回看的起点天数（约一年交易日）
ATR_N = 14
MIN_N = 60       # 有效样本少于这个数就不给底数
UP_PCT, RANGE_PCT = 1.0, 3.0  # 与 scorecard.verdict 同口径：看涨/看跌要过 ±1%，震荡在 ±3% 内


def _rows(bars) -> list[tuple[float, float, float]]:
    """bars：[{h,l,c}] 或 [{high,low,close}] 或纯收盘价列表 → [(high, low, close)]。坏行跳过。"""
    out = []
    for b in bars or []:
        try:
            if isinstance(b, dict):
                c = float(b.get("c", b.get("close")))
                h = b.get("h", b.get("high"))
                lo = b.get("l", b.get("low"))
                h = float(h) if h is not None else c
                lo = float(lo) if lo is not None else c
            else:
                c = h = lo = float(b)
        except (TypeError, ValueError):
            continue
        if c > 0 and h >= lo:
            out.append((h, lo, c))
    return out


def atr_series(rows, n: int = ATR_N) -> list[float | None]:
    """简单均值 ATR（真实波幅 = max(高-低, |高-昨收|, |低-昨收|)）；没有高低价时退化为 |收-昨收|。"""
    trs = [None]
    for i in range(1, len(rows)):
        h, lo, _ = rows[i]
        pc = rows[i - 1][2]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    out: list[float | None] = []
    for i in range(len(rows)):
        w = [t for t in trs[max(1, i - n + 1):i + 1] if t is not None]
        out.append(sum(w) / len(w) if i >= n and w and sum(w) > 0 else None)
    return out


def _starts(rows, atrs, days: int, lookback: int):
    last = len(rows) - 1 - days  # 之后还要有 N 天才能看结果
    first = max(ATR_N, last - lookback + 1)
    return [t for t in range(first, last + 1) if atrs[t]]


def rates(bars, view: str, price: float, days: int, flip_up: float | None = None, flip_down: float | None = None,
          lookback: int = LOOKBACK) -> dict | None:
    """返回 {base_rate, flip_rate, n, atr, k_*}（百分数，保留 1 位）；数据不够返回 None。"""
    try:
        days = int(days)
        price = float(price)
    except (TypeError, ValueError):
        return None
    rows = _rows(bars)
    if days < 1 or price <= 0 or len(rows) < ATR_N + days + MIN_N:
        return None
    atrs = atr_series(rows)
    atr_now = atrs[-1]
    if not atr_now:
        return None
    starts = _starts(rows, atrs, days, lookback)
    if len(starts) < MIN_N:
        return None
    out = {"n": len(starts), "days": days, "atr": round(atr_now, 4), "method": f"ATR{ATR_N} 归一，回看 {len(starts)} 个交易日"}
    # 判断成立：第 N 天收盘
    if view in ("看涨", "看跌"):
        k = price * UP_PCT / 100 / atr_now
        hit = 0
        for t in starts:
            move = rows[t + days][2] - rows[t][2]
            thr = k * atrs[t]
            hit += (move > thr) if view == "看涨" else (move < -thr)
        out["base_rate"], out["k_base"] = round(hit / len(starts) * 100, 1), round(k, 3)
    elif view == "震荡":
        k = price * RANGE_PCT / 100 / atr_now
        hit = sum(abs(rows[t + days][2] - rows[t][2]) <= k * atrs[t] for t in starts)
        out["base_rate"], out["k_base"] = round(hit / len(starts) * 100, 1), round(k, 3)
    # 失效价：N 天内盘中触及
    ku = (flip_up - price) / atr_now if flip_up and flip_up > price else None
    kd = (price - flip_down) / atr_now if flip_down and 0 < flip_down < price else None
    if ku is not None or kd is not None:
        hit = 0
        for t in starts:
            c0, a0 = rows[t][2], atrs[t]
            win = rows[t + 1:t + days + 1]
            up = ku is not None and max(r[0] for r in win) >= c0 + ku * a0
            dn = kd is not None and min(r[1] for r in win) <= c0 - kd * a0
            hit += up or dn
        out["flip_rate"] = round(hit / len(starts) * 100, 1)
        if ku is not None:
            out["k_up"] = round(ku, 3)
        if kd is not None:
            out["k_down"] = round(kd, 3)
    return out if ("base_rate" in out or "flip_rate" in out) else None


def for_judgment(j: dict, bars) -> dict | None:
    """给 judgments.judge() 的结果算底数；任何异常都吞掉返回 None（底数是附加信息，不能拖垮早报）。"""
    try:
        return rates(bars, j["view"], j["price"], j.get("days") or 5, j.get("flip_up"), j.get("flip_down"))
    except Exception:
        return None


def fmt(j: dict) -> str:
    """早报里的简短显示：「历史底数 38%（失效价 22%）」；没有就「—」。"""
    b, f = j.get("base_rate"), j.get("flip_rate")
    if b is None and f is None:
        return "—"
    s = f"历史底数 {b:g}%" if b is not None else ""
    if f is not None:
        s += (f"（失效价 {f:g}%）" if s else f"失效价历史触及 {f:g}%")
    return s


SMALL = 20  # 样本少于这个数标「样本不足」


def compare(rows: list[dict]) -> dict | None:
    """rows：已结算的早报判断（verdict 对/错 + base_rate）。返回命中率 vs 平均底数；没有带底数的样本返回 None。"""
    rs = [r for r in rows if r.get("verdict") in ("对", "错") and r.get("base_rate") is not None]
    if not rs:
        return None
    n = len(rs)
    hit = sum(r["verdict"] == "对" for r in rs) / n * 100
    base = sum(float(r["base_rate"]) for r in rs) / n
    return {"n": n, "hit_rate": round(hit, 1), "base_rate": round(base, 1), "diff": round(hit - base, 1), "enough": n >= SMALL}


def render_compare(c: dict | None) -> str:
    if not c:
        return ""
    s = (f"已结算的早报判断 {c['n']} 条：命中率 {c['hit_rate']:g}% vs 平均历史底数 {c['base_rate']:g}%，"
         f"差 {c['diff']:+g} 个百分点")
    s += "（样本不足，先别下结论）" if not c["enough"] else ""
    return s + "。底数是同等幅度的历史出现比例，不是预测概率。"
