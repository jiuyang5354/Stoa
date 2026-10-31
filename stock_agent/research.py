"""研究工具：新闻、公告、财报与估值、联网搜索、读网页。

全部免密钥可用：
- 新闻：东方财富资讯搜索（A股/港股，中文）+ Yahoo Finance RSS（美股/港股，英文）
- 公告：东方财富公告中心（A股/港股，原文链接）+ SEC EDGAR（美股 10-K/10-Q/8-K）
- 财报：东方财富 F10 主要指标（A股）+ Yahoo Finance 估值与利润表（三地）
- 搜索：Tavily / Brave（填了密钥就用）→ DuckDuckGo → Bing RSS
- 读网页：只读公网 http(s)，拒绝内网地址，正文截到 6000 字
"""
import html
import ipaddress
import json
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
SEC_UA = {"User-Agent": os.environ.get("SEC_USER_AGENT") or "Stoa Research stoa@example.com"}  # SEC 要求声明联系人
T = 12
_cache: dict = {}


def _get(url, ttl=600, **kw):
    key = url + json.dumps(kw.get("params") or kw.get("data") or {}, sort_keys=True, ensure_ascii=False)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    kw.setdefault("headers", UA)
    kw.setdefault("timeout", T)
    method = "post" if "data" in kw else "get"
    r = getattr(requests, method)(url, **kw)
    r.raise_for_status()
    _cache[key] = (time.time(), r)
    return r


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


# ---------- 代码识别 ----------
def market(symbol: str) -> tuple[str, str]:
    """Yahoo 格式代码 → (市场, 本地代码)。A: 600519 / HK: 00700 / US: NVDA"""
    s = symbol.upper().strip()
    if s.endswith((".SS", ".SH", ".SZ", ".BJ")):
        return "A", s.split(".")[0]
    if re.fullmatch(r"\d{6}", s):
        return "A", s
    if s.endswith(".HK"):
        return "HK", s.split(".")[0].zfill(5)
    return "US", s.replace(".", "-")


