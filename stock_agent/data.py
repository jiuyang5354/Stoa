"""行情数据：Yahoo Finance 为主；A股/港股历史不全时用东方财富日K补齐，再不行用昨收兜底。"""
import warnings

import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")
MIN_ROWS = 25


def _eastmoney_secid(symbol: str) -> str | None:
    s = symbol.upper()
    if s.endswith(".SS"):
        return f"1.{s[:-3]}"
    if s.endswith(".SZ"):
        return f"0.{s[:-3]}"
    if s.endswith(".HK"):
        code = s[:-3]
        return f"124.{code}" if not code.isdigit() else f"116.{code.zfill(5)}"
    if s == "^HSI":
        return "100.HSI"
    return None


def _eastmoney_closes(symbol: str, limit: int = 260) -> pd.Series | None:
    secid = _eastmoney_secid(symbol)
    if not secid:
        return None
    try:
        r = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={"secid": secid, "fields1": "f1", "fields2": "f51,f53", "klt": 101, "fqt": 1, "lmt": limit},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            timeout=15,
        )
        klines = (r.json().get("data") or {}).get("klines") or []
    except Exception:
        return None
    if not klines:
        return None
    rows = [k.split(",") for k in klines]
    return pd.Series([float(c) for _, c in rows], index=pd.to_datetime([d for d, _ in rows]), name=symbol)


def _prev_close_series(symbol: str) -> pd.Series | None:
    try:
        fi = yf.Ticker(symbol).fast_info
        last, prev = fi.last_price, fi.previous_close
    except Exception:
        return None
    if not last or not prev:
        return None
    today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)
    return pd.Series([prev, last], index=[today - pd.Timedelta(days=1), today], name=symbol)


def fetch_closes(symbols: list[str], period: str = "1y") -> dict[str, pd.Series]:
    if not symbols:
        return {}
    out: dict[str, pd.Series] = {}
    try:
        df = yf.download(symbols, period=period, progress=False, auto_adjust=False, threads=True)
        closes = df["Close"]
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(symbols[0])
        for s in symbols:
            if s in closes.columns:
                ser = closes[s].dropna()
                ser.index = pd.to_datetime(ser.index).tz_localize(None) if ser.index.tz is not None else ser.index
                out[s] = ser
    except Exception:
        pass
    for s in symbols:
        if len(out.get(s, [])) >= MIN_ROWS:
            continue
        alt = _eastmoney_closes(s)
        if alt is not None and len(alt) >= MIN_ROWS:
            out[s] = alt
            continue
        if len(out.get(s, [])) < 2:
            alt = _prev_close_series(s)
            if alt is not None:
                out[s] = alt
    return out


def fetch_news(symbol: str, limit: int = 3) -> list[dict]:
    """Yahoo Finance 上的个股新闻标题（英文为主）。"""
    try:
        items = yf.Ticker(symbol).news or []
    except Exception:
        return []
    out = []
    for it in items[:limit]:
        c = it.get("content", it)
        title = c.get("title")
        url = (c.get("canonicalUrl") or {}).get("url") or c.get("link")
        if title:
            out.append({"title": title, "url": url})
    return out
