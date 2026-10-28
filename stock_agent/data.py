"""行情数据：可插拔多数据源（DATA_PROVIDERS），默认 Yahoo Finance → 东方财富 → 昨收兜底。"""
import os
import time
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


# ---------------- 可插拔行情源 ----------------
# DATA_PROVIDERS 按顺序尝试，默认 yahoo,eastmoney。代码统一用 Yahoo 格式（600519.SS / 000001.SZ / 0700.HK / AAPL / ^GSPC），
# 各家自己的代码格式在下面转换。拿到不少于 MIN_ROWS 天就停，都不够时用最长的那份，最后用昨收兜底。
A_INDEX = {"000001.SS", "000300.SS", "000016.SS", "000688.SS", "000905.SS", "000852.SS", "399001.SZ", "399006.SZ", "399005.SZ"}


def _split(symbol: str) -> tuple[str, str]:
    """返回 (市场, 代码)：市场 = sh / sz / hk / us / idx（境外指数）。"""
    s = symbol.upper()
    if s.startswith("^"):
        return "idx", s[1:]
    if s.endswith(".SS"):
        return "sh", s[:-3]
    if s.endswith(".SZ"):
        return "sz", s[:-3]
    if s.endswith(".HK"):
        return "hk", s[:-3]
    return "us", s


def _series(pairs, symbol) -> pd.Series | None:
    pairs = [(d, float(c)) for d, c in pairs if c not in (None, "")]
    if not pairs:
        return None
    ser = pd.Series([c for _, c in pairs], index=pd.to_datetime([d for d, _ in pairs]), name=symbol).sort_index()
    return ser[~ser.index.duplicated(keep="last")]


def _start(days: int) -> pd.Timestamp:
    return pd.Timestamp.now().normalize() - pd.Timedelta(days=int(days * 1.5) + 10)


def _get(url, **params):
    r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "stoa"})
    r.raise_for_status()
    return r.json()


def p_tushare(symbol, days):
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        return None
    mkt, code = _split(symbol)
    if mkt in ("sh", "sz"):
        api, ts = ("index_daily" if symbol.upper() in A_INDEX else "daily"), f"{code}.{mkt.upper()}"
    elif mkt == "hk":
        api, ts = "hk_daily", f"{code.zfill(5)}.HK"
    elif mkt == "us":
        api, ts = "us_daily", code
    else:
        return None
    r = requests.post(os.getenv("TUSHARE_URL", "http://api.tushare.pro"), timeout=20, json={
        "api_name": api, "token": token, "fields": "trade_date,close",
        "params": {"ts_code": ts, "start_date": _start(days).strftime("%Y%m%d")}}).json()
    d = r.get("data") or {}
    return _series([(row[0], row[1]) for row in d.get("items") or []], symbol)


def p_finnhub(symbol, days):
    key = os.getenv("FINNHUB_API_KEY")
    mkt, code = _split(symbol)
    if not key or mkt == "idx":
        return None
    sym = {"sh": f"{code}.SS", "sz": f"{code}.SZ", "hk": f"{code.zfill(4)}.HK"}.get(mkt, code)
    j = _get("https://finnhub.io/api/v1/stock/candle", symbol=sym, resolution="D",
             **{"from": int(_start(days).timestamp()), "to": int(time.time())}, token=key)
    if j.get("s") != "ok":
        return None
    return _series([(pd.Timestamp(t, unit="s").date(), c) for t, c in zip(j["t"], j["c"])], symbol)


def p_alphavantage(symbol, days):
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    mkt, code = _split(symbol)
    if not key or mkt == "idx":
        return None
    sym = {"sh": f"{code}.SHH", "sz": f"{code}.SHZ", "hk": f"{code.zfill(4)}.HKG"}.get(mkt, code)
    j = _get("https://www.alphavantage.co/query", function="TIME_SERIES_DAILY", symbol=sym,
             outputsize="full" if days > 100 else "compact", apikey=key)
    ts = j.get("Time Series (Daily)") or {}
    return _series([(d, v.get("4. close")) for d, v in ts.items()], symbol)


def p_polygon(symbol, days):
    key = os.getenv("POLYGON_API_KEY")
    mkt, code = _split(symbol)
    if not key or mkt not in ("us", "idx"):
        return None
    tk = {"GSPC": "I:SPX", "IXIC": "I:COMP", "DJI": "I:DJI", "NDX": "I:NDX"}.get(code, code) if mkt == "idx" else code
    j = _get(f"https://api.polygon.io/v2/aggs/ticker/{tk}/range/1/day/{_start(days):%Y-%m-%d}/{pd.Timestamp.now():%Y-%m-%d}",
             adjusted="true", sort="asc", limit=50000, apiKey=key)
    return _series([(pd.Timestamp(x["t"], unit="ms").date(), x["c"]) for x in j.get("results") or []], symbol)


