"""更深的行情数据（v0.16）：资金流、北向、龙虎榜、分析师一致预期、分钟线、美股期权、多源报价交叉校验。

全部免密钥。每个工具返回：
  source / url / as_of —— 主数据源（接入 Citatio 引用：角标悬停能看到来源与时间）
  sources —— 每个尝试过的数据源 {name, url, as_of, ok, error}，失败的也列出来（如实说明哪里拿不到）
  warnings —— 多源交叉校验的偏差、口径差异、规则变化（如北向资金 2024 年后不再披露每日净买入）

数据源：
  A股资金流   东方财富 push2his 资金流日线（主力/超大单/大单/中单/小单）→ 新浪财经 MoneyFlow 历史资金流（主力、净流入）
              → 东方财富当日分时资金流（只有今天）。两源重叠的日期互相比对（口径不同，阈值放宽）。
  北向资金     东方财富数据中心 RPT_MUTUAL_DEAL_HISTORY（总额）+ RPT_MUTUAL_HOLDSTOCKNORTH_STA（个股持股，现为季度披露）。
  龙虎榜       东方财富数据中心 RPT_DAILYBILLBOARD_DETAILSNEW + 买卖前五席位 RPT_BILLBOARD_DAILYDETAILSBUY/SELL。
  一致预期     A股：东方财富 RPT_WEB_RESPREDICT（评级分布、EPS 预测、目标价区间）+ 研报列表 reportapi（逐家 EPS）+ Yahoo；
              美股/港股：Yahoo Finance（目标价、评级分布、EPS 预测）。
  分钟线       Yahoo chart（三地）→ 东方财富分钟 K 线（A/港）→ 腾讯分时（A/港，仅当日）。
  期权         Yahoo options（经 yfinance，自动处理 crumb）：ATM 隐含波动率、Put/Call 比、最大痛点。
  报价校验     新浪 hq / 腾讯 qt / Yahoo chart 实时报价，和日线收盘比对。
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

CN_TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
EM_REF = {**UA, "Referer": "https://data.eastmoney.com/"}
SINA_REF = {**UA, "Referer": "https://finance.sina.com.cn/"}
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
T = 10
_cache: dict = {}
_lock = threading.Lock()

# 两个源的报价偏差超过多少（%）写 warning；资金流口径不同，阈值单独放宽
def _envf(k: str, d: float) -> float:
    """读数字型环境变量；值里带行尾注释或写错时退回默认，不让整个行情模块导入失败。"""
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", os.getenv(k) or "")
    return float(m.group(1)) if m else d


QUOTE_DIFF_PCT = _envf("QUOTE_DIFF_PCT", 0.5)
FLOW_DIFF_PCT = _envf("FLOW_DIFF_PCT", 35)
EPS_DIFF_PCT = _envf("EPS_DIFF_PCT", 10)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def _fetch(url, params=None, headers=None, ttl=300, kind="json", data=None):
    key = url + json.dumps(params or {}, sort_keys=True) + json.dumps(data or {}, sort_keys=True) + kind
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    if data is not None:
        r = requests.post(url, data=data, headers=headers or UA, timeout=T)
    else:
        r = requests.get(url, params=params, headers=headers or UA, timeout=T)
    r.raise_for_status()
    out = r.json() if kind == "json" else r.content if kind == "bytes" else r.text
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def _err(e) -> str:
    s = str(e)
    if "ProxyError" in s or "Max retries" in s:
        return "连接失败（网络或代理拦截）"
    return s[:100]


def _f(x, nd=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or math.isinf(v):
        return None
    return round(v, nd) if nd is not None else v


def _yi(x, nd=3):
    v = _f(x)
    return None if v is None else round(v / 1e8, nd)


def src(name, url="", as_of="", ok=True, error=None, note=None) -> dict:
    d = {"name": name, "url": url, "as_of": as_of, "ok": ok}
    if error:
        d["error"] = error
    if note:
        d["note"] = note
    return d


# ---------------------------------------------------------------- 代码
def parse(symbol: str) -> dict:
    """Yahoo 格式 → 各家代码。"""
    s = (symbol or "").upper().strip()
    if re.fullmatch(r"\d{6}", s):
        s += ".SS" if s.startswith(("6", "9")) else ".BJ" if s.startswith(("4", "8")) else ".SZ"
    m = re.fullmatch(r"(\d{6})\.(SS|SH|SZ|BJ)", s)
    if m:
        code, ex = m.group(1), {"SH": "SS"}.get(m.group(2), m.group(2))
        mk = {"SS": "sh", "SZ": "sz", "BJ": "bj"}[ex]
        return {"market": "A", "code": code, "yahoo": f"{code}.{ex}", "em": f"{ {'sh': 1, 'sz': 0, 'bj': 0}[mk] }.{code}",
                "sina": f"{mk}{code}", "tencent": f"{mk}{code}", "ex": mk}
    m = re.fullmatch(r"(\d{1,5})\.HK", s)
    if m:
        c5 = m.group(1).zfill(5)
        return {"market": "HK", "code": c5, "yahoo": f"{c5[-4:] if c5.startswith('0') else c5}.HK", "em": f"116.{c5}",
                "sina": f"hk{c5}", "tencent": f"hk{c5}", "ex": "hk"}
    if s.startswith("^"):
        return {"market": "IDX", "code": s, "yahoo": s, "em": None, "sina": None, "tencent": None, "ex": "idx"}
    t = s.replace(".", "-")
    return {"market": "US", "code": t, "yahoo": t, "em": None, "sina": f"gb_{t.lower().replace('-', '')}", "tencent": f"us{t.replace('-', '.')}", "ex": "us"}


# ---------------------------------------------------------------- 交叉校验
def cross_check(values: dict[str, float], threshold_pct: float, label: str, note: str = "") -> tuple[float | None, list[str]]:
    """values = {来源名: 数值}。返回 (最大偏差%, warnings)。只有一个源时 spread=None 并提示无法交叉校验。"""
    vals = {k: v for k, v in values.items() if isinstance(v, (int, float)) and v == v}
    if len(vals) < 2:
        return None, ([f"{label}只有 {next(iter(vals))} 一个数据源可用，未能交叉校验"] if len(vals) == 1 else [])
    ks = list(vals)
    worst, pair = 0.0, None
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            va, vb = vals[a], vals[b]
            base = max(abs(va), abs(vb))
            if base == 0:
                continue
            d = abs(va - vb) / base * 100
            if (va > 0) != (vb > 0) and abs(va) > 0 and abs(vb) > 0:
                d = max(d, 100.0)  # 方向相反
            if d > worst:
                worst, pair = d, (a, b)
    w = []
    if pair and worst > threshold_pct:
        a, b = pair
        w.append(f"{label}两源差异过大：{a} {_fmt(vals[a])} vs {b} {_fmt(vals[b])}，相差 {worst:.1f}%（阈值 {threshold_pct:g}%）"
                 + (f"，{note}" if note else "") + "，请以交易所/公司公告为准")
    return round(worst, 2), w


def _fmt(v):
    return f"{v:,.4f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


# ---------------------------------------------------------------- 实时报价（交叉校验用）
def _sina_quotes(p) -> dict | None:
    if not p.get("sina"):
        return None
    t = _fetch(f"https://hq.sinajs.cn/list={p['sina']}", headers=SINA_REF, ttl=30, kind="text")
    m = re.search(r'="([^"]*)"', t)
    f = m.group(1).split(",") if m and m.group(1) else []
    if p["market"] == "A" and len(f) > 31 and _f(f[3]):
        return {"price": _f(f[3]), "prev_close": _f(f[2]), "date": f[30], "time": f[31]}
    if p["market"] == "HK" and len(f) > 18 and _f(f[6]):
        return {"price": _f(f[6]), "prev_close": _f(f[3]), "date": f[17].replace("/", "-"), "time": f[18]}
    if p["market"] == "US" and len(f) > 26 and _f(f[1]):
        d = f[3][:10]
        m2 = re.match(r"([A-Za-z]{3}) (\d{1,2})", f[25] if len(f) > 25 else "")
        if m2:  # 最近一次常规收盘（美东日期）
            try:
                d = datetime.strptime(f"{f[3][:4]} {m2.group(1)} {m2.group(2)}", "%Y %b %d").strftime("%Y-%m-%d")
            except ValueError:
                pass
        return {"price": _f(f[1]), "prev_close": _f(f[26]), "date": d, "time": f[3][11:]}
    return None


def _tencent_quotes(p) -> dict | None:
    if not p.get("tencent"):
        return None
    t = _fetch(f"https://qt.gtimg.cn/q={p['tencent']}", headers=UA, ttl=30, kind="text")
    f = t.split("~")
    if len(f) < 32 or not _f(f[3]):
        return None
    ts = f[30]
    if re.fullmatch(r"\d{14}", ts):
        d = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    else:
        d = ts[:10].replace("/", "-")
    return {"price": _f(f[3]), "prev_close": _f(f[4]), "date": d, "time": ts[-8:] if not ts.isdigit() else f"{ts[8:10]}:{ts[10:12]}"}


def _yahoo_meta(p) -> dict | None:
    j = _fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{p['yahoo']}", params={"interval": "1d", "range": "5d"}, headers=UA, ttl=60)
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        return None
    m = res[0]["meta"]
    tz = timezone(timedelta(seconds=m.get("gmtoffset") or 0))
    t = datetime.fromtimestamp(m.get("regularMarketTime") or 0, tz)
    return {"price": _f(m.get("regularMarketPrice")), "prev_close": _f(m.get("chartPreviousClose") or m.get("previousClose")),
            "date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M"), "currency": m.get("currency")}


RT_SOURCES = {"新浪财经": (_sina_quotes, lambda p: f"https://finance.sina.com.cn/realstock/company/{p['sina']}/nc.shtml" if p["market"] == "A" else "https://finance.sina.com.cn/stock/"),
              "腾讯证券": (_tencent_quotes, lambda p: f"https://gu.qq.com/{p['tencent']}"),
              "Yahoo Finance": (_yahoo_meta, lambda p: f"https://finance.yahoo.com/quote/{p['yahoo']}")}


def realtime(symbol: str, names=None) -> dict:
    """各源实时报价（并行，单源最多 T 秒）。"""
    p = parse(symbol)
    names = names or list(RT_SOURCES)
    out = {}

    def one(n):
        f, u = RT_SOURCES[n]
        try:
            q = f(p)
            return n, (q, u(p), None if q else "无数据")
        except Exception as e:
            return n, (None, u(p), _err(e))

    with ThreadPoolExecutor(len(names)) as ex:
        for n, v in ex.map(one, names):
            out[n] = v
    return out


def quote_check(symbol: str, close: float | None, date: str | None, primary: str = "日线", threshold: float | None = None,
                rt: dict | None = None) -> dict:
    """日线收盘 vs 实时报价源。同一天比现价；日线日期早于实时源日期时比实时源的昨收。"""
    thr = QUOTE_DIFF_PCT if threshold is None else threshold
    rt = rt if rt is not None else realtime(symbol, [n for n in RT_SOURCES if n != primary])
    vals, used, sources = {}, {}, []
    if close is not None:
        vals[primary] = float(close)
    for n, (q, url, err) in rt.items():
        if n == primary:
            continue
        if not q or q.get("price") is None:
            sources.append(src(n, url, ok=False, error=err or "无数据"))
            continue
        qd = q.get("date") or ""
        if date and qd and qd > str(date)[:10] and q.get("prev_close"):
            v, basis = q["prev_close"], "昨收"
        elif date and qd and qd < str(date)[:10]:
            sources.append(src(n, url, qd, ok=False, error=f"日期 {qd} 早于 {date}，不比对"))
            continue
        else:
            v, basis = q["price"], "现价"
        vals[n], used[n] = v, basis
        sources.append(src(n, url, f"{qd} {q.get('time') or ''}".strip(), note=f"按{basis}比对"))
    spread, warns = cross_check(vals, thr, "报价")
    return {"values": vals, "basis": used, "spread_pct": spread, "threshold_pct": thr, "sources": sources, "warnings": warns}


# ---------------------------------------------------------------- 资金流（A股）
EM_FLOW_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"


def _em_flow_daily(p, days):
    j = _fetch("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", params={
        "lmt": days, "klt": 101, "secid": p["em"], "fields1": "f1,f2,f3,f7", "fields2": EM_FLOW_FIELDS}, headers=EM_REF, ttl=300)
    rows = []
    for k in ((j.get("data") or {}).get("klines") or []):
        f = k.split(",")
        rows.append({"date": f[0], "main_net": _yi(f[1]), "small_net": _yi(f[2]), "mid_net": _yi(f[3]), "large_net": _yi(f[4]),
                     "super_net": _yi(f[5]), "main_pct": _f(f[6], 2), "close": _f(f[11]), "chg_pct": _f(f[12], 2)})
    return rows


def _em_flow_today(p):
    """当日分时资金流的最后一分钟 = 今天累计。"""
    j = _fetch("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get", params={
        "lmt": 0, "klt": 1, "secid": p["em"], "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56"}, headers=EM_REF, ttl=60)
    ks = (j.get("data") or {}).get("klines") or []
    if not ks:
        return []
    f = ks[-1].split(",")
    return [{"date": f[0][:10], "time": f[0][11:16], "main_net": _yi(f[1]), "small_net": _yi(f[2]), "mid_net": _yi(f[3]),
             "large_net": _yi(f[4]), "super_net": _yi(f[5]), "intraday": True}]


def _sina_flow(p, days):
    j = _fetch("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs",
               params={"page": 1, "num": days, "sort": "opendate", "asc": 0, "daima": p["sina"]}, headers=SINA_REF, ttl=300)
    rows = []
    for x in j or []:
        rows.append({"date": x.get("opendate"), "main_net": _yi(x.get("r0_net")), "net": _yi(x.get("netamount")),
                     "main_pct": _f(_f(x.get("r0_ratio"), 6) * 100, 2) if _f(x.get("r0_ratio")) is not None else None,
                     "close": _f(x.get("trade")), "chg_pct": _f(_f(x.get("changeratio"), 6) * 100, 2) if _f(x.get("changeratio")) is not None else None,
                     "turnover_pct": _f(_f(x.get("turnover"), 4) / 100, 2) if _f(x.get("turnover")) is not None else None})
    return sorted(rows, key=lambda r: r["date"] or "")


def filter_asof(rows: list[dict], asof) -> list[dict]:
    """回测用：只保留 date ≤ asof 的行（严格截断）。"""
    if asof is None:
        return rows
    a = str(asof)[:10]
    return [r for r in rows if str(r.get("date") or "9999")[:10] <= a]


def flow_history(symbol: str, n: int = 400, fetchers: dict | None = None) -> tuple[list[dict], str]:
    """回测用：一次取尽量长的资金流日线（东方财富优先，新浪兜底），返回 (rows 升序, 来源名)。"""
    p = parse(symbol)
    if p["market"] != "A":
        return [], ""
    fx = fetchers or {"东方财富": lambda: _em_flow_daily(p, n), "新浪财经": lambda: _sina_flow(p, n)}
    for name, f in fx.items():
        try:
            rows = sorted(f() or [], key=lambda r: r["date"])
            if rows:
                return rows, name
        except Exception:
            continue
    return [], ""


def flow_asof(rows: list[dict], asof, days: int = 5) -> dict | None:
    """回测里的资金流快照：只用 date ≤ asof 的行（严格截断），并且只给相对量（主力净占比 %），盲测时不暴露成交额规模。"""
    past = filter_asof(rows, asof)[-days:]
    if not past:
        return None
    pct = [r.get("main_pct") for r in past if r.get("main_pct") is not None]
    return {"unit": "%", "main_net_5d": round(sum(pct), 2) if pct else None, "inflow_days_5d": sum(1 for x in pct if x > 0),
            "rows": [{"d": i - len(past) + 1, "main_pct": r.get("main_pct")} for i, r in enumerate(past)], "_last_date": past[-1]["date"]}


def fund_flow(symbol: str, days: int = 10, asof=None, fetchers: dict | None = None) -> dict:
    """个股主力 / 超大单净流入（亿元，日级）。asof 给定时只返回该日及以前（回测）。"""
    p = parse(symbol)
    days = max(1, min(int(days or 10), 60))
    if p["market"] != "A":
        return {"symbol": symbol, "error": "资金流（主力/超大单）只支持 A股；港股、美股没有可靠的免费逐日数据"}
    fx = fetchers or {"东方财富": lambda: _em_flow_daily(p, days + 5), "新浪财经": lambda: _sina_flow(p, days + 5),
                      "东方财富·当日分时": lambda: _em_flow_today(p) if asof is None else []}
    urls = {"东方财富": f"https://data.eastmoney.com/zjlx/{p['code']}.html",
            "新浪财经": f"https://vip.stock.finance.sina.com.cn/moneyflow/#!ssfx!{p['sina']}",
            "东方财富·当日分时": f"https://data.eastmoney.com/zjlx/{p['code']}.html"}
    got, sources = {}, []

    def one(n):
        try:
            return n, filter_asof(fx[n]() or [], asof), None
        except Exception as e:
            return n, [], _err(e)

    with ThreadPoolExecutor(len(fx)) as ex:
        for n, rows, err in ex.map(one, list(fx)):
            got[n] = rows
            sources.append(src(n, urls.get(n, ""), rows[-1]["date"] if rows else "", ok=bool(rows), error=err or (None if rows else "无数据")))
    em, sina, today = got.get("东方财富") or [], got.get("新浪财经") or [], got.get("东方财富·当日分时") or []
    warns = []
    if em:
        rows, primary = [dict(r) for r in em], "东方财富"
        if today and today[0]["date"] > rows[-1]["date"]:
            rows.append(today[0])
    elif sina:
        rows, primary = [dict(r) for r in sina], "新浪财经"
        warns.append("东方财富资金流日线取不到，改用新浪财经（新浪只有「主力」和总净流入，没有超大单拆分）")
        if today:  # 用东方财富当日分时补超大单（只有今天）
            for r in rows:
                if r["date"] == today[0]["date"]:
                    r["super_net"], r["large_net"] = today[0]["super_net"], today[0]["large_net"]
            if today[0]["date"] > rows[-1]["date"]:
                rows.append(today[0])
    elif today:
        rows, primary = list(today), "东方财富·当日分时"
        warns.append("历史资金流日线都取不到，只有今天的分时累计")
    else:
        return {"symbol": symbol, "error": "资金流数据源都取不到", "sources": sources}
    rows = rows[-days:]
    # 交叉校验：两源重叠日期的「主力净流入」（口径不同：东方财富主力=超大单+大单，新浪按自己的分档）
    by_s = {r["date"]: r for r in sina}
    checks = []
    if em and sina:
        for r in em[-days:]:
            s = by_s.get(r["date"])
            if s and r.get("main_net") is not None and s.get("main_net") is not None:
                sp, w = cross_check({"东方财富": r["main_net"], "新浪财经": s["main_net"]}, FLOW_DIFF_PCT, f"{r['date']} 主力净流入",
                                    "两家主力口径不同（大单分档标准不一）")
                checks.append({"date": r["date"], "东方财富": r["main_net"], "新浪财经": s["main_net"], "diff_pct": sp})
                warns += w
        if len(warns) > 3:
            warns = warns[:2] + [f"另有 {len(warns) - 2} 天两源差异超过阈值"]
    elif em or sina:
        warns.append("资金流只有一个数据源可用，未能交叉校验")
    s5 = [r["main_net"] for r in rows[-5:] if r.get("main_net") is not None]
    sup5 = [r["super_net"] for r in rows[-5:] if r.get("super_net") is not None]
    p_src = next(x for x in sources if x["name"] == primary)
    return {"symbol": p["yahoo"], "unit": "亿元", "source": primary, "url": p_src["url"], "as_of": rows[-1]["date"] + (f" {rows[-1]['time']}" if rows[-1].get("time") else ""),
            "rows": rows, "main_net_5d": round(sum(s5), 3) if s5 else None, "super_net_5d": round(sum(sup5), 3) if sup5 else None,
            "inflow_days_5d": sum(1 for x in s5 if x > 0), "crosscheck": checks[-5:], "sources": sources, "warnings": warns,
            "note": "主力净流入 = 超大单 + 大单（东方财富口径），正数为净流入"}


# ---------------------------------------------------------------- 北向资金
NORTH_RULE = ("2024-08-19 起沪深港通不再盘后披露北向资金每日净买入/买入/卖出额，只公布成交总额与成交笔数；"
              "个股北向持股也从每日改为按季度披露。拿不到的字段如实返回 null。")


def northbound(symbol: str | None = None, days: int = 5) -> dict:
    out = {"source": "东方财富数据中心（沪深港通）", "url": "https://data.eastmoney.com/hsgt/index.html", "rule_note": NORTH_RULE, "sources": [], "warnings": []}
    try:
        j = _fetch(DC, params={"reportName": "RPT_MUTUAL_DEAL_HISTORY", "columns": "ALL", "filter": '(MUTUAL_TYPE="005")',
                               "pageSize": max(1, min(days, 20)), "sortColumns": "TRADE_DATE", "sortTypes": "-1"}, headers=EM_REF, ttl=600)
        rows = []
        for x in (j.get("result") or {}).get("data") or []:
            rows.append({"date": str(x.get("TRADE_DATE"))[:10], "deal_amt_yi": _f(_f(x.get("DEAL_AMT"), 2) / 100, 2) if _f(x.get("DEAL_AMT")) is not None else None,
                         "net_buy_yi": _f(_f(x.get("NET_DEAL_AMT")) / 100, 2) if _f(x.get("NET_DEAL_AMT")) is not None else None,
                         "deal_num": x.get("DEAL_NUM"), "lead_stock": x.get("LEAD_STOCKS_NAME")})
        out["market"] = rows
        out["as_of"] = rows[0]["date"] if rows else ""
        out["sources"].append(src("东方财富 · 北向成交", "https://data.eastmoney.com/hsgt/index.html", out["as_of"], ok=bool(rows)))
        if rows and all(r["net_buy_yi"] is None for r in rows):
            out["warnings"].append("北向每日净买入额已停止披露（2024-08-19 起），这里只有成交总额")
    except Exception as e:
        out["sources"].append(src("东方财富 · 北向成交", "https://data.eastmoney.com/hsgt/index.html", ok=False, error=_err(e)))
    if symbol:
        p = parse(symbol)
        out["symbol"] = p["yahoo"]
        if p["market"] != "A":
            out["holding_error"] = "个股北向持股只适用于 A股"
        else:
            url = f"https://data.eastmoney.com/hsgtcg/StockHdStatistics/{p['code']}.html"
            try:
                j = _fetch(DC, params={"reportName": "RPT_MUTUAL_HOLDSTOCKNORTH_STA", "columns": "ALL", "filter": f'(SECURITY_CODE="{p["code"]}")',
                                       "pageSize": 6, "sortColumns": "TRADE_DATE", "sortTypes": "-1"}, headers=EM_REF, ttl=3600)
                hs = []
                for x in (j.get("result") or {}).get("data") or []:
                    hs.append({"date": str(x.get("TRADE_DATE"))[:10], "hold_shares_wan": _f(_f(x.get("HOLD_SHARES")) / 1e4, 1) if _f(x.get("HOLD_SHARES")) is not None else None,
                               "hold_value_yi": _yi(x.get("HOLD_MARKET_CAP"), 2), "pct_of_a_shares": _f(x.get("A_SHARES_RATIO"), 2), "close": _f(x.get("CLOSE_PRICE"))})
                for a, b in zip(hs, hs[1:]):
                    if a["hold_shares_wan"] is not None and b["hold_shares_wan"]:
                        a["shares_chg_wan"] = round(a["hold_shares_wan"] - b["hold_shares_wan"], 1)
                        a["shares_chg_pct"] = round((a["hold_shares_wan"] / b["hold_shares_wan"] - 1) * 100, 2)
                out["holdings"] = hs
                out["sources"].append(src("东方财富 · 北向个股持股", url, hs[0]["date"] if hs else "", ok=bool(hs), error=None if hs else "无持股记录"))
                if hs:
                    age = (now_cn().date() - datetime.strptime(hs[0]["date"], "%Y-%m-%d").date()).days
                    if age > 20:
                        out["warnings"].append(f"个股北向持股最新只到 {hs[0]['date']}（{age} 天前，现为季度披露），不能当作近期资金动向")
            except Exception as e:
                out["sources"].append(src("东方财富 · 北向个股持股", url, ok=False, error=_err(e)))
    if not any(s["ok"] for s in out["sources"]):
        out["error"] = "北向资金数据取不到"
    return out


# ---------------------------------------------------------------- 龙虎榜
def dragon_tiger(symbol: str, limit: int = 5, lookback_days: int = 120) -> dict:
    p = parse(symbol)
    if p["market"] != "A":
        return {"symbol": symbol, "error": "龙虎榜只适用于 A股"}
    url = f"https://data.eastmoney.com/stock/lhb/{p['code']}.html"
    try:
        j = _fetch(DC, params={"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL", "filter": f'(SECURITY_CODE="{p["code"]}")',
                               "pageSize": max(1, min(limit, 20)), "sortColumns": "TRADE_DATE", "sortTypes": "-1"}, headers=EM_REF, ttl=600)
    except Exception as e:
        return {"symbol": p["yahoo"], "error": f"龙虎榜取不到：{_err(e)}", "sources": [src("东方财富 · 龙虎榜", url, ok=False, error=_err(e))]}
    rows = []
    for x in (j.get("result") or {}).get("data") or []:
        rows.append({"date": str(x.get("TRADE_DATE"))[:10], "reason": x.get("EXPLANATION"), "close": _f(x.get("CLOSE_PRICE")), "chg_pct": _f(x.get("CHANGE_RATE"), 2),
                     "net_buy_yi": _yi(x.get("BILLBOARD_NET_AMT")), "buy_yi": _yi(x.get("BILLBOARD_BUY_AMT")), "sell_yi": _yi(x.get("BILLBOARD_SELL_AMT")),
                     "deal_ratio_pct": _f(x.get("DEAL_AMOUNT_RATIO"), 2), "explain": x.get("EXPLAIN")})
    out = {"symbol": p["yahoo"], "source": "东方财富 · 龙虎榜", "url": url, "sources": [src("东方财富 · 龙虎榜", url, rows[0]["date"] if rows else "")], "warnings": []}
    if not rows:
        return {**out, "as_of": now_cn().strftime("%Y-%m-%d"), "records": [], "listed_recently": False, "note": "没有上过龙虎榜的记录"}
    age = (now_cn().date() - datetime.strptime(rows[0]["date"], "%Y-%m-%d").date()).days
    out.update({"as_of": rows[0]["date"], "records": rows, "listed_recently": age <= lookback_days, "days_since_last": age})
    if age > lookback_days:
        out["note"] = f"近 {lookback_days} 天没有上龙虎榜，最近一次是 {rows[0]['date']}（{age} 天前），太久远的席位不再列出"
        out["records"] = rows[:2]
        return out
    # 最近一次上榜的买卖前五席位
    d0 = rows[0]["date"]
    for side, rep, key in (("buy_seats", "RPT_BILLBOARD_DAILYDETAILSBUY", "BUY"), ("sell_seats", "RPT_BILLBOARD_DAILYDETAILSSELL", "SELL")):
        try:
            jj = _fetch(DC, params={"reportName": rep, "columns": "ALL", "filter": f"(TRADE_DATE='{d0}')(SECURITY_CODE=\"{p['code']}\")",
                                    "pageSize": 5, "sortColumns": key, "sortTypes": "-1"}, headers=EM_REF, ttl=3600)
            out[side] = [{"seat": x.get("OPERATEDEPT_NAME"), "buy_wan": _f(_f(x.get("BUY")) / 1e4, 1) if _f(x.get("BUY")) is not None else None,
                          "sell_wan": _f(_f(x.get("SELL")) / 1e4, 1) if _f(x.get("SELL")) is not None else None,
                          "net_wan": _f(_f(x.get("NET")) / 1e4, 1) if _f(x.get("NET")) is not None else None}
                         for x in ((jj.get("result") or {}).get("data") or [])[:5]]
        except Exception as e:
            out[side] = []
            out["warnings"].append(f"{'买入' if side == 'buy_seats' else '卖出'}席位取不到：{_err(e)}")
    out["seats_date"] = d0
    return out


# ---------------------------------------------------------------- 一致预期
def _yahoo_consensus(ysym):
    import yfinance as yf
    t = yf.Ticker(ysym)
    out = {}
    try:
        pt = t.analyst_price_targets or {}
        out["target"] = {k: _f(pt.get(k), 2) for k in ("low", "mean", "median", "high", "current") if pt.get(k) is not None}
    except Exception:
        pass
    try:
        rs = t.recommendations_summary
        if rs is not None and not rs.empty:
            r0 = rs.iloc[0]
            out["ratings"] = {"强烈买入": int(r0.get("strongBuy", 0)), "买入": int(r0.get("buy", 0)), "持有": int(r0.get("hold", 0)),
                              "卖出": int(r0.get("sell", 0)), "强烈卖出": int(r0.get("strongSell", 0))}
    except Exception:
        pass
    try:
        ee = t.earnings_estimate
        if ee is not None and not ee.empty:
            lab = {"0q": "本季", "+1q": "下季", "0y": "本财年", "+1y": "下财年"}
            out["eps"] = [{"period": lab.get(i, i), "avg": _f(ee.at[i, "avg"], 3), "low": _f(ee.at[i, "low"], 3), "high": _f(ee.at[i, "high"], 3),
                           "analysts": int(ee.at[i, "numberOfAnalysts"]) if _f(ee.at[i, "numberOfAnalysts"]) is not None else None,
                           "growth_pct": _f(_f(ee.at[i, "growth"]) * 100, 1) if _f(ee.at[i, "growth"]) is not None else None,
                           "currency": ee.at[i, "currency"] if "currency" in ee.columns else None} for i in ee.index]
    except Exception:
        pass
    return out


NASDAQ_H = {**UA, "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def _nasdaq_consensus(sym):
    base = f"https://api.nasdaq.com/api/analyst/{sym}"
    tp = (_fetch(base + "/targetprice", headers=NASDAQ_H, ttl=3600).get("data") or {}).get("consensusOverview") or {}
    out = {}
    if tp.get("priceTarget"):
        out["target"] = {"low": _f(tp.get("lowPriceTarget"), 2), "mean": _f(tp.get("priceTarget"), 2), "high": _f(tp.get("highPriceTarget"), 2)}
        out["ratings"] = {"买入": int(tp.get("buy") or 0), "持有": int(tp.get("hold") or 0), "卖出": int(tp.get("sell") or 0)}
    try:
        fc = _fetch(base + "/earnings-forecast", headers=NASDAQ_H, ttl=3600).get("data") or {}
        out["eps_years"] = [{"fiscal_end": r.get("fiscalEnd"), "avg": _f(r.get("consensusEPSForecast"), 3), "low": _f(r.get("lowEPSForecast"), 3),
                             "high": _f(r.get("highEPSForecast"), 3), "analysts": r.get("noOfEstimates")}
                            for r in ((fc.get("yearlyForecast") or {}).get("rows") or [])[:3]]
    except Exception:
        pass
    return out or None


def _em_consensus(p):
    j = _fetch(DC, params={"reportName": "RPT_WEB_RESPREDICT", "columns": "ALL", "filter": f'(SECURITY_CODE="{p["code"]}")', "pageSize": 1,
                           "sortColumns": "SECURITY_CODE", "sortTypes": "-1"}, headers=EM_REF, ttl=3600)
    d = ((j.get("result") or {}).get("data") or [None])[0]
    if not d:
        return None
    out = {"ratings": {k: int(d.get(f) or 0) for k, f in (("买入", "RATING_BUY_NUM"), ("增持", "RATING_ADD_NUM"), ("中性", "RATING_NEUTRAL_NUM"),
                                                          ("减持", "RATING_REDUCE_NUM"), ("卖出", "RATING_SALE_NUM"))},
           "orgs": d.get("RATING_ORG_NUM"),
           "eps": [{"year": d.get(f"YEAR{i}"), "kind": "实际" if d.get(f"YEAR_MARK{i}") == "A" else "预测", "eps": _f(d.get(f"EPS{i}"), 3)}
                   for i in range(1, 5) if d.get(f"YEAR{i}")],
           "target": {"low": _f(d.get("DEC_AIMPRICEMIN"), 2), "high": _f(d.get("DEC_AIMPRICEMAX"), 2)}}
    return out


def _em_reports(p, n=12):
    end = now_cn()
    j = _fetch("https://reportapi.eastmoney.com/report/list", params={
        "industryCode": "*", "pageSize": n, "industry": "*", "rating": "*", "ratingChange": "*", "beginTime": (end - timedelta(days=365)).strftime("%Y-%m-%d"),
        "endTime": end.strftime("%Y-%m-%d"), "pageNo": 1, "qType": 0, "code": p["code"]}, headers=EM_REF, ttl=3600)
    out = []
    for x in j.get("data") or []:
        out.append({"date": str(x.get("publishDate"))[:10], "org": x.get("orgSName"), "rating": x.get("emRatingName") or x.get("sRatingName"),
                    "eps_this_year": _f(x.get("predictThisYearEps"), 3), "eps_next_year": _f(x.get("predictNextYearEps"), 3),
                    "target": _f(x.get("indvAimPriceT"), 2), "title": x.get("title"),
                    "url": f"https://data.eastmoney.com/report/info/{x.get('infoCode')}.html" if x.get("infoCode") else ""})
    return out


def consensus(symbol: str) -> dict:
    p = parse(symbol)
    if p["market"] == "IDX":
        return {"symbol": symbol, "error": "指数没有分析师一致预期"}
    sources, warns = [], []
    out = {"symbol": p["yahoo"], "market": p["market"]}
    yurl = f"https://finance.yahoo.com/quote/{p['yahoo']}/analysis"
    jobs = {"yahoo": lambda: _yahoo_consensus(p["yahoo"])}
    if p["market"] == "A":
        jobs["em"] = lambda: _em_consensus(p)
        jobs["rep"] = lambda: _em_reports(p)
    if p["market"] == "US":
        jobs["nasdaq"] = lambda: _nasdaq_consensus(p["yahoo"])
    res = {}

    def one(k):
        try:
            return k, jobs[k](), None
        except Exception as e:
            return k, None, _err(e)

    with ThreadPoolExecutor(len(jobs)) as ex:
        for k, v, e in ex.map(one, list(jobs)):
            res[k] = (v, e)
    today = now_cn().strftime("%Y-%m-%d")
    y, ye = res["yahoo"]
    y = y if y and (y.get("target") or y.get("ratings") or y.get("eps")) else None
    if p["market"] == "A":
        em, ee = res["em"]
        reps, re_ = res["rep"]
        emurl = f"https://data.eastmoney.com/stockdata/{p['code']}.html"
        sources.append(src("东方财富 · 盈利预测", emurl, today, ok=bool(em), error=ee or (None if em else "无数据")))
        sources.append(src("东方财富 · 研报", f"https://data.eastmoney.com/report/singlestock.jshtml?stockcode={p['code']}",
                           reps[0]["date"] if reps else "", ok=bool(reps), error=re_ or (None if reps else "近一年没有研报")))
        sources.append(src("Yahoo Finance", yurl, today, ok=bool(y), error=ye or (None if y else "无覆盖")))
        if em:
            out.update({"ratings": em["ratings"], "orgs": em["orgs"], "eps": em["eps"], "target": em["target"]})
            out["source"], out["url"] = "东方财富 · 盈利预测", emurl
        elif y:
            out.update({k: y[k] for k in ("ratings", "eps", "target") if k in y})
            out["source"], out["url"] = "Yahoo Finance", yurl
        if reps:
            out["reports"] = reps[:8]
            # 研报逐家给的今年 EPS 中位数 vs 东方财富汇总
            this = [r["eps_this_year"] for r in reps if r["eps_this_year"]]
            if this and em:
                yr = int(today[:4])
                agg = next((e["eps"] for e in em["eps"] if e["year"] == yr and e["kind"] == "预测"), None)
                med = round(statistics.median(this), 3)
                out["eps_reports_median"] = med
                if agg:
                    _, w = cross_check({"东方财富汇总": agg, "近一年研报中位数": med}, EPS_DIFF_PCT, f"{yr} 年 EPS 预测", "研报发布时点不同")
                    warns += w
            tg = [r["target"] for r in reps if r["target"]]
            if tg:
                out["target_reports"] = {"n": len(tg), "low": min(tg), "high": max(tg), "median": round(statistics.median(tg), 2)}
        if y and y.get("target") and out.get("target", {}).get("low") and out["source"] != "Yahoo Finance":
            out["target_yahoo"] = y["target"]
            ym, lo, hi = y["target"].get("mean"), out["target"]["low"], out["target"]["high"]
            if ym and lo and hi and not (lo * .9 <= ym <= hi * 1.1):
                warns.append(f"目标价两源不一致：Yahoo 均值 {ym} 不在东方财富区间 {lo}–{hi} 内（可能币种、覆盖机构或更新时间不同）")
        elif not y and em:
            warns.append("Yahoo 对这只 A股没有分析师覆盖数据，目标价无法交叉校验")
    else:
        sources.append(src("Yahoo Finance", yurl, today, ok=bool(y), error=ye or (None if y else "无覆盖或被限流")))
        nq, ne = res.get("nasdaq", (None, None))
        nurl = f"https://www.nasdaq.com/market-activity/stocks/{p['yahoo'].lower()}/analyst-research"
        if p["market"] == "US":
            sources.append(src("Nasdaq", nurl, today, ok=bool(nq), error=ne or (None if nq else "无数据")))
        if y:
            out.update({k: y[k] for k in ("ratings", "eps", "target") if k in y})
            out["source"], out["url"] = "Yahoo Finance", yurl
        elif nq:
            out.update({k: nq[k] for k in ("ratings", "target") if k in nq})
            out["source"], out["url"] = "Nasdaq", nurl
        if nq:
            if out["source"] != "Nasdaq":
                out["target_nasdaq"] = nq.get("target")
                out["ratings_nasdaq"] = nq.get("ratings")
            out["eps_nasdaq"] = nq.get("eps_years")
            ym, nm = (y or {}).get("target", {}).get("mean"), (nq.get("target") or {}).get("mean")
            if ym and nm:
                _, w = cross_check({"Yahoo": ym, "Nasdaq": nm}, 12, "目标价均值", "两家统计的机构和更新时间不同")
                warns += w
            ye0 = next((e["avg"] for e in (y or {}).get("eps") or [] if e["period"] == "本财年"), None)
            ne0 = ((nq.get("eps_years") or [{}])[0]).get("avg")
            if ye0 and ne0:
                _, w = cross_check({"Yahoo": ye0, "Nasdaq": ne0}, EPS_DIFF_PCT, "本财年 EPS 预测")
                warns += w
            if not y:
                warns.append("Yahoo 不可用（无覆盖或被限流），一致预期只有 Nasdaq 一个源，未能交叉校验")
        elif y:
            warns.append("一致预期只有 Yahoo Finance 一个源可用，未能交叉校验")
    if not out.get("source"):
        return {"symbol": p["yahoo"], "error": "取不到分析师一致预期（可能无人覆盖或数据源不可用）", "sources": sources}
    tg = out.get("target") or {}
    cur = tg.get("current")
    if cur is None:
        try:
            q = realtime(p["yahoo"], ["Yahoo Finance"]).get("Yahoo Finance", (None,))[0]
            cur = q and q.get("price")
        except Exception:
            cur = None
    if cur:
        out["price_now"] = cur
        mid = tg.get("mean") or tg.get("median") or ((tg["low"] + tg["high"]) / 2 if tg.get("low") and tg.get("high") else None)
        if mid:
            out["upside_pct"] = round((mid / cur - 1) * 100, 1)
        if p["market"] == "HK" and (out.get("eps") or [{}])[0].get("currency") not in (None, "HKD"):
            warns.append(f"港股 EPS 预测币种是 {(out['eps'][0] or {}).get('currency')}，股价是港元，算市盈率要先换汇")
    out.update({"as_of": today, "sources": sources, "warnings": warns})
    return out


# ---------------------------------------------------------------- 分钟线
def _yahoo_intraday(p, days, interval):
    rng = "1d" if days <= 1 else "5d"
    j = _fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{p['yahoo']}", params={"interval": f"{interval}m", "range": rng}, headers=UA, ttl=60)
    r = ((j.get("chart") or {}).get("result") or [None])[0]
    if not r or not r.get("timestamp"):
        return None
    tz = timezone(timedelta(seconds=r["meta"].get("gmtoffset") or 0))
    q = r["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(r["timestamp"]):
        c = q["close"][i]
        if c is None:
            continue
        bars.append({"t": datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M"), "o": _f(q["open"][i], 3), "h": _f(q["high"][i], 3),
                     "l": _f(q["low"][i], 3), "c": _f(c, 3), "v": q["volume"][i] or 0})
    return {"bars": bars, "prev_close": _f(r["meta"].get("chartPreviousClose") or r["meta"].get("previousClose")), "tz": r["meta"].get("exchangeTimezoneName")}


def _em_intraday(p, days, interval):
    if not p.get("em"):
        return None
    per_day = {1: 240, 5: 48, 15: 16, 30: 8, 60: 4}.get(interval, 48) + (88 if p["market"] == "HK" else 0) // max(interval, 1)
    j = _fetch("https://push2his.eastmoney.com/api/qt/stock/kline/get", params={
        "secid": p["em"], "fields1": "f1,f3", "fields2": "f51,f52,f53,f54,f55,f56", "klt": interval, "fqt": 0, "lmt": per_day * days + 5}, headers=EM_REF, ttl=60)
    d = j.get("data") or {}
    bars = []
    for k in d.get("klines") or []:
        f = k.split(",")
        bars.append({"t": f[0], "o": _f(f[1]), "c": _f(f[2]), "h": _f(f[3]), "l": _f(f[4]), "v": (_f(f[5]) or 0) * (100 if p["market"] == "A" else 1)})
    return {"bars": bars, "prev_close": _f(d.get("preKPrice")), "tz": "Asia/Shanghai"} if bars else None


def _tencent_intraday(p, days, interval):
    if p["market"] not in ("A", "HK"):
        return None
    j = _fetch("https://web.ifzq.gtimg.cn/appstock/app/minute/query", params={"code": p["tencent"]}, headers=UA, ttl=60)
    d = ((j.get("data") or {}).get(p["tencent"]) or {}).get("data") or {}
    date = str(d.get("date") or "")
    rows = d.get("data") or []
    if not rows or not re.fullmatch(r"\d{8}", date):
        return None
    bars, prev_v = [], 0.0
    for s in rows:
        f = s.split()
        if len(f) < 3:
            continue
        v = float(f[2]) * (100 if p["market"] == "A" else 1)
        c = _f(f[1])
        bars.append({"t": f"{date[:4]}-{date[4:6]}-{date[6:]} {f[0][:2]}:{f[0][2:]}", "o": c, "h": c, "l": c, "c": c, "v": max(0.0, v - prev_v)})
        prev_v = v
    if interval > 1:  # 1 分钟聚合成 interval 分钟
        agg = []
        for i in range(0, len(bars), interval):
            g = bars[i:i + interval]
            agg.append({"t": g[-1]["t"], "o": g[0]["o"], "h": max(b["h"] for b in g), "l": min(b["l"] for b in g), "c": g[-1]["c"], "v": sum(b["v"] for b in g)})
        bars = agg
    return {"bars": bars, "prev_close": None, "tz": "Asia/Shanghai"}


def summarize_bars(bars: list[dict], prev_close=None, max_points: int = 60) -> list[dict]:
    """按交易日汇总：开高低收、高低点时刻、VWAP、相对昨收涨跌；附抽样后的走势点。"""
    days = {}
    for b in bars:
        days.setdefault(b["t"][:10], []).append(b)
    out, pc = [], prev_close
    for d in sorted(days):
        g = days[d]
        hi = max(g, key=lambda b: b["h"] if b["h"] is not None else -1e18)
        lo = min(g, key=lambda b: b["l"] if b["l"] is not None else 1e18)
        vol = sum(b["v"] or 0 for b in g)
        vwap = sum(((b["h"] + b["l"] + b["c"]) / 3) * (b["v"] or 0) for b in g if b["h"] is not None and b["l"] is not None) / vol if vol else None
        step = max(1, math.ceil(len(g) / max_points))
        row = {"date": d, "open": g[0]["o"], "high": hi["h"], "low": lo["l"], "last": g[-1]["c"], "high_time": hi["t"][11:16], "low_time": lo["t"][11:16],
               "vwap": round(vwap, 3) if vwap else None, "volume": int(vol), "bars": len(g), "first": g[0]["t"][11:16], "last_time": g[-1]["t"][11:16],
               "points": [[b["t"][11:16], b["c"]] for b in g[::step]] + ([[g[-1]["t"][11:16], g[-1]["c"]]] if (len(g) - 1) % step else [])}
        if pc:
            row["chg_pct"] = round((g[-1]["c"] / pc - 1) * 100, 2)
            row["prev_close"] = pc
        if row["high"] and row["low"] and row["low"] > 0:
            row["range_pct"] = round((row["high"] / row["low"] - 1) * 100, 2)
        out.append(row)
        pc = g[-1]["c"]
    return out


def intraday(symbol: str, days: int = 1, interval: int = 5) -> dict:
    p = parse(symbol)
    days = max(1, min(int(days or 1), 5))
    interval = int(interval or 5)
    interval = min((1, 5, 15, 30, 60), key=lambda x: abs(x - interval))
    order = [("Yahoo Finance", _yahoo_intraday, f"https://finance.yahoo.com/quote/{p['yahoo']}/chart"),
             ("东方财富", _em_intraday, f"https://quote.eastmoney.com/{p['ex']}{p['code']}.html" if p["market"] == "A" else f"https://quote.eastmoney.com/hk/{p['code']}.html"),
             ("腾讯证券", _tencent_intraday, f"https://gu.qq.com/{p['tencent']}")]
    if p["market"] in ("US", "IDX"):
        order = order[:1]
    got, sources = {}, []

    def one(item):
        n, f, u = item
        try:
            return n, f(p, days, interval), u, None
        except Exception as e:
            return n, None, u, _err(e)

    with ThreadPoolExecutor(len(order)) as ex:
        for n, r, u, e in ex.map(one, order):
            got[n] = r
            last = r["bars"][-1]["t"] if r and r.get("bars") else ""
            sources.append(src(n, u, last, ok=bool(r and r.get("bars")), error=e or (None if r and r.get("bars") else "无数据")))
    primary = next((n for n, _, _ in order if got.get(n) and got[n].get("bars")), None)
    if not primary:
        return {"symbol": p["yahoo"], "error": "分钟线数据源都取不到", "sources": sources}
    g = got[primary]
    bars = g["bars"]
    keep_days = sorted({b["t"][:10] for b in bars})[-days:]
    bars = [b for b in bars if b["t"][:10] in keep_days]
    summ = summarize_bars(bars, g.get("prev_close"), max_points=48 if len(keep_days) == 1 else 16)
    warns = []
    # 交叉校验：各源同一天最后一根 K 线的收盘
    last_d = keep_days[-1]
    vals = {}
    for n, r in got.items():
        if r and r.get("bars"):
            same = [b for b in r["bars"] if b["t"][:10] == last_d]
            if same:
                vals[n] = same[-1]["c"]
    if len(vals) >= 2:
        _, w = cross_check(vals, max(QUOTE_DIFF_PCT, 0.8), f"{last_d} 分钟线最新价", "可能是各源最后一根 K 线的时刻不同")
        warns += w
    elif p["market"] in ("A", "HK"):
        warns.append("分钟线只有一个数据源可用，未能交叉校验")
    if p["market"] in ("US",):
        warns.append("美股分钟线只有 Yahoo 一个免费源；时间为美东时间")
    return {"symbol": p["yahoo"], "interval_min": interval, "tz": g.get("tz"), "source": primary,
            "url": next(s["url"] for s in sources if s["name"] == primary), "as_of": f"{bars[-1]['t']} {g.get('tz') or ''}".strip(),
            "days": summ, "sources": sources, "warnings": warns}


# ---------------------------------------------------------------- 期权（美股）
def max_pain(chain: list[dict]) -> float | None:
    """chain = [{strike, call_oi, put_oi}]；到期价 = 让期权买方总收益（卖方总赔付）最小的行权价。"""
    strikes = sorted({c["strike"] for c in chain if c.get("strike") is not None})
    if not strikes:
        return None
    best, bp = None, None
    for s in strikes:
        pay = 0.0
        for c in chain:
            k = c["strike"]
            pay += (c.get("call_oi") or 0) * max(0.0, s - k) + (c.get("put_oi") or 0) * max(0.0, k - s)
        if bp is None or pay < bp:
            best, bp = s, pay
    return best


def _clean_iv(x):
    v = _f(x)
    return v if v is not None and 0.01 < v < 5 else None


def chain_stats(calls: list[dict], puts: list[dict], spot: float) -> dict:
    """calls/puts: [{strike, iv, volume, oi}]。"""
    by = {}
    for side, rows in (("call", calls), ("put", puts)):
        for r in rows:
            k = _f(r.get("strike"))
            if k is None:
                continue
            d = by.setdefault(k, {"strike": k})
            d[f"{side}_oi"] = int(_f(r.get("oi")) or 0)
            d[f"{side}_vol"] = int(_f(r.get("volume")) or 0)
            d[f"{side}_iv"] = _clean_iv(r.get("iv"))
    chain = sorted(by.values(), key=lambda d: d["strike"])
    cv, pv = sum(d.get("call_vol", 0) for d in chain), sum(d.get("put_vol", 0) for d in chain)
    co, po = sum(d.get("call_oi", 0) for d in chain), sum(d.get("put_oi", 0) for d in chain)
    atm = None
    ivs = [d for d in chain if d.get("call_iv") or d.get("put_iv")]
    if ivs and spot:
        near = sorted(ivs, key=lambda d: abs(d["strike"] - spot))[:2]
        vals = [v for d in near for v in (d.get("call_iv"), d.get("put_iv")) if v]
        atm = round(statistics.mean(vals) * 100, 1) if vals else None
    return {"atm_iv_pct": atm, "pcr_volume": round(pv / cv, 2) if cv else None, "pcr_oi": round(po / co, 2) if co else None,
            "max_pain": max_pain(chain), "call_oi": co, "put_oi": po, "call_volume": cv, "put_volume": pv, "strikes": len(chain)}


def _nasdaq_chain(sym, expiry=None):
    params = {"assetclass": "stocks", "limit": 2000, "money": "all", "type": "all"}
    if expiry:
        params.update({"fromdate": expiry, "todate": expiry})
    d = _fetch(f"https://api.nasdaq.com/api/quote/{sym}/option-chain", params=params, headers=NASDAQ_H, ttl=600).get("data") or {}
    rows = ((d.get("table") or {}).get("rows")) or []
    groups, cur = {}, None
    for r in rows:
        if r.get("expirygroup"):
            try:
                cur = datetime.strptime(r["expirygroup"], "%B %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                cur = r["expirygroup"]
            continue
        if r.get("strike") and cur:
            g = groups.setdefault(cur, {"calls": [], "puts": []})
            k = _f(str(r["strike"]).replace(",", ""))
            num = lambda x: _f(str(x).replace(",", "")) if x not in (None, "--", "") else 0
            g["calls"].append({"strike": k, "iv": None, "volume": num(r.get("c_Volume")), "oi": num(r.get("c_Openinterest"))})
            g["puts"].append({"strike": k, "iv": None, "volume": num(r.get("p_Volume")), "oi": num(r.get("p_Openinterest"))})
    m = re.search(r"\$([\d,.]+)", d.get("lastTrade") or "")
    return groups, (_f(m.group(1).replace(",", "")) if m else None)


def options(symbol: str, expiries: int = 2, yahoo=None, nasdaq=None) -> dict:
    p = parse(symbol)
    url = f"https://finance.yahoo.com/quote/{p['yahoo']}/options"
    if p["market"] != "US":
        return {"symbol": symbol, "error": "期权数据目前只支持美股（Yahoo options）", "sources": []}
    nurl = f"https://www.nasdaq.com/market-activity/stocks/{p['yahoo'].lower()}/option-chain"
    today = now_cn().date()
    stamp = now_cn().strftime("%Y-%m-%d")
    sources, warns, out, spot, primary = [], [], [], None, None

    def yahoo_chains():
        import yfinance as yf
        t = yf.Ticker(p["yahoo"])
        exps = list(t.options or [])
        if not exps:
            raise RuntimeError("Yahoo 未返回期权链（可能被限流）")
        try:
            sp = _f(t.fast_info.last_price)
        except Exception:
            sp = None
        pick = exps[:max(1, min(int(expiries or 2), 4))]
        m30 = min(exps, key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d").date() - today).days - 30))
        if m30 not in pick:
            pick.append(m30)
        conv = lambda df: [{"strike": r.strike, "iv": r.impliedVolatility, "volume": r.volume, "oi": r.openInterest} for r in df.itertuples()]
        ch = {}
        for e in pick:
            oc = t.option_chain(e)
            ch[e] = {"calls": conv(oc.calls), "puts": conv(oc.puts)}
        return ch, sp, m30

    try:
        ych, spot, m30 = (yahoo or yahoo_chains)()
        sources.append(src("Yahoo Finance · 期权", url, stamp))
        primary = "Yahoo Finance · 期权"
    except Exception as e:
        ych, m30 = {}, None
        sources.append(src("Yahoo Finance · 期权", url, ok=False, error=_err(e)))
    nq_first = None
    try:
        first = sorted(ych)[0] if ych else None
        groups, nspot = (nasdaq or _nasdaq_chain)(p["yahoo"], first)
        if groups:
            nq_first = sorted(groups)[0]
            sources.append(src("Nasdaq · 期权", nurl, stamp))
            spot = spot or nspot
            if not ych:
                ych, primary = {nq_first: groups[nq_first]}, "Nasdaq · 期权"
                warns.append("Yahoo 期权链取不到，改用 Nasdaq（只有成交量和持仓量，没有隐含波动率）")
        else:
            sources.append(src("Nasdaq · 期权", nurl, ok=False, error="无数据"))
    except Exception as e:
        groups = {}
        sources.append(src("Nasdaq · 期权", nurl, ok=False, error=_err(e)))
    if not ych:
        return {"symbol": p["yahoo"], "error": "期权数据取不到（没有上市期权，或数据源都不可用）", "sources": sources}
    for e in sorted(ych):
        st = chain_stats(ych[e]["calls"], ych[e]["puts"], spot or 0)
        st["expiry"] = e
        st["days"] = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if e == m30:
            st["near_30d"] = True
        out.append(st)
    if primary.startswith("Yahoo") and groups and nq_first in groups and nq_first == out[0]["expiry"]:
        ns = chain_stats(groups[nq_first]["calls"], groups[nq_first]["puts"], spot or 0)
        out[0]["nasdaq"] = {"pcr_oi": ns["pcr_oi"], "max_pain": ns["max_pain"], "call_oi": ns["call_oi"], "put_oi": ns["put_oi"]}
        _, w = cross_check({"Yahoo": out[0]["pcr_oi"], "Nasdaq": ns["pcr_oi"]}, 15, f"{nq_first} 到期 Put/Call 持仓比", "持仓量更新时点可能不同")
        warns += w
        if out[0]["max_pain"] and ns["max_pain"] and out[0]["max_pain"] != ns["max_pain"]:
            _, w = cross_check({"Yahoo": out[0]["max_pain"], "Nasdaq": ns["max_pain"]}, 3, f"{nq_first} 到期最大痛点")
            warns += w
    elif primary.startswith("Yahoo"):
        warns.append("期权只有 Yahoo 一个源可用，未能交叉校验")
    if primary.startswith("Yahoo"):
        warns.append("隐含波动率由 Yahoo 按最近成交价反推，盘前盘后和冷门行权价会失真")
    if spot and out[0].get("max_pain"):
        out[0]["max_pain_gap_pct"] = round((out[0]["max_pain"] / spot - 1) * 100, 2)
    return {"symbol": p["yahoo"], "spot": spot, "source": primary, "url": url if primary.startswith("Yahoo") else nurl,
            "as_of": now_cn().strftime("%Y-%m-%d %H:%M") + " 北京时间", "expiries": out, "sources": sources, "warnings": warns,
            "note": "Put/Call 比 >1 表示看跌合约更多；最大痛点 = 到期时让期权买方总价值最小的价格，常被当作到期日的「磁铁」，但不是预测"}
