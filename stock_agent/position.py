"""仓位上限（v0.40）：纯 Python 确定性计算，不调模型。

思路参考 virattt/ai-hedge-fund 的 risk/limits：观点归模型，钳制归算术。
几道约束各算一个金额上限，取最小；被钳掉的部分留现金，不再分配给别的标的。
  1. 单票上限：资金 × max_weight（保守 10% / 稳健 20% / 积极 30%，config.yaml risk.max_weight）
  2. 流动性：20 日均成交额（ADV）× adv_pct（默认 1%）
  3. 风险预算：止损下的亏损 ≤ 资金 × risk_per_trade（保守 0.5% / 稳健 1% / 积极 2%）
止损参考价 = 现价 − atr_mult × ATR14（用户给了止损幅度就用用户的）。
A股按 100 股一手向下取整；美股 1 股；港股每手股数因股而异，取不到就说明、按 1 股算。
结果只是算术上限，不是买入建议。
"""
from __future__ import annotations

import math
import re

LEVELS = ("保守", "稳健", "积极")
EN_LEVEL = {"conservative": "保守", "moderate": "稳健", "balanced": "稳健", "aggressive": "积极"}
DEFAULTS = {
    "max_weight": {"保守": 0.10, "稳健": 0.20, "积极": 0.30},
    "adv_pct": 0.01,
    "risk_per_trade": {"保守": 0.005, "稳健": 0.01, "积极": 0.02},
    "atr_mult": {"保守": 1.5, "稳健": 2.0, "积极": 2.5},
}

TOOL_NAMES = {"position_limit"}
TOOL_SPECS = [{
    "name": "position_limit",
    "description": "仓位上限测算（确定性计算，非买入建议）：单票比例、20 日均成交额 1%、ATR 止损下的亏损预算三者取最小，A股按手取整。用户给了资金或持仓、问买多少/仓位时用。",
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "capital": {"type": "number", "description": "可用资金（标的货币）"},
        "risk": {"type": "string", "enum": list(LEVELS)}, "stop_pct": {"type": "number", "description": "止损幅度%，可选"},
        "held": {"type": "number", "description": "已持股数，可选"}}, "required": ["symbol"]}},
]


def cfg() -> dict:
    """config.yaml 的 risk 段覆盖默认值（读不到就用默认）。"""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    try:
        from .agent import load_cfg
        r = (load_cfg() or {}).get("risk") or {}
    except Exception:
        r = {}
    for k, v in r.items():
        if k not in out:
            continue
        if isinstance(out[k], dict) and isinstance(v, dict):
            for lv, x in v.items():
                try:
                    out[k][str(lv)] = float(x)
                except (TypeError, ValueError):
                    pass
        elif not isinstance(out[k], dict):
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def level(risk) -> str:
    r = str(risk or "").strip()
    return r if r in LEVELS else EN_LEVEL.get(r.lower(), "稳健")


def market(symbol: str) -> str:
    s = (symbol or "").upper()
    if re.match(r"^\d{6}\.(SS|SZ|SH|BJ)$", s) or re.match(r"^\d{6}$", s):
        return "A"
    if s.endswith(".HK"):
        return "HK"
    return "US"


CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD"}


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def atr(bars: list[dict], n: int = 14) -> float | None:
    """ATR：真实波幅的简单均值（没有高低价时退化为 |Δ收盘|）。"""
    if len(bars) < 2:
        return None
    trs = []
    for prev, b in zip(bars[:-1], bars[1:]):
        h, l, pc = b.get("h"), b.get("l"), prev["c"]
        if isinstance(h, (int, float)) and isinstance(l, (int, float)) and h >= l:
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        else:
            trs.append(abs(b["c"] - pc))
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else None


def vol20(bars: list[dict]) -> float | None:
    """近 20 日日收益（对数）标准差，年化 ×√252，单位 %。"""
    cs = [b["c"] for b in bars[-21:] if b.get("c")]
    rs = [math.log(b / a) for a, b in zip(cs[:-1], cs[1:]) if a > 0 and b > 0]
    if len(rs) < 5:
        return None
    m = sum(rs) / len(rs)
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1))
    return sd * math.sqrt(252) * 100


def adv20(bars: list[dict], vol_mult: float = 1.0) -> float | None:
    """20 日均成交额 = mean(收盘 × 成交量 × 单位换算)。"""
    rows = [b["c"] * b["v"] * vol_mult for b in bars[-20:] if b.get("c") and b.get("v")]
    return sum(rows) / len(rows) if rows else None


def get_bars(symbol: str) -> tuple[list[dict], dict]:
    from .volprofile import fetch_bars
    bars, meta = fetch_bars(symbol, "daily", 30)
    # 腾讯日线的 A股成交量单位是「手」（100 股），Yahoo / 腾讯港美股是「股」
    meta = dict(meta)
    meta["vol_mult"] = 100.0 if (meta.get("source") == "腾讯证券" and market(symbol) == "A") else 1.0
    return bars, meta


