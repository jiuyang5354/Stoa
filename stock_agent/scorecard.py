"""判断事后打分（Rationes）：每笔判断按「到期日收盘」结算，对照同市场基准算超额，记下途中最大浮盈/浮亏。

- 只用日线收盘，不调用模型；复盘晚了几天也按到期那天的收盘算，不拿今天的价冒充。
- 基准：A股 沪深300（000300.SS）、港股 恒生指数（^HSI）、美股 标普500（^GSPC）。
- 方向分（edge）：看涨 = 超额，看跌 = −超额，震荡 = 3% − |涨跌|（区间内为正）。
- 下次再分析同一只股票时，get_quotes / record_call 会带上这只股票过去的判断与打分（track）。
"""
from __future__ import annotations

import pandas as pd

BENCH = {"A股": "000300.SS", "港股": "^HSI", "美股": "^GSPC"}
BENCH_NAME = {"000300.SS": "沪深300", "^HSI": "恒生指数", "^GSPC": "标普500"}
RANGE_BAND = 3.0  # 震荡判断的区间（±%）


def market(sym: str) -> str:
    s = str(sym).upper()
    if s.endswith((".SS", ".SZ", ".BJ")):
        return "A股"
    if s.endswith(".HK") or s in ("^HSI", "^HSCE", "^HSTECH"):
        return "港股"
    return "美股"


def bench_of(sym: str) -> str | None:
    b = BENCH[market(sym)]
    return None if str(sym).upper() == b.upper() else b


def _day(s) -> pd.Timestamp:
    return pd.Timestamp(str(s)[:10])


def _idx(ser: pd.Series) -> pd.Series:
    ser = ser.dropna()
    ser.index = pd.to_datetime(ser.index).tz_localize(None) if getattr(ser.index, "tz", None) else pd.to_datetime(ser.index)
    return ser.sort_index()


def _on_or_before(ser: pd.Series, d: pd.Timestamp):
    s = ser[ser.index <= d]
    return (s.index[-1], float(s.iloc[-1])) if len(s) else (None, None)


def _on_or_after(ser: pd.Series, d: pd.Timestamp):
    s = ser[ser.index >= d]
    return (s.index[0], float(s.iloc[0])) if len(s) else (None, None)


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if a and b else None


def edge(view: str, pct, alpha):
    if view == "看涨":
        return alpha if alpha is not None else pct
    if view == "看跌":
        return -(alpha if alpha is not None else pct) if pct is not None else None
    if view == "震荡" and pct is not None:
        return round(RANGE_BAND - abs(pct), 2)
    return None


def verdict(view: str, pct) -> str:
    if pct is None:
        return ""
    right = (view == "看涨" and pct > 1) or (view == "看跌" and pct < -1) or (view == "震荡" and abs(pct) <= RANGE_BAND)
    return "对" if right else "错"


def score(call: dict, ser: pd.Series | None, bser: pd.Series | None = None, final: bool = True) -> dict | None:
    """final=True：按到期日（之后第一个交易日）收盘结算，数据还没到期就返回 None；final=False：按最新收盘盯市。"""
    if ser is None or len(ser) < 1:
        return None
    ser = _idx(ser)
    start, due = _day(call["ts"]), _day(call["due"])
    if final:
        end_d, end_px = _on_or_after(ser, due)
        if end_d is None:
            return None
    else:
        end_d, end_px = ser.index[-1], float(ser.iloc[-1])
    entry = float(call["price"] or 0) or _on_or_before(ser, start)[1]
    pct = _pct(end_px, entry)
    win = ser[(ser.index > start) & (ser.index <= end_d)]
    mfe = _pct(float(win.max()), entry) if len(win) else None
    mae = _pct(float(win.min()), entry) if len(win) else None
    b = bench_of(call["symbol"])
    bpct = alpha = None
    if bser is not None and len(bser) > 1 and b:
        bser = _idx(bser)
        b0, b1 = _on_or_before(bser, start)[1], _on_or_before(bser, end_d)[1]
        bpct = _pct(b1, b0)
        alpha = round(pct - bpct, 2) if pct is not None and bpct is not None else None
    return {"end_date": end_d.strftime("%Y-%m-%d"), "end_price": round(end_px, 4), "pct": pct,
            "bench": b, "bench_pct": bpct, "alpha": alpha, "mfe": mfe, "mae": mae,
            "edge": edge(call["view"], pct, alpha), "verdict": verdict(call["view"], pct)}


def _closes(symbols: list[str]) -> dict:
    from . import data
    try:
        return data.fetch_closes(sorted(set(s for s in symbols if s)), "1y")
    except Exception:
        return {}


