"""成交分布（Volume Profile）近似：用日线或分钟线把每根 K 线的成交量摊到它的最高—最低区间。

输出 POC（成交最密的价位）、高/低成交量节点（HVN/LVN）、价值区（含 70% 成交量的连续区间 VAL–VAH），
再按「现价在不在价值区里、近半段与前半段的 POC 是否迁移」判断市场处在平衡（震荡）还是失衡（趋势）。

近似口径：交易所没有逐价成交时，只能假设每根 K 线的量在 [low, high] 里均匀分布——日线粒度粗，
分钟线更接近真实。纯计算 profile(bars) 不联网，便于测试；fetch_bars 负责取数。
"""
from __future__ import annotations

import math

VA_SHARE = 0.70


def profile(bars: list[dict], bins: int = 40, va_share: float = VA_SHARE) -> dict:
    """bars: [{t, o, h, l, c, v}]，按时间升序。返回分布与判断；数据不够时返回 error。"""
    bars = [b for b in bars if b.get("c") is not None and (b.get("v") or 0) > 0]
    if len(bars) < 5:
        return {"error": "K 线太少（少于 5 根有成交的），算不出成交分布"}
    lo = min(min(b.get("l") or b["c"], b["c"]) for b in bars)
    hi = max(max(b.get("h") or b["c"], b["c"]) for b in bars)
    if hi <= lo:
        return {"error": "区间内价格没有波动"}
    bins = max(10, min(200, int(bins or 40)))
    vol = _distribute(bars, lo, hi, bins)
    w = (hi - lo) / bins
    total = sum(vol)
    price = lambda i: lo + (i + 0.5) * w  # noqa: E731
    poc_i = max(range(bins), key=lambda i: vol[i])
    a, b = _value_area(vol, poc_i, va_share)
    val, vah = lo + a * w, lo + (b + 1) * w
    hvn, lvn = _nodes(vol, a, b)
    last = bars[-1]["c"]
    # 前后两半各算一次 POC：迁移幅度相对价值区宽度 → 价值在不在移动
    half = len(bars) // 2
    poc_old = _poc(bars[:half], lo, hi, bins) if half >= 3 else None
    poc_new = _poc(bars[half:], lo, hi, bins) if len(bars) - half >= 3 else None
    shift = (poc_new - poc_old) if poc_old is not None and poc_new is not None else 0.0
    va_w = max(vah - val, w)
    outside = [("above" if x["c"] > vah else "below" if x["c"] < val else "in") for x in bars[-5:]]
    state, regime, why = _judge(last, val, vah, shift, va_w, outside, hvn, lvn, price)
    r4 = lambda x: round(x, 4)  # noqa: E731
    return {
        "poc": r4(price(poc_i)), "vah": r4(vah), "val": r4(val), "va_share": va_share,
        "va_width_pct": round(va_w / last * 100, 2) if last else None,
        "range": [r4(lo), r4(hi)], "bin_size": r4(w), "bins": bins,
        "hvn": [{"price": r4(price(i)), "vol_pct": round(vol[i] / total * 100, 2)} for i in hvn],
        "lvn": [{"price": r4(price(i)), "vol_pct": round(vol[i] / total * 100, 2)} for i in lvn],
        "last_close": r4(last), "vs_poc_pct": round((last / price(poc_i) - 1) * 100, 2),
        "poc_shift_pct": round(shift / last * 100, 2) if last else None,
        "last5": "".join({"above": "↑", "below": "↓", "in": "·"}[x] for x in outside),
        "state": state, "regime": regime, "reasons": why,
        "profile": [{"price": r4(price(i)), "vol_pct": round(vol[i] / total * 100, 3)} for i in range(bins)],
    }


def _distribute(bars, lo, hi, bins) -> list[float]:
    w = (hi - lo) / bins
    vol = [0.0] * bins
    for b in bars:
        l, h, v = min(b.get("l") or b["c"], b["c"]), max(b.get("h") or b["c"], b["c"]), float(b["v"])
        i0 = min(bins - 1, int((l - lo) / w))
        i1 = min(bins - 1, int((h - lo) / w))
        if h - l <= 1e-12 or i0 == i1:
            vol[i0] += v
            continue
        for i in range(i0, i1 + 1):  # 按与 [l,h] 重叠的长度分摊
            a, z = max(l, lo + i * w), min(h, lo + (i + 1) * w)
            if z > a:
                vol[i] += v * (z - a) / (h - l)
    return vol


def _poc(bars, lo, hi, bins):
    vol = _distribute(bars, lo, hi, bins)
    i = max(range(bins), key=lambda k: vol[k])
    return lo + (i + 0.5) * (hi - lo) / bins


