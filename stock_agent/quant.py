"""确定性计算（v0.26）：数字由代码算，模型只负责解读。

- calc：安全四则运算/函数表达式，任何派生数字（占比、增速、差额、倍数）都应该走这里。
- price_levels：用收盘价摆动点聚类出支撑/阻力位，附走势供对话内画图。
- financial_bridge：最近一期 营收 → 成本 → 毛利 → 费用及其他 → 净利 的瀑布，及毛利率/净利率等比率。
- option_payoff：期权/正股组合到期收益曲线、盈亏平衡点、最大盈亏。
- trade_replay：「行情重放」——用该股过去几年的日收益分块重抽样，模拟上万条路径，看一个交易计划先碰止损还是先碰目标。
"""
from __future__ import annotations

import ast
import math
import operator as op

import numpy as np

# ---------------- calc ----------------
_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Pow: op.pow, ast.Mod: op.mod, ast.FloorDiv: op.floordiv}
_UN = {ast.UAdd: op.pos, ast.USub: op.neg}
_FN = {"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "sqrt": math.sqrt, "log": math.log, "exp": math.exp,
       "pct": lambda a, b: (a / b - 1) * 100, "share": lambda a, b: a / b * 100, "cagr": lambda a, b, n: ((a / b) ** (1 / n) - 1) * 100}


def _ev(n, env):
    if isinstance(n, ast.Expression):
        return _ev(n.body, env)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.Name):
        if n.id in env:
            return env[n.id]
        raise ValueError(f"未定义的变量 {n.id}")
    if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
        a, b = _ev(n.left, env), _ev(n.right, env)
        if isinstance(n.op, ast.Pow) and abs(b) > 100:
            raise ValueError("指数过大")
        return _BIN[type(n.op)](a, b)
    if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
        return _UN[type(n.op)](_ev(n.operand, env))
    if isinstance(n, (ast.List, ast.Tuple)):
        return [_ev(x, env) for x in n.elts]
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FN and not n.keywords:
        return _FN[n.func.id](*[_ev(a, env) for a in n.args])
    raise ValueError("只支持数字、变量、+ - * / ** %、括号和 abs/round/min/max/sum/sqrt/log/exp/pct/share/cagr")


def calc(expressions, variables: dict | None = None) -> dict:
    """expressions：一个表达式或 {名字: 表达式}；variables：{名字: 数字}。前面算出的结果可在后面的表达式里引用。"""
    env = {k: float(v) for k, v in (variables or {}).items() if isinstance(v, (int, float)) or _isnum(v)}
    items = expressions.items() if isinstance(expressions, dict) else [("result", expressions)]
    out = {}
    for k, e in items:
        if len(str(e)) > 300:
            return {"error": f"{k} 表达式太长"}
        try:
            v = _ev(ast.parse(str(e), mode="eval"), env)
            v = [round(x, 6) for x in v] if isinstance(v, list) else round(float(v), 6)
            out[k] = v
            if not isinstance(v, list):
                env[k] = v
        except Exception as ex:
            out[k] = None
            out.setdefault("errors", {})[k] = str(ex)[:120]
    return {"results": out, "note": "确定性计算结果，回答里的派生数字以此为准。"}