def settle_due(mem, closes: dict | None = None) -> list[str]:
    """到期判断按到期日收盘结算并写回账本；返回给模型看的复盘句子。"""
    due = mem.due_calls()
    if not due:
        return []
    syms = [c["symbol"] for c in due] + [bench_of(c["symbol"]) for c in due]
    closes = closes if closes is not None else _closes([s for s in syms if s])
    notes = []
    for c in due:
        r = score(c, closes.get(c["symbol"]), closes.get(bench_of(c["symbol"]) or ""), final=True)
        if not r:
            continue
        mem.close_call(c["id"], r["end_price"], r["pct"], r["verdict"], r)
        name = c.get("name") or c["symbol"]
        rel = (f"，同期{BENCH_NAME.get(r['bench'], r['bench'])} {r['bench_pct']:+.2f}%，超额 {r['alpha']:+.2f}%" if r["alpha"] is not None else "")
        path = (f"，途中最高 {r['mfe']:+.2f}%、最低 {r['mae']:+.2f}%" if r["mfe"] is not None else "")
        notes.append(f"{c['ts'][:10]} 判断 {name} {c['view']}（{c['horizon_days']}天，理由：{c['reason']}），"
                     f"到期日 {r['end_date']} 收 {r['end_price']}，{r['pct']:+.2f}%{rel}{path}，判断{r['verdict']}。")
    return notes


def track(mem, symbol: str, quote: dict | None = None, limit: int = 4) -> list[dict]:
    """这只股票过去的判断：已结算的给到期打分，未到期的按最新收盘盯市（不额外取数据）。"""
    sym = str(symbol).upper()
    rows = [c for c in mem.recent_calls(2000) if str(c["symbol"]).upper() == sym][-limit:]
    out = []
    for c in rows:
        item = {"date": c["ts"][:10], "view": c["view"], "horizon_days": c["horizon_days"], "price": c["price"],
                "confidence": c.get("confidence"), "reason": (c.get("reason") or "")[:60]}
        if c["status"] == "closed":
            item.update({"status": "已结算", "pct": c.get("result_pct"), "alpha": c.get("alpha_pct"), "verdict": c.get("verdict")})
        else:
            item["status"] = f"未到期（{c['due']}）"
            px = (quote or {}).get("close") if isinstance(quote, dict) else None
            if px and c["price"]:
                item["pct_so_far"] = _pct(float(px), float(c["price"]))
        out.append(item)
    return out


def track_note(items: list[dict]) -> str:
    if not items:
        return ""
    bits = []
    for t in items:
        if t["status"] == "已结算":
            a = f"，超额 {t['alpha']:+.2f}%" if t.get("alpha") is not None else ""
            bits.append(f"{t['date']} {t['view']}{t['horizon_days']}天 → {t['pct']:+.2f}%{a}，{t['verdict']}")
        else:
            s = f"，至今 {t['pct_so_far']:+.2f}%" if t.get("pct_so_far") is not None else ""
            bits.append(f"{t['date']} {t['view']}{t['horizon_days']}天 {t['status']}{s}")
    return "你以前对这只股票的判断：" + "；".join(bits) + "。新判断要说明和上次相比什么变了。"


def stats(rows: list[dict]) -> dict:
    """超额口径的战绩：平均超额、方向分胜率、按置信度分组（校准；高 ≥75、中 60–74、低 <60）。rows 是已结算的判断。"""
    rs = [r for r in rows if r.get("edge") is not None]
    if not rs:
        return {}
    n = len(rs)
    out = {"超额样本": n, "方向分胜率%": round(sum(r["edge"] > 0 for r in rs) / n * 100, 1),
           "平均方向分%": round(sum(r["edge"] for r in rs) / n, 2)}
    al = [r["alpha"] for r in rs if r.get("alpha") is not None and r.get("view") == "看涨"]
    if al:
        out["看涨平均超额%"] = round(sum(al) / len(al), 2)
    tiers = {}
    for r in rs:
        c = r.get("confidence")
        if c is None:
            continue
        k = "高" if c >= 75 else "中" if c >= 60 else "低"
        tiers.setdefault(k, []).append(r)
    if tiers:
        out["按置信度"] = {k: {"n": len(v), "胜率%": round(sum(x["edge"] > 0 for x in v) / len(v) * 100, 1)} for k, v in tiers.items()}
        hi, lo = tiers.get("高"), tiers.get("低") or tiers.get("中")
        if hi and lo and len(hi) >= 5 and len(lo) >= 5:
            h = sum(x["edge"] > 0 for x in hi) / len(hi)
            l = sum(x["edge"] > 0 for x in lo) / len(lo)
            out["校准"] = "高置信度确实更准" if h > l + 0.05 else "高置信度并不更准：别把置信度说得太满"
    return out

