import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> float | None:
    close = close.dropna()
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    val = 100 - 100 / (1 + rs.iloc[-1])
    return None if pd.isna(val) else round(float(val), 1)


def pct(a: float, b: float) -> float | None:
    if b is None or a is None or b == 0 or pd.isna(a) or pd.isna(b):
        return None
    return round((a / b - 1) * 100, 2)


def analyze(close: pd.Series, alerts: dict) -> dict:
    """单只标的的关键数据和信号。"""
    close = close.dropna()
    if len(close) < 2:
        return {}
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    ma20 = float(close.tail(20).mean()) if len(close) >= 20 else None
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else None
    r = rsi(close)
    out = {
        "date": close.index[-1].strftime("%Y-%m-%d"),
        "close": round(last, 3),
        "chg_pct": pct(last, prev),
        "chg_5d": pct(last, float(close.iloc[-6])) if len(close) > 5 else None,
        "chg_20d": pct(last, float(close.iloc[-21])) if len(close) > 20 else None,
        "ma20": round(ma20, 3) if ma20 else None,
        "ma60": round(ma60, 3) if ma60 else None,
        "rsi": r,
        "high_52w": round(float(close.tail(250).max()), 3) if len(close) >= 60 else None,
        "low_52w": round(float(close.tail(250).min()), 3) if len(close) >= 60 else None,
        "high_20d": round(float(close.iloc[-21:-1].max()), 3) if len(close) >= 21 else None,  # 不含当天：突破判断用
        "low_20d": round(float(close.iloc[-21:-1].min()), 3) if len(close) >= 21 else None,
        # 近 20 日年化波动率（%），风控红线用
        "vol_20d": round(float(close.pct_change().tail(20).std() * (252 ** 0.5) * 100), 1) if len(close) >= 21 else None,
    }
    sig = []
    move = alerts.get("daily_move_pct", 3.0)
    if out["chg_pct"] is not None and abs(out["chg_pct"]) >= move:
        sig.append(f"单日{'大涨' if out['chg_pct'] > 0 else '大跌'}")
    if r is not None and r >= alerts.get("rsi_overbought", 70):
        sig.append("RSI 超买")
    if r is not None and r <= alerts.get("rsi_oversold", 30):
        sig.append("RSI 超卖")
    if ma20 and len(close) >= 21:
        prev_ma20 = float(close.iloc[-21:-1].mean())
        if prev <= prev_ma20 and last > ma20:
            sig.append("上穿 20 日线")
        elif prev >= prev_ma20 and last < ma20:
            sig.append("跌破 20 日线")
    if out["high_52w"] and last >= out["high_52w"]:
        sig.append("创 52 周新高")
    elif out["low_52w"] and last <= out["low_52w"]:
        sig.append("创 52 周新低")
    out["signals"] = sig
    return out