def _value_area(vol, poc_i, share) -> tuple[int, int]:
    """从 POC 向两侧扩，每次加入相邻两格里量更大的那边，直到覆盖 share 的成交量（Market Profile 标准做法）。"""
    total, a, b = sum(vol), poc_i, poc_i
    acc = vol[poc_i]
    while acc < share * total and (a > 0 or b < len(vol) - 1):
        up = vol[b + 1] if b < len(vol) - 1 else -1
        dn = vol[a - 1] if a > 0 else -1
        if up >= dn:
            b += 1
            acc += up
        else:
            a -= 1
            acc += dn
    return a, b


def _nodes(vol, a, b) -> tuple[list[int], list[int]]:
    """平滑后找局部极大（HVN，≥ 均值 1.2 倍）与价值区附近的局部极小（LVN，≤ 均值 0.6 倍），各取 3 个。"""
    n = len(vol)
    s = [sum(vol[max(0, i - 1):i + 2]) / len(vol[max(0, i - 1):i + 2]) for i in range(n)]
    mean = sum(vol) / n
    g = lambda i: s[i] if 0 <= i < n else -1.0  # noqa: E731  区间两端也可以是高量节点
    peaks = [i for i in range(n) if g(i) >= g(i - 1) and g(i) > g(i + 1) and s[i] >= 1.2 * mean]
    troughs = [i for i in range(1, n - 1) if s[i] <= s[i - 1] and s[i] < s[i + 1] and s[i] <= 0.6 * mean]
    lo_lim, hi_lim = max(0, a - (b - a) // 2), min(n - 1, b + (b - a) // 2)
    troughs = [i for i in troughs if lo_lim <= i <= hi_lim] or troughs
    hvn = sorted(sorted(peaks, key=lambda i: -s[i])[:3])
    lvn = sorted(sorted(troughs, key=lambda i: s[i])[:3])
    return hvn, lvn


def _judge(last, val, vah, shift, va_w, outside, hvn, lvn, price):
    why = []
    out_up, out_dn = outside.count("above"), outside.count("below")
    migr = abs(shift) / va_w if va_w else 0
    if val <= last <= vah:
        state = "平衡"
        why.append(f"现价 {last:.4g} 在价值区 {val:.4g}–{vah:.4g} 内")
    elif last > vah:
        state = "向上失衡"
        why.append(f"现价 {last:.4g} 高于价值区上沿 {vah:.4g}")
    else:
        state = "向下失衡"
        why.append(f"现价 {last:.4g} 低于价值区下沿 {val:.4g}")
    if out_up >= 3 or out_dn >= 3:
        why.append(f"近 5 根里 {max(out_up, out_dn)} 根收在价值区{'上' if out_up >= out_dn else '下'}方，接受了新价位")
    if migr >= 0.5:
        why.append(f"后半段 POC 较前半段{'上移' if shift > 0 else '下移'} {abs(shift):.4g}（约价值区宽度的 {migr:.0%}），价值在迁移")
    if len(hvn) >= 2 and lvn and any(hvn[0] < i < hvn[-1] for i in lvn):
        why.append("分布呈双峰（两个高量节点中间夹着低量节点），可能是从一个平衡区走向另一个")
    trend = state != "平衡" and (out_up >= 3 or out_dn >= 3 or migr >= 0.5)
    if state == "平衡" and migr >= 0.8:
        trend = True
        why.append("虽回到价值区内，但价值迁移明显，按趋势处理")
    regime = "趋势" if trend else "震荡"
    if state != "平衡" and not trend:
        why.append("只是刚出价值区、价值还没跟着移动，先当试探（可能回到价值区）")
    return state, regime, why


# ---------------- 取数 ----------------
def fetch_bars(symbol: str, source: str = "daily", days: int = 60, interval: int = 5) -> tuple[list[dict], dict]:
    """日线：腾讯 → Yahoo；分钟线：marketdata 的 Yahoo / 东方财富 / 腾讯分钟线。返回 (bars, meta)。"""
    errs = []
    if source == "intraday":
        from . import marketdata as md
        p = md.parse(symbol)
        for name, fn, url in (("Yahoo Finance", md._yahoo_intraday, f"https://finance.yahoo.com/quote/{p['yahoo']}/chart"),
                              ("东方财富", md._em_intraday, "https://quote.eastmoney.com/"),
                              ("腾讯证券", md._tencent_intraday, f"https://gu.qq.com/{p['tencent']}")):
            if p["market"] in ("US", "IDX") and name != "Yahoo Finance":
                continue
            try:
                r = fn(p, max(1, min(5, int(days or 5))), int(interval or 5))
                bars = [b for b in (r or {}).get("bars") or [] if b.get("c") is not None]
                if bars:
                    return bars, {"source": name, "url": url, "as_of": bars[-1]["t"], "granularity": f"{interval} 分钟线"}
            except Exception as e:
                errs.append(f"{name}: {str(e)[:80]}")
        raise LookupError("分钟线都取不到：" + "；".join(errs))
    days = max(10, min(500, int(days or 60)))
    from . import data
    for code in data._tx_code(symbol):
        try:
            api = "usfqkline/get" if code.startswith("us") else "fqkline/get"
            j = data._tx_json(f"https://web.ifzq.gtimg.cn/appstock/app/{api}?param={code},day,,,{days},qfq")
            node = ((j or {}).get("data") or {}).get(code)
            rows = (node.get("qfqday") or node.get("day")) if isinstance(node, dict) else None
            if rows:
                bars = [{"t": r[0], "o": float(r[1]), "c": float(r[2]), "h": float(r[3]), "l": float(r[4]), "v": float(r[5])}
                        for r in rows if len(r) > 5]
                if bars:
                    return bars[-days:], {"source": "腾讯证券", "url": f"https://gu.qq.com/{code}", "as_of": bars[-1]["t"], "granularity": "日线（前复权）"}
        except Exception as e:
            errs.append(f"腾讯: {str(e)[:80]}")
    try:
        from datetime import date, timedelta
        from . import edgar
        d1 = date.today()
        rows, meta = edgar._yahoo_daily(symbol, (d1 - timedelta(days=int(days * 1.6) + 10)).isoformat(), d1.isoformat())
        bars = [{"t": r["date"], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"], "v": r["volume"]} for r in rows if r.get("volume")]
        if bars:
            return bars[-days:], {"source": "Yahoo Finance", "url": f"https://finance.yahoo.com/quote/{meta.get('symbol', symbol)}/history",
                                  "as_of": bars[-1]["t"], "granularity": "日线（未复权）"}
    except Exception as e:
        errs.append(f"Yahoo: {str(e)[:80]}")
    raise LookupError("日线都取不到：" + "；".join(errs))


def volume_profile(symbol: str, source: str = "daily", days: int = 60, bins: int = 0, interval: int = 5, bars=None) -> dict:
    source = "intraday" if str(source).lower() in ("intraday", "minute", "分钟", "分钟线") else "daily"
    if bars is None:
        try:
            bars, meta = fetch_bars(symbol, source, days, interval)
        except Exception as e:
            return {"symbol": symbol, "error": str(e)[:300]}
    else:
        meta = {"source": "调用方提供", "as_of": bars[-1].get("t") if bars else None, "granularity": source}
    nb = int(bins or 0) or (40 if source == "daily" else 50)
    r = profile(bars, bins=nb)
    if "error" in r:
        return {"symbol": symbol, **meta, **r}
    out = {"symbol": symbol, **meta, "bars": len(bars), "period": f"{bars[0].get('t')} → {bars[-1].get('t')}", **r,
           "note": ("近似口径：每根 K 线的成交量均匀摊到当根最高—最低区间；" +
                    ("日线粒度粗，节点位置只到大约价位。" if source == "daily" else "分钟线较接近真实逐价成交。") +
                    " 平衡=价值区内来回（震荡思路：上沿附近谨慎、下沿附近关注）；失衡=价格被新价位接受且价值迁移（趋势思路）。这是结构描述，不是买卖信号。")}
    if not math.isfinite(out.get("poc") or float("nan")):
        return {"symbol": symbol, "error": "计算结果异常"}
    return out


TOOL_NAMES = {"volume_profile"}
TOOL_SPECS = [
    {"name": "volume_profile", "description": "成交分布（Volume Profile）：用日线或分钟线近似每个价位的成交量，给 POC（成交最密价位）、"
     "高/低成交量节点（HVN 支撑阻力、LVN 价格容易快速穿过）、70% 价值区 VAL–VAH，并判断现在是平衡（震荡）还是失衡（趋势）。"
     "问「筹码/成交密集区在哪」「是震荡还是趋势」「支撑阻力」时用。source=daily（默认近 60 根日线）或 intraday（近 1–5 天分钟线）。",
     "parameters": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "source": {"type": "string", "enum": ["daily", "intraday"]},
         "days": {"type": "integer", "description": "日线根数（默认 60），分钟线为天数（1–5）"},
         "bins": {"type": "integer", "description": "价格分档数，默认日线 40、分钟线 50"}}, "required": ["symbol"]}},
]


def run_tool(name: str, a: dict):
    r = volume_profile(str(a.get("symbol", "")).strip(), a.get("source") or "daily", a.get("days") or (60 if (a.get("source") or "daily") == "daily" else 5),
                       a.get("bins") or 0)
    if "profile" in r:  # 给模型看的结果不带 40 档明细（卡片另取）
        r = dict(r)
        r["_profile_bins"] = len(r.pop("profile"))
    return r