def resolve(symbol: str) -> dict:
    """东方财富联想搜索，拿中文简称。"""
    mk, code = market(symbol)
    if mk == "US":
        try:
            return {"market": mk, "code": code, "name": (_cik(code) or (0, code))[1]}
        except Exception:
            return {"market": mk, "code": code, "name": code}
    try:
        r = _get("https://searchapi.eastmoney.com/api/suggest/get", ttl=86400,
                 params={"input": code, "type": 14, "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 5})
        for d in r.json()["QuotationCodeTable"]["Data"] or []:
            if d["Code"] == code and ((mk == "HK") == (d.get("Classify") == "HK")):
                return {"market": mk, "code": code, "name": d["Name"]}
    except Exception:
        pass
    return {"market": mk, "code": code, "name": code}


def secucode(code: str) -> str:
    return code + (".SH" if code.startswith(("6", "9")) else ".BJ" if code.startswith(("4", "8")) else ".SZ")


# ---------- 新闻 ----------
def _em_news(keyword, limit):
    p = {"uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web",
         "clientVersion": "curr", "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                                                  "pageIndex": 1, "pageSize": limit, "preTag": "", "postTag": ""}}}
    r = _get("https://search-api-web.eastmoney.com/search/jsonp", params={"cb": "cb", "param": json.dumps(p, ensure_ascii=False)})
    body = json.loads(r.text[r.text.index("(") + 1:r.text.rindex(")")])
    out = []
    for a in body["result"]["cmsArticleWebOld"][:limit]:
        out.append({"time": a["date"][:16], "title": _clean(a["title"]), "summary": _clean(a.get("content", ""))[:120],
                    "source": a.get("mediaName") or "东方财富", "url": f"https://finance.eastmoney.com/a/{a['code']}.html"})
    return out


def _yahoo_news(symbol, limit):
    r = _get("https://feeds.finance.yahoo.com/rss/2.0/headline", params={"s": symbol, "region": "US", "lang": "en-US"})
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        pub = it.findtext("pubDate") or ""
        try:
            pub = time.strftime("%Y-%m-%d %H:%M", time.strptime(pub[:25].strip(), "%a, %d %b %Y %H:%M:%S"))
        except ValueError:
            pass
        out.append({"time": pub, "title": _clean(it.findtext("title")), "summary": _clean(it.findtext("description"))[:160],
                    "source": "Yahoo Finance", "url": it.findtext("link")})
        if len(out) >= limit:
            break
    return out


def news(symbol: str, limit: int = 8, keyword: str = "") -> dict:
    info = resolve(symbol)
    items, errs = [], []
    srcs = []
    if info["market"] in ("A", "HK") or keyword:
        srcs.append(lambda: _em_news(keyword or info["name"], max(limit * 2, 10)))
    if info["market"] in ("US", "HK"):
        srcs.append(lambda: _yahoo_news(symbol.upper(), limit))
    for f in srcs:
        try:
            items += f()
        except Exception as e:
            errs.append(str(e)[:80])
    seen, uniq = set(), []
    nm = keyword or info["name"]
    for it in sorted(items, key=lambda x: (nm in x["title"] or info["code"] in x["title"], x["time"]), reverse=True):
        if it["title"] not in seen:
            seen.add(it["title"])
            uniq.append(it)
    return {"symbol": symbol, "name": info["name"], "news": uniq[:limit], **({"errors": errs} if errs and not uniq else {})}


# ---------- 公告 ----------
_SEC_MAP = {}


def _cik(ticker):
    if not _SEC_MAP:
        r = _get("https://www.sec.gov/files/company_tickers.json", ttl=86400, headers=SEC_UA)
        for v in r.json().values():
            _SEC_MAP[v["ticker"].upper()] = (str(v["cik_str"]).zfill(10), v["title"])
    return _SEC_MAP.get(ticker.upper())


def announcements(symbol: str, limit: int = 10) -> dict:
    info = resolve(symbol)
    mk, code = info["market"], info["code"]
    out = []
    if mk in ("A", "HK"):
        r = _get("https://np-anotice-stock.eastmoney.com/api/security/ann",
                 params={"sr": -1, "page_size": limit, "page_index": 1, "ann_type": "A" if mk == "A" else "H",
                         "client_source": "web", "stock_list": code})
        for a in (r.json().get("data") or {}).get("list") or []:
            out.append({"time": a["display_time"][:16], "title": _clean(a["title"]),
                        "type": "、".join(c["column_name"] for c in a.get("columns") or []),
                        "url": f"https://data.eastmoney.com/notices/detail/{code}/{a['art_code']}.html"})
        return {"symbol": symbol, "name": info["name"], "source": "东方财富公告中心", "announcements": out}
    hit = _cik(code)
    if not hit:
        return {"symbol": symbol, "error": "SEC 里找不到这个代码（可能不是美国上市公司）"}
    cik, title = hit
    r = _get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_UA)
    rec = r.json()["filings"]["recent"]
    want = {"10-K", "10-Q", "8-K", "20-F", "6-K", "S-1", "DEF 14A", "4", "SC 13G", "SC 13D"}
    for i, form in enumerate(rec["form"]):
        if form in want and form != "4":
            acc = rec["accessionNumber"][i].replace("-", "")
            out.append({"time": rec["filingDate"][i], "title": form if (rec["primaryDocDescription"][i] or form).upper() == form else f"{form} {rec['primaryDocDescription'][i]}",
                        "type": form, "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{rec['primaryDocument'][i]}"})
            if len(out) >= limit:
                break
    return {"symbol": symbol, "name": title, "source": "SEC EDGAR", "announcements": out}


# ---------- 财报与估值 ----------
def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    for u, d in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if abs(x) >= d:
            return f"{x / d:.2f}{u}"
    return round(x, 4)


def _f10(symbol: str) -> str:
    from .cite import f10_url
    return f10_url(symbol)


def _add_operating_revenue(rows: list[dict], raw: list[dict], code: str):
    """东方财富利润表 OPERATE_INCOME（营业收入）→ 每期加「营业收入」与按上年同期算的「营业收入同比%」（同为累计口径）。"""
    r = _get("https://datacenter.eastmoney.com/securities/api/data/v1/get",
             params={"reportName": "RPT_F10_FINANCE_GINCOME", "columns": "REPORT_DATE,TOTAL_OPERATE_INCOME,OPERATE_INCOME",
                     "filter": f'(SECUCODE="{secucode(code)}")', "pageSize": 12, "sortColumns": "REPORT_DATE", "sortTypes": "-1"}, ttl=3600)
    inc = {str(x.get("REPORT_DATE") or "")[:10]: x for x in ((r.json().get("result") or {}).get("data") or [])}
    for row, rr in zip(rows, raw):
        d = str(rr.get("REPORT_DATE") or "")[:10]
        cur = inc.get(d)
        if not cur or cur.get("OPERATE_INCOME") is None:
            continue
        row["营业收入"] = _num(cur["OPERATE_INCOME"])
        prev = inc.get(f"{int(d[:4]) - 1}{d[4:]}") if d[:4].isdigit() else None
        if prev and prev.get("OPERATE_INCOME"):
            row["营业收入同比%"] = round((float(cur["OPERATE_INCOME"]) / float(prev["OPERATE_INCOME"]) - 1) * 100, 4)


def financials(symbol: str) -> dict:
    info = resolve(symbol)
    out = {"symbol": symbol, "name": info["name"]}
    unified: dict[str, list] = {}  # 来源 → 统一口径行（finunify.FIELDS）
    from . import finunify
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        i = t.info or {}
        keys = {"currency": "货币", "marketCap": "市值", "trailingPE": "市盈率TTM", "forwardPE": "预期市盈率", "priceToBook": "市净率",
                "dividendYield": "股息率%", "profitMargins": "净利率%", "returnOnEquity": "ROE%", "revenueGrowth": "营收同比%",
                "earningsGrowth": "盈利同比%", "debtToEquity": "负债/权益%", "totalCash": "现金", "totalDebt": "总负债",
                "freeCashflow": "自由现金流", "targetMeanPrice": "分析师目标均价", "recommendationKey": "分析师评级",
                "numberOfAnalystOpinions": "覆盖分析师数", "fiftyTwoWeekHigh": "52周高", "fiftyTwoWeekLow": "52周低"}
        pct = {"profitMargins", "returnOnEquity", "revenueGrowth", "earningsGrowth"}
        out["valuation"] = {v: (i[k] if isinstance(i.get(k), str) else round(i[k] * 100, 2) if k in pct else _num(i.get(k)))
                            for k, v in keys.items() if i.get(k) is not None}
        q = t.quarterly_income_stmt
        if q is not None and not q.empty:
            rows = [r for r in ("Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Diluted EPS") if r in q.index]
            out["quarterly"] = [{"period": str(c.date()), **{r: _num(q.at[r, c]) for r in rows}} for c in list(q.columns)[:4]]
        if info["market"] == "A":  # 年报口径的第二个源，用来和东方财富交叉校验
            try:
                ya = finunify.from_yahoo_annual(t.income_stmt, i.get("financialCurrency") or i.get("currency"))
                if ya:
                    unified["Yahoo Finance"] = ya
            except Exception:
                pass
    except Exception as e:
        out["yahoo_error"] = str(e)[:80]
    if info["market"] == "A":
        try:
            cols = ("REPORT_DATE,REPORT_DATE_NAME,TOTALOPERATEREVE,TOTALOPERATEREVETZ,PARENTNETPROFIT,PARENTNETPROFITTZ,"
                    "KCFJCXSYJLR,EPSJB,BPS,ROEJQ,XSMLL,XSJLL,ZCFZL,MGJYXJJE")
            r = _get("https://datacenter.eastmoney.com/securities/api/data/v1/get",
                     params={"reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": cols, "filter": f'(SECUCODE="{secucode(info["code"])}")',
                             "pageSize": 6, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
            names = {"REPORT_DATE_NAME": "报告期", "TOTALOPERATEREVE": "营业总收入", "TOTALOPERATEREVETZ": "营业总收入同比%",
                     "PARENTNETPROFIT": "归母净利润", "PARENTNETPROFITTZ": "净利同比%", "KCFJCXSYJLR": "扣非净利润",
                     "EPSJB": "每股收益", "BPS": "每股净资产", "ROEJQ": "ROE%", "XSMLL": "毛利率%", "XSJLL": "净利率%",
                     "ZCFZL": "资产负债率%", "MGJYXJJE": "每股经营现金流"}
            raw = r.json()["result"]["data"]
            out["main_indicators"] = [{names[k]: (v if k == "REPORT_DATE_NAME" else _num(v)) for k, v in row.items() if k in names}
                                      for row in raw]
            unified["东方财富"] = finunify.from_eastmoney(raw)
            out["source"] = "东方财富 F10 + Yahoo Finance"
            out["url"] = _f10(symbol)
            try:  # 营业收入（不含利息收入等）与营业总收入分开给：盲评 Q14 把营业总收入同比 1.82% 写成了营业收入同比（实为 1.96%）
                _add_operating_revenue(out["main_indicators"], raw, info["code"])
            except Exception as e:
                out["operating_revenue_error"] = str(e)[:80]
            out["caliber"] = ("营业总收入 = 营业收入 + 利息收入/手续费等（集团有财务公司时两者不同）；营业总收入同比% ≠ 营业收入同比%，按题目字面取。"
                              "数字均为年初至今累计（三季报 = 前三季度）。")
        except Exception as e:
            out["eastmoney_error"] = str(e)[:80]
        from .sources import tushare
        if tushare.enabled():  # 只有用户自己填了 token 才查
            ts = tushare.financials(symbol)
            if ts.get("rows"):
                unified["Tushare"] = [{**row, "basis": "cumulative", "currency": "CNY"} for row in ts["rows"]]
                out["source"] = (out.get("source") or "Yahoo Finance") + " + Tushare"
            elif ts.get("error"):
                out["tushare_error"] = ts["error"]
    else:
        out["source"] = "Yahoo Finance"
    if unified:
        out["unified"] = [{"source": s, **row} for s, rows in unified.items() for row in rows[:4]]
        if len(unified) >= 2:
            cc = finunify.cross(unified)
            out["fin_crosscheck"] = cc
            if cc["warnings"]:
                out["warnings"] = (out.get("warnings") or []) + cc["warnings"]
            elif cc["compared"]:
                out["fin_crosscheck_note"] = f"{'/'.join(unified)} 共比较 {cc['compared']} 项，差异都在 {cc['threshold_pct']:g}% 以内"
        else:
            out["fin_crosscheck_note"] = f"财务数据只有 {next(iter(unified))} 一个源，未能交叉校验"
    return out


# ---------- 联网搜索 ----------
def _ddg(q, n):
    r = _get("https://html.duckduckgo.com/html/", data={"q": q}, ttl=300)
    links = re.findall(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', r.text)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    out = []
    for i, (u, t) in enumerate(links[:n]):
        if "uddg=" in u:
            u = unquote(parse_qs(urlparse(u).query).get("uddg", [u])[0])
        out.append({"title": _clean(t), "url": u, "snippet": _clean(snips[i]) if i < len(snips) else ""})
    return out


def _bing(q, n):
    r = _get("https://www.bing.com/search", params={"q": q, "format": "rss"}, ttl=300)
    return [{"title": _clean(i.findtext("title")), "url": i.findtext("link"), "snippet": _clean(i.findtext("description"))}
            for i in list(ET.fromstring(r.content).iter("item"))[:n]]


def _tavily(q, n):
    r = requests.post("https://api.tavily.com/search", json={"api_key": os.environ["TAVILY_API_KEY"], "query": q, "max_results": n}, timeout=20)
    r.raise_for_status()
    return [{"title": x["title"], "url": x["url"], "snippet": x.get("content", "")[:300]} for x in r.json()["results"]]


def _brave(q, n):
    r = requests.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": n},
                     headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"}, timeout=20)
    r.raise_for_status()
    return [{"title": x["title"], "url": x["url"], "snippet": _clean(x.get("description", ""))} for x in r.json()["web"]["results"][:n]]


def web_search(query: str, limit: int = 6) -> dict:
    order = []
    if os.environ.get("TAVILY_API_KEY"):
        order.append(("tavily", _tavily))
    if os.environ.get("BRAVE_API_KEY"):
        order.append(("brave", _brave))
    order += [("duckduckgo", _ddg), ("bing", _bing)]
    errs = []
    for name, f in order:
        try:
            res = f(query, limit)
            if res:
                return {"query": query, "engine": name, "results": res}
        except Exception as e:
            errs.append(f"{name}: {str(e)[:60]}")
    return {"query": query, "results": [], "errors": errs}


def _public(host: str) -> bool:
    try:
        for fam, *_, addr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(addr[0])
            if not ip.is_global or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def fetch_url(url: str, max_chars: int = 6000) -> dict:
    current = url
    try:
        for _ in range(6):
            u = urlparse(current)
            if u.scheme not in ("http", "https") or not u.hostname or not _public(u.hostname):
                return {"url": current, "error": "只能读取公网 http(s) 网页（含每次跳转）"}
            r = requests.get(current, headers=UA, timeout=15, allow_redirects=False, stream=True)
            try:
                if r.status_code in (301, 302, 303, 307, 308):
                    location = r.headers.get("Location")
                    if not location:
                        return {"url": current, "error": "网页跳转缺少目标地址"}
                    current = urljoin(current, location)
                    continue
                if r.status_code >= 400:
                    return {"url": current, "error": f"网页返回 {r.status_code}"}
                ctype = r.headers.get("content-type", "")
                if "pdf" in ctype or u.path.lower().endswith(".pdf"):
                    return {"url": current, "error": "这是 PDF，暂不支持直接读取，可以让用户打开链接"}
                raw = r.raw.read(2_000_000, decode_content=True)
                enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
                text = raw.decode(enc, errors="ignore")
                break
            finally:
                r.close()
        else:
            return {"url": current, "error": "网页跳转次数过多（最多 5 次）"}
    except Exception as e:
        return {"url": url, "error": str(e)[:120]}
    title = _clean((re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I) or [None, ""])[1])
    body = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav|aside|form)[^>]*>.*?</\1>", " ", text)
    arts = re.findall(r"(?is)<article[^>]*>(.*?)</article>", body)
    if arts and len(_clean(max(arts, key=lambda a: len(_clean(a))))) > 400:
        body = max(arts, key=lambda a: len(_clean(a)))
    m = None if arts and body in arts else re.search(
        r'(?is)<div[^>]+(?:id|class)="[^"]*(?:ContentBody|article-content|article_content|txtinfos|post-content|entry-content|newsContent)[^"]*"[^>]*>(.*)', body)
    if m:
        body = m.group(1)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", body)
    lines = [l for l in (_clean(x) for x in body.split("\n")) if len(l) > 1]
    para = [l for l in lines if len(l) >= 20]
    if sum(map(len, para)) > 300:  # 去掉导航、菜单这类短行
        lines = para
    out = "\n".join(lines)
    return {"url": current, "title": title, "text": out[:max_chars], "truncated": len(out) > max_chars}