def compute(symbol: str, capital=None, risk="稳健", stop_pct=None, held=None, bars=None, meta=None, conf=None) -> dict:
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "缺少代码"}
    if bars is None:
        try:
            bars, meta = get_bars(sym)
        except Exception as e:
            return {"symbol": sym, "error": f"取不到日线，算不了仓位上限：{str(e)[:160]}"}
    meta = meta or {}
    bars = [b for b in bars if isinstance(b.get("c"), (int, float)) and b["c"] > 0]
    if len(bars) < 15:
        return {"symbol": sym, "error": f"{sym} 日线不足 15 根，算不了仓位上限"}
    c = conf or cfg()
    lv = level(risk)
    mk = market(sym)
    cur = CURRENCY[mk]
    last = float(bars[-1]["c"])
    a14 = atr(bars)
    v20 = vol20(bars)
    adv = adv20(bars, float(meta.get("vol_mult") or 1.0))
    cap = float(capital) if isinstance(capital, (int, float)) and capital > 0 else None
    held_n = float(held) if isinstance(held, (int, float)) and held > 0 else 0.0

    # 止损参考价
    if isinstance(stop_pct, (int, float)) and 0 < stop_pct < 100:
        stop_px, stop_how = last * (1 - stop_pct / 100), f"用户给的止损幅度 {_r(stop_pct)}%"
    elif a14:
        k = c["atr_mult"].get(lv, 2.0)
        stop_px, stop_how = last - k * a14, f"现价 − {k:g} × ATR14（{_r(a14, 3)}）"
    else:
        stop_px, stop_how = None, "没有 ATR，未给止损参考"
    if stop_px is not None and stop_px <= 0:
        stop_px = None
    per_share_loss = (last - stop_px) if stop_px else None

    lot = 100 if mk == "A" else 1
    lot_note = ""
    if mk == "A" and sym.startswith("688"):
        lot_note = "科创板单笔至少 200 股、之后可按 1 股递增，这里仍按 100 股取整。"
    if mk == "HK":
        lot_note = "港股每手股数因股票而异，没有取到该股每手股数，按 1 股取整；下单前以券商显示的每手股数为准。"

    constraints = []
    w = c["max_weight"].get(lv, 0.2)
    if cap:
        constraints.append({"name": "单票上限", "rule": f"资金 × {w:.0%}（{lv}）", "amount": cap * w})
    if adv:
        constraints.append({"name": "流动性", "rule": f"20 日均成交额 × {c['adv_pct']:.2%}", "amount": adv * c["adv_pct"]})
    rpt = c["risk_per_trade"].get(lv, 0.01)
    if cap and per_share_loss and per_share_loss > 0:
        constraints.append({"name": "风险预算", "rule": f"止损下亏损 ≤ 资金 × {rpt:.1%}", "amount": cap * rpt / per_share_loss * last})
    for x in constraints:
        x["shares"] = int(x["amount"] / last // lot * lot)
        x["amount"] = _r(x["amount"])
        if cap and x["amount"] <= cap:
            x["pct"] = _r(x["amount"] / cap * 100)

    out = {"symbol": sym, "currency": cur, "last": _r(last, 3), "as_of": str(bars[-1].get("t") or meta.get("as_of") or ""),
           "source": meta.get("source") or "", "url": meta.get("url") or "", "risk": lv,
           "capital": _r(cap) if cap else None, "adv20": _r(adv), "atr14": _r(a14, 3), "vol20_ann_pct": _r(v20),
           "stop_ref": _r(stop_px, 3), "stop_pct": _r((1 - stop_px / last) * 100) if stop_px else None, "stop_method": stop_how,
           "constraints": constraints, "lot": lot}
    if not constraints:
        out["error"] = "缺少资金且取不到成交额，算不出上限"
        return out
    bind = min(constraints, key=lambda x: x["amount"])
    sh = int(max(0.0, bind["shares"] - held_n) // lot * lot)
    amt = sh * last
    out["binding"] = bind["name"]
    out["final"] = {"shares": sh, "amount": _r(amt), "pct": _r(amt / cap * 100) if cap else None}
    if held_n:
        out["held"] = int(held_n)
        out["final"]["note"] = f"已持有 {int(held_n)} 股，上限按总持仓算，可再买 {sh} 股"
    if per_share_loss and sh:
        out["max_loss"] = {"amount": _r(sh * per_share_loss), "pct_of_capital": _r(sh * per_share_loss / cap * 100) if cap else None}
    if cap:
        out["cash_left"] = _r(cap - amt)
    if sh == 0 and bind["amount"] < lot * last and not held_n:
        out["final"]["note"] = f"一手（{lot} 股 ≈ {_r(lot * last)} {cur}）已超过「{bind['name']}」上限 {bind['amount']}，按这些约束买不了一手"
    if lot_note:
        out["lot_note"] = lot_note
    if not cap:
        out["note_capital"] = "没给资金，只算了流动性上限；给出可用资金才能算单票比例和风险预算。"
    out["note"] = ("算术上限，不是买入建议；被约束钳掉的资金留现金，不再分配给别的标的。资金按标的货币（" + cur + "）理解。")
    return out


def run_tool(name: str, a: dict, agent=None):
    def num(x):
        try:
            return float(str(x).replace(",", "")) if x not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return compute(str(a.get("symbol") or ""), num(a.get("capital")), a.get("risk") or "稳健", num(a.get("stop_pct")), num(a.get("held")))