def _isnum(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


# ---------------- price_levels ----------------
def levels_from_closes(closes, window: int = 5, tol: float = 0.015, k: int = 3) -> dict:
    c = np.asarray(closes, dtype=float)
    n = len(c)
    if n < window * 2 + 5:
        return {"supports": [], "resistances": []}
    piv = []
    for i in range(window, n - window):
        seg = c[i - window:i + window + 1]
        if c[i] == seg.max() or c[i] == seg.min():
            piv.append((c[i], i))
    piv.sort()
    clusters = []  # [价位均值, 触碰次数, 最近一次位置]
    for v, i in piv:
        if clusters and abs(v / clusters[-1][0] - 1) <= tol:
            cl = clusters[-1]
            cl[0] = (cl[0] * cl[1] + v) / (cl[1] + 1)
            cl[1] += 1
            cl[2] = max(cl[2], i)
        else:
            clusters.append([v, 1, i])
    last = c[-1]
    score = lambda cl: cl[1] + cl[2] / n  # 触碰越多、越近越重要
    sup = sorted([cl for cl in clusters if cl[0] < last * (1 - tol / 3)], key=lambda cl: -score(cl))[:k]
    res = sorted([cl for cl in clusters if cl[0] > last * (1 + tol / 3)], key=lambda cl: -score(cl))[:k]
    if not sup:  # 创新低时没有下方摆动点：用区间最低收盘兜底
        sup = [[float(c.min()), 1, int(c.argmin())]] if c.min() < last else []
    if not res:
        res = [[float(c.max()), 1, int(c.argmax())]] if c.max() > last else []
    fmt = lambda cl: {"price": round(float(cl[0]), 3), "touches": int(cl[1]), "dist_pct": round(float(cl[0] / last - 1) * 100, 2), "last_touch_bars_ago": int(n - 1 - cl[2])}
    return {"supports": [fmt(x) for x in sorted(sup, key=lambda cl: -cl[0])], "resistances": [fmt(x) for x in sorted(res, key=lambda cl: cl[0])]}


def price_levels(symbol: str, days: int = 250) -> dict:
    from .data import fetch_closes
    period = "2y" if days > 252 else "1y"
    ser = fetch_closes([symbol], period=period).get(symbol)
    if ser is None or len(ser) < 30:
        return {"error": f"{symbol} 行情不足，算不了支撑阻力"}
    ser = ser.iloc[-days:]
    lv = levels_from_closes(ser.values)
    step = max(1, len(ser) // 160)
    pts = [[d.strftime("%Y-%m-%d"), round(float(v), 3)] for d, v in list(ser.items())[::step]]
    if pts[-1][0] != ser.index[-1].strftime("%Y-%m-%d"):
        pts.append([ser.index[-1].strftime("%Y-%m-%d"), round(float(ser.iloc[-1]), 3)])
    return {"symbol": symbol, "last": round(float(ser.iloc[-1]), 3), **lv, "points": pts, "as_of": ser.index[-1].strftime("%Y-%m-%d"),
            "source": ser.attrs.get("source", ""), "method": f"近 {len(ser)} 个交易日收盘价摆动点（前后各 5 日极值）按 1.5% 聚类；touches=触碰次数",
            "note": "支撑阻力是代码按历史价格算的参考位，不代表会在此止跌或受阻。"}


# ---------------- financial_bridge ----------------
def _bridge_steps(rev, gross, op_inc, net):
    steps = [{"label": "营收", "value": rev, "kind": "total"}]
    if gross is not None:
        steps += [{"label": "营业成本", "value": -(rev - gross), "kind": "neg"}, {"label": "毛利", "value": gross, "kind": "total"}]
        base = gross
    else:
        base = rev
    if op_inc is not None:
        steps += [{"label": "经营费用", "value": op_inc - base, "kind": "neg" if op_inc < base else "pos"}, {"label": "经营利润", "value": op_inc, "kind": "total"}]
        base = op_inc
    if net is not None:
        steps += [{"label": "利息税项及其他", "value": net - base, "kind": "neg" if net < base else "pos"}, {"label": "净利润", "value": net, "kind": "total"}]
    return steps


def financial_bridge(symbol: str) -> dict:
    from . import research
    info = research.resolve(symbol)
    out = {"symbol": symbol, "name": info.get("name")}
    if info.get("market") == "A":
        try:
            cols = "REPORT_DATE_NAME,REPORT_DATE,TOTALOPERATEREVE,TOTALOPERATEREVETZ,PARENTNETPROFIT,PARENTNETPROFITTZ,XSMLL,XSJLL"
            r = research._get("https://datacenter.eastmoney.com/securities/api/data/v1/get",
                              params={"reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": cols, "filter": f'(SECUCODE="{research.secucode(info["code"])}")',
                                      "pageSize": 5, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
            rows = r.json()["result"]["data"]
            x = rows[0]
            rev, gm, net = float(x["TOTALOPERATEREVE"]), x.get("XSMLL"), x.get("PARENTNETPROFIT")
            gross = rev * float(gm) / 100 if gm is not None else None
            net = float(net) if net is not None else None
            out.update(period=x["REPORT_DATE_NAME"], currency="CNY", steps=_bridge_steps(rev, gross, None, net),
                       ratios={"毛利率%": _r(gm), "归母净利率%": _r(net / rev * 100 if net is not None and rev else None),
                               "营收同比%": _r(x.get("TOTALOPERATEREVETZ")), "归母净利同比%": _r(x.get("PARENTNETPROFITTZ"))},
                       history=[{"period": y["REPORT_DATE_NAME"], "revenue": y.get("TOTALOPERATEREVE"), "net": y.get("PARENTNETPROFIT")} for y in rows],
                       source="东方财富 F10 主要指标",
                       note="累计口径（年初至报告期末）；营业成本=营收×(1−毛利率)，费用及其他=毛利−归母净利，均为代码计算。")
            out["steps"] = [s if s["label"] != "利息税项及其他" else {**s, "label": "费用税项及其他"} for s in out["steps"]]
            out["steps"][-1]["label"] = "归母净利"
            return out
        except Exception as e:
            out["eastmoney_error"] = str(e)[:100]
    try:
        import yfinance as yf
        q = yf.Ticker(symbol).quarterly_income_stmt
        if q is None or q.empty:
            return {**out, "error": "取不到利润表"}
        c = q.columns[0]
        g = lambda k: float(q.at[k, c]) if k in q.index and q.at[k, c] == q.at[k, c] else None
        rev, gross, opi, net = g("Total Revenue"), g("Gross Profit"), g("Operating Income"), g("Net Income")
        if not rev:
            return {**out, "error": "利润表缺营收"}
        prev = q.columns[4] if len(q.columns) > 4 else None
        yoy = None
        if prev is not None and "Total Revenue" in q.index and q.at["Total Revenue", prev]:
            yoy = (rev / float(q.at["Total Revenue", prev]) - 1) * 100
        out.update(period=str(c.date()), steps=_bridge_steps(rev, gross, opi, net),
                   ratios={"毛利率%": _r(gross / rev * 100 if gross is not None else None), "经营利润率%": _r(opi / rev * 100 if opi is not None else None),
                           "净利率%": _r(net / rev * 100 if net is not None else None), "营收同比%": _r(yoy)},
                   source="Yahoo Finance 季度利润表", note="单季口径；各项差额均为代码计算。")
        return out
    except Exception as e:
        return {**out, "error": f"取不到利润表：{str(e)[:100]}"}


def _r(v, d=2):
    try:
        return round(float(v), d)
    except (TypeError, ValueError):
        return None


# ---------------- option_payoff ----------------
def option_payoff(legs: list[dict], spot: float, multiplier: float = 1) -> dict:
    if not legs or not spot:
        return {"error": "需要 legs 和 spot"}
    lo, hi = spot * 0.6, spot * 1.4
    strikes = [float(l.get("strike") or 0) for l in legs if l.get("strike")]
    if strikes:
        lo, hi = min(lo, min(strikes) * 0.85), max(hi, max(strikes) * 1.15)
    xs = np.concatenate([[0.0], np.linspace(lo, hi, 161)])  # 第 0 个点=价格归零，用来算真实最大亏损
    pnl = np.zeros_like(xs)
    for l in legs:
        t = str(l.get("type", "call")).lower()
        sgn = -1 if str(l.get("side", "long")).lower() in ("short", "sell", "卖", "卖出") else 1
        q = float(l.get("qty") or 1)
        k, prem = float(l.get("strike") or 0), float(l.get("premium") or 0)
        if t in ("stock", "share", "正股"):
            leg = xs - (float(l.get("price") or prem or spot))
        elif t in ("put", "认沽"):
            leg = np.maximum(k - xs, 0) - prem
        else:
            leg = np.maximum(xs - k, 0) - prem
        pnl += sgn * q * leg * multiplier
    p0, xs, pnl = pnl[0], xs[1:], pnl[1:]
    be = [round(float(xs[i] - pnl[i] * (xs[i + 1] - xs[i]) / (pnl[i + 1] - pnl[i])), 3) for i in range(len(xs) - 1) if pnl[i] == 0 or pnl[i] * pnl[i + 1] < 0]
    slope_hi = pnl[-1] - pnl[-2]
    return {"spot": spot, "legs": legs, "multiplier": multiplier, "breakevens": be,
            "max_profit": "无上限" if slope_hi > 1e-9 else round(float(pnl.max()), 2),
            "max_loss": "无下限" if slope_hi < -1e-9 else round(float(min(pnl.min(), p0)), 2),
            "pnl_at_spot": round(float(np.interp(spot, xs, pnl)), 2),
            "points": [[round(float(x), 3), round(float(p), 3)] for x, p in zip(xs[::4], pnl[::4])], "source": "代码计算（到期收益，不含手续费）"}


# ---------------- trade_replay ----------------
def replay_paths(rets, entry, stop, target, horizon=20, n=10000, block=5, seed=7):
    r = np.asarray(rets, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < block * 4:
        raise ValueError("历史收益太少")
    rng = np.random.default_rng(seed)
    nb = math.ceil(horizon / block)
    starts = rng.integers(0, len(r) - block, size=(n, nb))
    idx = (starts[..., None] + np.arange(block)).reshape(n, -1)[:, :horizon]
    paths = entry * np.exp(np.cumsum(r[idx], axis=1))
    long = target > entry
    hit_t = paths >= target if long else paths <= target
    hit_s = paths <= stop if long else paths >= stop
    big = horizon + 1
    ft = np.where(hit_t.any(1), hit_t.argmax(1), big)
    fs = np.where(hit_s.any(1), hit_s.argmax(1), big)
    win, loss = ft < fs, fs < ft
    neither = ~(win | loss)
    exit_px = np.where(win, target, np.where(loss, stop, paths[:, -1]))
    ret = (exit_px / entry - 1) * (100 if long else -100)
    risk = abs(entry - stop) / entry * 100
    pct = lambda a: [round(float(np.percentile(a, p)), 2) for p in (5, 25, 50, 75, 95)]
    fan = [[int(d + 1), *[round(float(np.percentile(paths[:, d], p)), 3) for p in (5, 50, 95)]] for d in range(horizon)]
    sample = [[round(float(v), 3) for v in paths[i]] for i in range(0, n, max(1, n // 24))][:24]
    return {"n": n, "horizon": horizon, "p_target_first": round(float(win.mean()) * 100, 1), "p_stop_first": round(float(loss.mean()) * 100, 1),
            "p_neither": round(float(neither.mean()) * 100, 1), "exp_return_pct": round(float(ret.mean()), 2), "ret_pct_p5_25_50_75_95": pct(ret),
            "exp_R": round(float(ret.mean() / risk), 2) if risk else None, "median_days_to_exit": int(np.median(np.minimum(np.minimum(ft, fs), horizon - 1)) + 1),
            "fan": fan, "sample": sample}


def trade_replay(symbol: str, entry: float | None = None, stop: float | None = None, target: float | None = None,
                 horizon: int = 20, n: int = 10000, years: int = 3) -> dict:
    from .data import fetch_closes
    ser = fetch_closes([symbol], period="5y").get(symbol)
    if ser is not None:
        ser = ser.iloc[-(max(1, min(int(years), 5)) * 252 + 1):]
    if ser is None or len(ser) < 120:
        return {"error": f"{symbol} 历史行情不足（至少半年）"}
    last = float(ser.iloc[-1])
    entry = float(entry or last)
    if not stop or not target:
        return {"error": "需要 stop（止损价）和 target（目标价）", "last": last}
    stop, target = float(stop), float(target)
    if (target - entry) * (stop - entry) >= 0:
        return {"error": "止损和目标应在入场价两侧"}
    horizon, n = max(2, min(int(horizon), 120)), max(1000, min(int(n), 20000))
    rets = np.diff(np.log(ser.values.astype(float)))
    res = replay_paths(rets, entry, stop, target, horizon, n)
    return {"symbol": symbol, "entry": entry, "stop": stop, "target": target, "last": last, **res,
            "as_of": ser.index[-1].strftime("%Y-%m-%d"), "source": ser.attrs.get("source", ""),
            "method": f"近 {len(rets)} 个交易日日收益，5 日分块重抽样 {n} 条路径，按收盘价判断先触及止损还是目标",
            "note": "这是用历史波动做的压力测试，不是预测；没有考虑跳空、涨跌停和交易成本。择时信号经回测不显著。"}
