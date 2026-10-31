"""行情数据：可插拔多数据源（DATA_PROVIDERS），默认 腾讯证券 → 东方财富 → 新浪（AKShare）→ Yahoo Finance → 昨收兜底（国内网络可直连）。"""
import os
import re
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
    return EM_INDEX.get(s)


EM_INDEX = {"^HSI": "100.HSI", "^GSPC": "100.SPX", "^IXIC": "100.NDX", "^DJI": "100.DJIA", "^N225": "100.N225"}


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
# DATA_PROVIDERS 按顺序尝试，默认 tencent,eastmoney,yahoo。代码统一用 Yahoo 格式（600519.SS / 000001.SZ / 0700.HK / AAPL / ^GSPC），
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
    """Tushare Pro（可选，用户自填 TUSHARE_TOKEN 才启用）：实现在 sources/tushare.py。"""
    from .sources import tushare
    if not tushare.enabled():
        return None
    rows = tushare.daily(symbol, _start(days).strftime("%Y%m%d"))
    return _series(rows, symbol) if rows else None


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


# ---- 腾讯证券：A股 / 港股 / 美股 / 主要指数日线都有，国内直连、不用密钥，默认第一个试 ----
TX_INDEX = {"^HSI": "hkHSI", "^GSPC": "usINX", "^IXIC": "usIXIC", "^DJI": "usDJI", "^NDX": "usNDX"}
_TX_US: dict[str, str] = {}


def _tx_json(url: str, tries: int = 3):
    """腾讯接口偶尔回空包，重试几次。"""
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}, timeout=8)
            if r.text.strip().startswith("{"):
                return r.json()
        except Exception:
            pass
        time.sleep(0.4 * (i + 1))
    return None


def _tx_code(symbol: str) -> list[str]:
    s = symbol.upper()
    if s in TX_INDEX:
        return [TX_INDEX[s]]
    mkt, code = _split(s)
    code = code.replace("-", ".") if mkt == "us" else code  # Yahoo 的 BRK-B = 腾讯的 BRK.B
    if mkt in ("sh", "sz"):
        return [mkt + code]
    if mkt == "hk":
        return ["hk" + (code.zfill(5) if code.isdigit() else code)]
    if mkt == "us" and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", code):
        if code in _TX_US:
            return [_TX_US[code]]
        try:  # 腾讯美股代码要带交易所后缀（AAPL.OQ / JPM.N），先问它的搜索框
            r = requests.get("https://smartbox.gtimg.cn/s3/", params={"v": 2, "q": code, "t": "us"}, timeout=6)
            m = re.search(r"us~([a-z0-9.\-]+\.(?:oq|n|am))~", r.text)
            if m:
                return ["us" + m.group(1).upper()]
        except Exception:
            pass
        return ["us" + code + x for x in (".OQ", ".N", ".AM")]
    return []


def p_tencent(symbol, days):
    n = min(max(days + 5, 30), 640)
    for code in _tx_code(symbol):
        api = "usfqkline/get" if code.startswith("us") else "fqkline/get"
        j = _tx_json(f"https://web.ifzq.gtimg.cn/appstock/app/{api}?param={code},day,,,{n},qfq")
        node = ((j or {}).get("data") or {}).get(code)
        rows = ((node.get("qfqday") or node.get("day")) if isinstance(node, dict) else None)
        if rows:
            if code.startswith("us") and "." in code[2:]:
                _TX_US[_split(symbol)[1].replace("-", ".")] = code
            return _series([(r[0], r[2]) for r in rows if len(r) > 2], symbol)
    return None


def p_custom(symbol, days):
    """自建接口：DATA_CUSTOM_URL 里用 {symbol} {days} 占位，返回 [{"date": "...", "close": ...}] 或 {"data": [...]}。"""
    url = os.getenv("DATA_CUSTOM_URL")
    if not url:
        return None
    hdr = {"Authorization": f"Bearer {os.getenv('DATA_CUSTOM_TOKEN')}"} if os.getenv("DATA_CUSTOM_TOKEN") else {}
    j = requests.get(url.format(symbol=symbol, days=days), headers=hdr, timeout=20).json()
    rows = j.get("data", j) if isinstance(j, dict) else j
    return _series([(x.get("date"), x.get("close")) for x in rows or []], symbol)


