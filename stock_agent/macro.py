"""宏观/统计时间序列：FRED（圣路易斯联储，免密钥 CSV）。

macro_series(query=…) 搜索序列；macro_series(series_id=…, start, end, dates) 取数据。
FRED 也收录大量海外数据（OECD 主要经济指标、IMF、BIS），中国 CPI/PPI/外储等常可在此找到。
"""
from __future__ import annotations

import html as htmlmod
import re
from datetime import datetime

import requests

UA = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}  # FRED 对浏览器 UA 有反爬拦截，curl UA 秒回
_cache: dict = {}


def _get(url: str, params: dict | None = None) -> str:
    k = (url, tuple(sorted((params or {}).items())))
    if k in _cache:
        return _cache[k]
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    _cache[k] = r.text
    return r.text


def search(query: str, limit: int = 12) -> list[dict]:
    h = _get("https://fred.stlouisfed.org/searchresults/", {"st": query})
    out = []
    for m in re.finditer(r'<li class="search-list-item">(.*?)</li>\s*(?=<li class="search-list-item">|</ul>)', h, re.S):
        blk = m.group(1)
        t = re.search(r'href="/series/([A-Za-z0-9_]+)"[^>]*aria-label="([^"]*)"', blk)
        if not t:
            continue
        meta = " ".join(" ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", x)).split()) for x in re.findall(r'class="search-result-meta"[^>]*>(.*?)</span>', blk, re.S))
        out.append({"series_id": t.group(1), "title": htmlmod.unescape(t.group(2)), "meta": meta[:200]})
        if len(out) >= limit:
            break
    return out


def series(series_id: str, start: str = "", end: str = "", dates=None, vintage: str = "") -> dict:
    sid = series_id.strip().upper()
    params = {"id": sid}
    if vintage:  # ALFRED：取某个发布日（vintage）当时公布的数据版本，题目写「data from 2025-07-18」时用
        params["vintage_date"] = str(vintage)[:10]
    if start:
        params["cosd"] = start
    if end:
        params["coed"] = end
    txt = _get("https://alfred.stlouisfed.org/graph/alfredgraph.csv" if vintage else "https://fred.stlouisfed.org/graph/fredgraph.csv", params)
    lines = [l for l in txt.strip().splitlines() if l]
    if not lines or "," not in lines[0]:
        return {"series_id": sid, "error": "FRED 没有这个序列；先用 query 搜索"}
    rows = []
    for l in lines[1:]:
        d, _, v = l.partition(",")
        try:
            rows.append({"date": d, "value": float(v)})
        except ValueError:
            continue
    info = {}
    try:
        page = _get(f"https://fred.stlouisfed.org/series/{sid}")
        t = re.search(r'<title>(.*?)</title>', page, re.S)
        info["title"] = htmlmod.unescape(t.group(1)).split("|")[0].strip() if t else ""
        u = re.search(r'Units:\s*</span>\s*<span[^>]*>(.*?)</span>', page, re.S)
        if u:
            info["units"] = " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", u.group(1))).split())
        f = re.search(r'Frequency:\s*</span>\s*<span[^>]*>(.*?)</span>', page, re.S)
        if f:
            info["frequency"] = " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", f.group(1))).split())
    except Exception:
        pass
    out = {"series_id": sid, **info, "source": "ALFRED (vintage " + str(vintage)[:10] + "), Federal Reserve Bank of St. Louis" if vintage else "FRED, Federal Reserve Bank of St. Louis",
           "url": f"https://alfred.stlouisfed.org/series?seid={sid}" if vintage else f"https://fred.stlouisfed.org/series/{sid}", **({"vintage": str(vintage)[:10]} if vintage else {}),
           "count": len(rows), "as_of": rows[-1]["date"] if rows else None,
           "note": "date 是观测期起始日（月度 2024-03-01 即 2024 年 3 月；周度为周末前某日，看标题）。单位见 units，亿/万换算自己做。"}
    if dates:
        if isinstance(dates, str):
            dates = re.split(r"[,\s]+", dates)
        res = []
        for d in dates:
            d = str(d)[:10]
            exact = next((r for r in rows if r["date"] == d), None)
            prev = next((r for r in reversed(rows) if r["date"] <= d), None)
            nxt = next((r for r in rows if r["date"] > d), None)
            res.append({"date": d, "exact": exact, "latest_on_or_before": prev, "next": nxt})
        out["dates"] = res
    else:
        out["observations"] = rows if len(rows) <= 240 else rows[-240:]
        if len(rows) > 240:
            out["truncated"] = f"共 {len(rows)} 条，只列最后 240 条；缩小 start/end"
    return out


def run_tool(name: str, a: dict):
    if a.get("series_id"):
        return series(a["series_id"], a.get("start") or "", a.get("end") or "", a.get("dates"), a.get("vintage") or "")
    if a.get("query"):
        return {"query": a["query"], "results": search(a["query"]),
                "hint": "挑合适的 series_id 再调一次取数据；注意季调(SA)/非季调(NSA)、单位和频率"}
    return {"error": "给 query 搜索，或 series_id 取数据"}


TOOL_NAMES = {"macro_series"}
S = {"type": "string"}
TOOL_SPECS = [
    {"name": "macro_series", "description": "宏观经济/金融统计时间序列（FRED，收录美联储 H.8 银行信贷、利率、美国 CPI/GDP/就业，以及 OECD/IMF 收录的各国 CPI、PPI、GDP、汇率、外储、商业信心指数等数十万条）。"
     "先用 query（英文，如 'total bank credit', 'China producer price index', 'Germany business climate'）搜 series_id，再给 series_id + start/end（YYYY-MM-DD）或 dates 取值。"
     "问历史宏观数字时优先用它，不要只靠网页搜索摘要。题目指定数据版本（data from 某日）时给 vintage=YYYY-MM-DD（ALFRED）。",
     "parameters": {"type": "object", "properties": {"query": S, "series_id": S, "start": S, "end": S, "dates": {"type": "array", "items": S}, "vintage": S}}},
]