def p_twelvedata(symbol, days):
    key = os.getenv("TWELVEDATA_API_KEY")
    mkt, code = _split(symbol)
    if not key:
        return None
    params = {"symbol": code, "interval": "1day", "outputsize": min(days + 5, 5000), "apikey": key}
    if mkt in ("sh", "sz", "hk"):
        params["exchange"] = {"sh": "SSE", "sz": "SZSE", "hk": "HKEX"}[mkt]
    if mkt == "idx":
        params["symbol"] = {"GSPC": "SPX", "IXIC": "IXIC", "DJI": "DJI", "HSI": "HSI"}.get(code, code)
    j = _get("https://api.twelvedata.com/time_series", **params)
    return _series([(v["datetime"], v["close"]) for v in j.get("values") or []], symbol)


def p_tiingo(symbol, days):
    key = os.getenv("TIINGO_API_KEY")
    mkt, code = _split(symbol)
    if not key or mkt != "us":
        return None
    j = _get(f"https://api.tiingo.com/tiingo/daily/{code.lower()}/prices", startDate=f"{_start(days):%Y-%m-%d}", token=key)
    return _series([(x["date"][:10], x.get("adjClose") or x["close"]) for x in j or []], symbol)


def p_fmp(symbol, days):
    key = os.getenv("FMP_API_KEY")
    mkt, code = _split(symbol)
    if not key:
        return None
    sym = {"sh": f"{code}.SS", "sz": f"{code}.SZ", "hk": f"{code.zfill(4)}.HK", "idx": f"^{code}"}.get(mkt, code)
    j = _get("https://financialmodelingprep.com/stable/historical-price-eod/light", symbol=sym,
             **{"from": f"{_start(days):%Y-%m-%d}"}, apikey=key)
    rows = j if isinstance(j, list) else j.get("historical") or []
    return _series([(x["date"], x.get("price", x.get("close"))) for x in rows], symbol)


def p_eastmoney(symbol, days):
    return _eastmoney_closes(symbol, max(days, 30))


def p_custom(symbol, days):
    """自建接口：DATA_CUSTOM_URL 里用 {symbol} {days} 占位，返回 [{"date": "...", "close": ...}] 或 {"data": [...]}。"""
    url = os.getenv("DATA_CUSTOM_URL")
    if not url:
        return None
    hdr = {"Authorization": f"Bearer {os.getenv('DATA_CUSTOM_TOKEN')}"} if os.getenv("DATA_CUSTOM_TOKEN") else {}
    j = requests.get(url.format(symbol=symbol, days=days), headers=hdr, timeout=20).json()
    rows = j.get("data", j) if isinstance(j, dict) else j
    return _series([(x.get("date"), x.get("close")) for x in rows or []], symbol)


PROVIDERS = {"tushare": p_tushare, "finnhub": p_finnhub, "alphavantage": p_alphavantage, "polygon": p_polygon,
             "twelvedata": p_twelvedata, "tiingo": p_tiingo, "fmp": p_fmp, "eastmoney": p_eastmoney, "custom": p_custom}
PERIOD_DAYS = {"1mo": 22, "3mo": 66, "6mo": 126, "1y": 252, "2y": 504, "5y": 1260}


def _yahoo(symbols: list[str], period: str) -> dict[str, pd.Series]:
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
    return out


def provider_order() -> list[str]:
    names = [x.strip().lower() for x in os.getenv("DATA_PROVIDERS", "yahoo,eastmoney").split(",") if x.strip()]
    return [n for n in names if n == "yahoo" or n in PROVIDERS] or ["yahoo", "eastmoney"]


def fetch_closes(symbols: list[str], period: str = "1y") -> dict[str, pd.Series]:
    if not symbols:
        return {}
    days = PERIOD_DAYS.get(period, 252)
    order = provider_order()
    best: dict[str, pd.Series] = {}

    def keep(s, ser):
        if ser is not None and len(ser) > len(best.get(s, [])):
            best[s] = ser.iloc[-(days + 1):]

    for name in order:
        todo = [s for s in symbols if len(best.get(s, [])) < MIN_ROWS]
        if not todo:
            break
        if name == "yahoo":
            for s, ser in _yahoo(todo, period).items():
                keep(s, ser)
            continue
        for s in todo:
            try:
                keep(s, PROVIDERS[name](s, days))
            except Exception:
                pass
    for s in symbols:
        if len(best.get(s, [])) < 2:
            keep(s, _prev_close_series(s))
    return best


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