# ---- AKShare（新浪财经日线）：腾讯/东财都挂了时的第三个国内源；没装 akshare 就跳过 ----
def p_akshare(symbol, days):
    try:
        import akshare as ak
    except Exception:
        return None
    s = symbol.upper()
    mkt, code = _split(s)
    start = _start(days).strftime("%Y%m%d")
    if s in A_INDEX:
        df = ak.stock_zh_index_daily(symbol=mkt + code)
    elif mkt in ("sh", "sz"):
        df = ak.stock_zh_a_daily(symbol=mkt + code, start_date=start, adjust="qfq")
    elif s == "^HSI" or (mkt == "hk" and not code.isdigit()):
        df = ak.stock_hk_index_daily_sina(symbol="HSI" if s == "^HSI" else code)
    elif mkt == "hk":
        df = ak.stock_hk_daily(symbol=code.zfill(5), adjust="qfq")
    elif s in AK_US_INDEX:
        df = ak.index_us_stock_sina(symbol=AK_US_INDEX[s])
    elif mkt == "us":
        df = ak.stock_us_daily(symbol=code.replace("-", "."), adjust="qfq")
    else:
        return None
    if df is None or df.empty:
        return None
    ser = _series(zip(pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"), df["close"]), symbol)
    return ser.iloc[-(days + 1):] if ser is not None else None


AK_US_INDEX = {"^GSPC": ".INX", "^IXIC": ".IXIC", "^DJI": ".DJI", "^NDX": ".NDX"}


# ---- 新浪实时报价（批量一次请求）：交易时段给右栏指数/自选覆盖最新价 ----
def live_quotes(symbols: list[str]) -> dict[str, tuple[float, str]]:
    """返回 {代码: (最新价, 日期 YYYY-MM-DD)}，只支持 A股（含指数）和港股（含恒指/恒科）。"""
    m = {}
    for s in symbols:
        u = s.upper()
        mkt, code = _split(u)
        if mkt in ("sh", "sz"):
            m[mkt + code] = s
        elif u == "^HSI":
            m["rt_hkHSI"] = s
        elif mkt == "hk":
            m[("hk" + code.zfill(5)) if code.isdigit() else ("rt_hk" + code)] = s
    if not m:
        return {}
    try:
        r = requests.get("https://hq.sinajs.cn/list=" + ",".join(m), headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"}, timeout=6)
        r.encoding = "gbk"
        text = r.text
    except Exception:
        return {}
    out = {}
    for key, body in re.findall(r'hq_str_(\w+)="([^"]*)"', text):
        f = body.split(",")
        try:
            if key[:2] in ("sh", "sz") and len(f) > 30 and float(f[3]) > 0:
                out[m[key]] = (float(f[3]), f[30])
            elif key.startswith(("hk", "rt_hk")) and len(f) > 17 and float(f[6]) > 0:
                out[m[key]] = (float(f[6]), f[17].replace("/", "-"))
        except (ValueError, KeyError):
            continue
    return out


PROVIDERS = {"tencent": p_tencent, "akshare": p_akshare, "tushare": p_tushare, "finnhub": p_finnhub, "alphavantage": p_alphavantage, "polygon": p_polygon,
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


SOURCE_NAMES = {"tencent": "腾讯证券", "akshare": "新浪财经（AKShare）", "eastmoney": "东方财富", "tushare": "Tushare", "finnhub": "Finnhub", "alphavantage": "Alpha Vantage", "polygon": "Polygon",
                "twelvedata": "Twelve Data", "tiingo": "Tiingo", "fmp": "FMP", "custom": "自定义数据源"}


def provider_order() -> list[str]:
    # 默认国内源优先（国内网络直连），Yahoo 放最后兜底；海外用户可设 DATA_PROVIDERS=yahoo,tencent,eastmoney
    raw = os.getenv("DATA_PROVIDERS", "").replace(" ", "").lower()
    if raw in ("", "yahoo,eastmoney"):  # 旧版默认值（很多人 .env 里照抄了）按新默认处理
        raw = DEFAULT_ORDER
    names = [x for x in raw.split(",") if x]
    return [n for n in names if n == "yahoo" or n in PROVIDERS] or DEFAULT_ORDER.split(",")


DEFAULT_ORDER = "tencent,eastmoney,akshare,yahoo"


def fetch_closes(symbols: list[str], period: str = "1y") -> dict[str, pd.Series]:
    if not symbols:
        return {}
    days = PERIOD_DAYS.get(period, 252)
    order = provider_order()
    best: dict[str, pd.Series] = {}

    def keep(s, ser, src=""):
        if ser is not None and len(ser) > len(best.get(s, [])):
            best[s] = ser.iloc[-(days + 1):].copy()
            best[s].attrs["source"] = src  # 引用用：这只标的的行情来自哪个数据源

    for name in order:
        todo = [s for s in symbols if len(best.get(s, [])) < MIN_ROWS]
        if not todo:
            break
        if name == "yahoo":
            for s, ser in _yahoo(todo, period).items():
                keep(s, ser, "Yahoo Finance")
            continue
        def get(s, fn=PROVIDERS[name]):
            try:
                return fn(s, days)
            except Exception:
                return None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(min(6, len(todo))) as ex:  # 多只标的并行取，指数栏一次十几只也很快
            for s, ser in zip(todo, ex.map(get, todo)):
                keep(s, ser, SOURCE_NAMES.get(name, name))
    for s in symbols:
        if len(best.get(s, [])) < 2:
            keep(s, _prev_close_series(s), "Yahoo 报价（仅昨收）")
    return best


def fetch_news(symbol: str, limit: int = 3) -> list[dict]:
    """个股新闻标题（东方财富 + Yahoo），给报告用。"""
    from . import research
    try:
        return [{"title": n["title"], "url": n["url"]} for n in research.news(symbol, limit)["news"]]
    except Exception:
        return []
