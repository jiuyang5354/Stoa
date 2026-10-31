"""SEC EDGAR 全文工具 + 历史收盘价 + 电话会文字稿。

全部免费公开接口，SEC 要求声明联系人 User-Agent（环境变量 SEC_USER_AGENT 可覆盖，格式「名字 邮箱」）；限速 10 次/秒，这里串行加 0.12 秒间隔。
- cik_lookup：代码 → CIK（company_tickers.json），找不到时按公司名查 EDGAR 实体索引（退市公司如 Seagen 用名字查）。
- list_filings：submissions（含更早的分页文件）按表格类型 / 日期 / 8-K item 过滤。
- filing_documents：一份申报里的全部文件（主文件 + EX-99.1 新闻稿等附件）。
- read_document：读 sec.gov 上的任一文件；表格按「 | 」保留行列；section=cover/business/risk/mdna/market_risk/financial/controls 定位章节；query 用 BM25 选段；start 分页。
- xbrl_facts：companyfacts 精确科目（us-gaap / dei / ifrs-full），按财年、期间、表格过滤；search 按关键词找科目名。
- xbrl_frames：同一科目同一期间的全市场横截面。
- full_text_search：efts.sec.gov 全文检索（2001 年以后）。
- earnings_release：8-K item 2.02 的 EX-99 新闻稿（业绩、指引、Adjusted EBITDA 调节表）。
- price_history：Yahoo 日线，给定日期的收盘价（非交易日给出前后最近交易日），或区间收盘序列。
- transcripts：Motley Fool 公开的电话会文字稿（fool.com 免费页面）；取不到时用 earnings_release 的新闻稿代替。
"""
from __future__ import annotations

import html as htmlmod
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

T = 30
SEC_UA = {"User-Agent": os.environ.get("SEC_USER_AGENT") or "Stoa Research stoa@example.com", "Accept-Encoding": "gzip, deflate"}
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
_cache: dict = {}
_lock = threading.Lock()
_last = [0.0]


def _fetch(url: str, kind: str = "json", ttl: int = 3600, params: dict | None = None):
    """GET + 内存缓存。sec.gov 串行限速并带声明 UA。测试里 monkeypatch 这个函数。"""
    key = url + json.dumps(params or {}, sort_keys=True)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    sec = "sec.gov" in url
    if sec:
        with _lock:
            wait = .12 - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
    r = requests.get(url, headers=SEC_UA if sec else WEB_UA, params=params, timeout=T)
    if r.status_code == 403 and sec:
        raise RuntimeError("SEC 拒绝访问（403），通常是 User-Agent 不合规或访问过快；可在设置里填 SEC_USER_AGENT=名字 邮箱")
    r.raise_for_status()
    val = r.json() if kind == "json" else r.text
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = (time.time(), val)
    return val


# ====================================================================== 公司
def _ticker_map() -> dict:
    d = _fetch("https://www.sec.gov/files/company_tickers.json", ttl=86400)
    return {v["ticker"].upper(): (str(v["cik_str"]).zfill(10), v["title"]) for v in d.values()}


def cik_lookup(query: str) -> dict:
    """代码、CIK 或公司名 → {cik, name, ticker}；名字模糊时给 matches 让模型挑。"""
    q = str(query or "").strip()
    if not q:
        return {"error": "缺少代码或公司名"}
    if re.fullmatch(r"\d{1,10}", q):
        return {"cik": q.zfill(10), "name": None, "ticker": None}
    t = re.sub(r"^(NASDAQ|NYSE|NYSEAMERICAN|AMEX|OTC)\s*:\s*", "", q, flags=re.I).strip().upper().replace(".", "-")
    hit = _ticker_map().get(t)
    if hit:
        return {"cik": hit[0], "name": hit[1], "ticker": t}
    try:
        d = _fetch("https://efts.sec.gov/LATEST/search-index", params={"keysTyped": q}, ttl=86400)
        ms = [{"cik": str(h["_id"]).zfill(10), "name": h["_source"].get("entity"), "tickers": h["_source"].get("tickers")}
              for h in (d.get("hits") or {}).get("hits") or []]
    except Exception:
        ms = []
    if ms:
        return {"cik": ms[0]["cik"], "name": ms[0]["name"], "ticker": None, "matches": ms[:5],
                "note": "代码不在现行代码表里（可能已退市/改名），按公司名匹配；确认 matches 里是不是你要的公司"}
    return {"error": f"SEC 找不到「{q}」。退市或改名的公司请改用公司全名（如 Seagen），或直接给 CIK"}


def _cik(symbol: str) -> tuple[str, str]:
    r = cik_lookup(symbol)
    if r.get("error"):
        raise LookupError(r["error"])
    return r["cik"], r.get("name") or ""


# ====================================================================== 申报列表
def _rows(rec: dict) -> list[dict]:
    out = []
    n = len(rec.get("form") or [])
    for i in range(n):
        g = lambda k: (rec.get(k) or [""] * n)[i] if i < len(rec.get(k) or []) else ""
        out.append({"form": g("form"), "filing_date": g("filingDate"), "report_date": g("reportDate"), "accession": g("accessionNumber"),
                    "items": g("items"), "primary_doc": g("primaryDocument"), "description": g("primaryDocDescription")})
    return out


def _arch(cik: str, acc: str, doc: str = "") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"


def list_filings(symbol: str, forms=None, start: str | None = None, end: str | None = None, items: str | None = None, limit: int = 20) -> dict:
    cik, name = _cik(symbol)
    sub = _fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", ttl=1800)
    name = sub.get("name") or name
    rows = _rows(sub["filings"]["recent"])
    oldest = min((r["filing_date"] for r in rows if r["filing_date"]), default="")
    if start and oldest and start < oldest:
        for f in sub["filings"].get("files") or []:
            if f.get("filingTo", "9999") >= start:
                try:
                    rows += _rows(_fetch(f"https://data.sec.gov/submissions/{f['name']}", ttl=86400))
                except Exception:
                    pass
    if isinstance(forms, str):
        forms = [x.strip() for x in forms.split(",") if x.strip()]
    want = {f.upper() for f in forms or []}
    out = []
    for r in rows:
        if want and r["form"].upper() not in want:
            continue
        if start and r["filing_date"] < start or end and r["filing_date"] > end:
            continue
        if items and not any(it.strip() in (r["items"] or "") for it in str(items).split(",")):
            continue
        out.append({**{k: r[k] for k in ("form", "filing_date", "report_date", "accession", "items", "description") if r[k]},
                    "url": _arch(cik, r["accession"], r["primary_doc"]), "index_url": _arch(cik, r["accession"], f"{r['accession']}-index.htm")})
    out.sort(key=lambda r: r["filing_date"], reverse=True)
    return {"cik": cik, "company": name, "tickers": sub.get("tickers"), "fiscal_year_end": sub.get("fiscalYearEnd"),
            "source": "SEC EDGAR submissions", "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
            "count": len(out), "filings": out[:max(1, min(int(limit or 20), 100))]}


def _index_url(url_or_acc: str, cik: str | None = None) -> str:
    s = str(url_or_acc).strip()
    m = re.search(r"/data/(\d+)/(\d{18})", s)
    if m:
        acc = m.group(2)
        acc_d = f"{acc[:10]}-{acc[10:12]}-{acc[12:]}"
        return f"https://www.sec.gov/Archives/edgar/data/{int(m.group(1))}/{acc}/{acc_d}-index.htm"
    m = re.fullmatch(r"(\d{10})-?(\d{2})-?(\d{6})", s)
    if m and cik:
        acc_d = "-".join(m.groups())
        return _arch(cik, acc_d, f"{acc_d}-index.htm")
    raise ValueError("需要 sec.gov Archives 链接，或 accession 号 + symbol")


def filing_documents(url_or_accession: str, symbol: str | None = None) -> dict:
    cik = _cik(symbol)[0] if symbol and not re.search(r"/data/\d+/", str(url_or_accession)) else None
    idx = _index_url(url_or_accession, cik)
    raw = _fetch(idx, kind="text", ttl=86400)
    base = idx.rsplit("/", 1)[0] + "/"
    docs = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", raw):
        tds = re.findall(r"(?is)<td[^>]*>(.*?)</td>", tr)
        if len(tds) < 4:
            continue
        href = re.search(r'href="([^"]+)"', tds[2])
        if not href:
            continue
        h = href.group(1)
        if h.startswith("/ix?doc="):
            h = h[len("/ix?doc="):]
        url = "https://www.sec.gov" + h if h.startswith("/") else base + h
        docs.append({"seq": _txt(tds[0]), "description": _txt(tds[1]), "document": _txt(tds[2]).split(" ")[0], "type": _txt(tds[3]),
                     "size": _txt(tds[4]) if len(tds) > 4 else "", "url": url})
    form = re.search(r'(?is)<div class="companyName">.*?</div>', raw)
    title = re.search(r"(?is)<div id=\"formName\">(.*?)</div>", raw)
    fdate = re.search(r"(?is)Filing Date</div>\s*<div class=\"info\">([^<]+)", raw)
    return {"index_url": idx, "company": _txt(form.group(0)) if form else "", "form": _txt(title.group(1)) if title else "",
            "filing_date": fdate.group(1).strip() if fdate else "", "source": "SEC EDGAR", "url": idx, "documents": docs}


# ====================================================================== 文件正文
def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ")).strip()


def html_to_text(raw: str) -> str:
    """SEC HTML → 文本；表格每行一行，单元格用 | 分开（去掉空格和单独的 $），括号负数保留。"""
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", s)
    s = re.sub(r"(?is)<div[^>]*display:\s*none[^>]*>.*?</div>", " ", s)

    def table(m):
        rows = []
        for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", m.group(0)):
            cells = [_txt(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)]
            merged = []
            for c in cells:
                if not c or c in ("$", "%", ")"):
                    if c in ("%", ")") and merged:
                        merged[-1] += c
                    continue
                merged.append(c)
            if merged:
                rows.append(" | ".join(merged))
        return "\n" + "\n".join(rows) + "\n"

    s = re.sub(r"(?is)<table[^>]*>.*?</table>", table, s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmlmod.unescape(s).replace("\xa0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in s.split("\n")]
    return "\n".join(l for l in lines if l)


SECTIONS_10K = {"business": "1", "risk": "1a", "properties": "2", "legal": "3", "mdna": "7", "market_risk": "7a", "financial": "8", "controls": "9a",
                "executive": "10", "compensation": "11"}
SECTIONS_10Q = {"financial": "1", "mdna": "2", "market_risk": "3", "controls": "4", "risk": "1a", "legal": "1"}
ITEM_RE = re.compile(r"(?im)^\s*item\s*(\d{1,2}[a-c]?)\s*[\.:\-–—]?\s*(?=\S)")


def find_section(text: str, item: str) -> tuple[int, int] | None:
    """在全文里找「Item X」章节：同名标题可能出现在目录里，取到下一个不同 Item 标题之间最长的那段。"""
    heads = [(m.start(), m.group(1).lower()) for m in ITEM_RE.finditer(text)]
    best = None
    for i, (pos, it) in enumerate(heads):
        if it != item.lower():
            continue
        end = next((p for p, x in heads[i + 1:] if x != it), len(text))
        if not best or end - pos > best[1] - best[0]:
            best = (pos, end)
    return best


SHARES_RE = re.compile(r"(?i)((?:as of|at)\s+[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4}[^.;]{0,200}?([\d,]{7,})\s+shares[^.;]{0,160}|([\d,]{7,})\s+shares\s+of\s+(?:the\s+registrant['’]s\s+)?(?:class\s+[a-c]\s+)?common\s+stock[^.;]{0,200})")


def _chunks(text: str, size: int = 1400) -> list[tuple[int, str]]:
    out, cur, at, pos = [], "", 0, 0
    for line in text.split("\n"):
        if len(cur) + len(line) > size and cur:
            out.append((at, cur))
            cur, at = "", pos
        cur += line + "\n"
        pos += len(line) + 1
    if cur.strip():
        out.append((at, cur))
    return out


def read_document(url: str, section: str = "", query: str = "", start: int = 0, max_chars: int = 7000, k: int = 4) -> dict:
    if "sec.gov" not in str(url):
        return {"error": "read_document 只读 sec.gov 上的文件；别的网页用 fetch_url"}
    url = re.sub(r"https://www\.sec\.gov/ix\?doc=", "https://www.sec.gov", url)
    raw = _fetch(url, kind="text", ttl=86400)
    text = html_to_text(raw) if re.search(r"(?i)<(html|div|table|p)\b", raw[:5000]) else raw
    head = text[:4000]
    form = "10-Q" if re.search(r"(?i)form\s+10-q", head) else "10-K" if re.search(r"(?i)form\s+10-k", head) else ""
    out = {"url": url, "source": "SEC EDGAR", "form": form or None, "total_chars": len(text)}
    sec = (section or "").lower().strip()
    lo, hi = 0, len(text)
    if sec == "cover":
        cov = text[:9000]
        shares = [m.group(0).strip() for m in SHARES_RE.finditer(cov)][:4]
        return {**out, "section": "cover", "shares_outstanding_statements": shares, "text": cov[:max_chars],
                "note": "封面股数是封面所写日期的流通股（不是加权平均股数）；也可用 xbrl_facts concepts=dei:EntityCommonStockSharesOutstanding 核对"}
    if sec:
        item = sec if re.fullmatch(r"\d{1,2}[a-c]?", sec) else (SECTIONS_10Q if form == "10-Q" else SECTIONS_10K).get(sec)
        if not item:
            return {**out, "error": f"未知 section {section}；可用 cover/business/risk/mdna/market_risk/financial/controls 或 Item 号如 7、1a"}
        span = find_section(text, item)
        if not span:
            out["warning"] = f"没找到 Item {item} 标题，改为全文检索"
        else:
            lo, hi = span
            out["section"] = f"Item {item.upper()}"
    body = text[lo:hi]
    if query:
        from .filings import BM25, tokens
        ch = _chunks(body)
        bm = BM25([tokens(c) for _, c in ch])
        sc = bm.score(tokens(query))
        top = sorted(range(len(ch)), key=lambda i: -sc[i])[:max(1, min(int(k or 4), 8))]
        per = max(800, max_chars // max(1, len(top)))
        out["passages"] = [{"offset": lo + ch[i][0], "score": round(sc[i], 2), "text": ch[i][1][:per]} for i in sorted(top) if sc[i] > 0]
        if not out["passages"]:
            out["passages"] = [{"offset": lo, "text": body[:max_chars]}]
            out["warning"] = "关键词没有命中，返回开头"
        return out
    s = max(0, int(start or 0))
    piece = body[s:s + max_chars]
    out.update({"start": lo + s, "text": piece})
    if s + max_chars < len(body):
        out["next_start"] = s + max_chars
        out["note"] = "没读完：用同样参数加 start=next_start 继续，或用 query 检索"
    return out


# ====================================================================== XBRL
def _facts(cik: str) -> dict:
    return _fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", ttl=86400)


def _split(c: str) -> tuple[str | None, str]:
    return tuple(c.split(":", 1)) if ":" in c else (None, c)


def xbrl_facts(symbol: str, concepts=None, search: str = "", fy: int | None = None, fp: str | None = None, form: str | None = None,
               annual_only: bool = False, limit: int = 12, end: str | None = None) -> dict:
    cik, name = _cik(symbol)
    d = _facts(cik)
    facts = d.get("facts") or {}
    base = {"cik": cik, "company": d.get("entityName") or name, "source": "SEC XBRL companyfacts",
            "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"}
    if search and not concepts:
        words = [w for w in re.split(r"\W+", search.lower()) if w]
        hits = []
        for tax, cs in facts.items():
            for tag, v in cs.items():
                hay = (tag + " " + str(v.get("label") or "")).lower()
                if all(w in hay for w in words):
                    last = max((x for u in (v.get("units") or {}).values() for x in u), key=lambda x: (x.get("end", ""), x.get("filed", "")), default={})
                    hits.append({"concept": f"{tax}:{tag}", "label": v.get("label"), "last_end": last.get("end"), "last_val": last.get("val"), "last_form": last.get("form")})
        hits.sort(key=lambda h: h.get("last_end") or "", reverse=True)
        return {**base, "search": search, "matches": hits[:30], "note": "选好 concept 后用 concepts=[...] 取各期数值"}
    if isinstance(concepts, str):
        concepts = [c.strip() for c in concepts.split(",") if c.strip()]
    if not concepts:
        return {**base, "error": "给 concepts（如 us-gaap:Revenues、dei:EntityCommonStockSharesOutstanding）或 search 关键词"}
    out = {}
    for c in concepts[:8]:
        tax, tag = _split(c)
        node = None
        for t in ([tax] if tax else ["us-gaap", "dei", "ifrs-full", "srt"]):
            if t in facts and tag in facts[t]:
                node, tax = facts[t][tag], t
                break
        if not node:
            out[c] = {"error": "这家公司没有报告这个科目；用 search 找相近科目名"}
            continue
        rows = []
        for unit, xs in (node.get("units") or {}).items():
            for x in xs:
                if fy and x.get("fy") != int(fy):
                    continue
                if fp and str(x.get("fp") or "").upper() != fp.upper():
                    continue
                if form and not str(x.get("form") or "").upper().startswith(form.upper()):
                    continue
                if end and not str(x.get("end") or "").startswith(str(end)):
                    continue
                dur = None
                if x.get("start"):
                    dur = (datetime.fromisoformat(x["end"]) - datetime.fromisoformat(x["start"])).days
                    if annual_only and not 340 <= dur <= 380:
                        continue
                rows.append({"start": x.get("start"), "end": x.get("end"), "days": dur, "val": x.get("val"), "unit": unit, "fy": x.get("fy"),
                             "fp": x.get("fp"), "form": x.get("form"), "filed": x.get("filed"), "frame": x.get("frame"), "accn": x.get("accn")})
        # 同一期间同一数值只留最早披露的那条（后续文件会重复报告上年数）；数值被重述时两条都留
        seen, uniq = set(), []
        for r in sorted(rows, key=lambda r: r["filed"] or ""):
            key = (r["start"], r["end"], r["val"], r["unit"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append({k: v for k, v in r.items() if v is not None})
        uniq.sort(key=lambda r: (r.get("end") or "", r.get("filed") or ""), reverse=True)
        item = {"label": node.get("label"), "values": uniq[:max(1, min(int(limit or 12), 40))]}
        latest = max((x.get("end") or "" for xs in (node.get("units") or {}).values() for x in xs), default="")
        if latest and latest < (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d"):
            item["warning"] = f"这个科目最后一次报告到 {latest}，公司之后改用了别的科目；用 search 找现行科目（如收入常换成 RevenueFromContractWithCustomerExcludingAssessedTax）"
        out[f"{tax}:{tag}"] = item
    return {**base, "facts": out, "note": "val 是原始单位（USD 是美元，不是千/百万）；days≈365 为全年，≈90 为单季，没有 start 的是时点数（资产负债表/股数）。fy/fp 是「申报文件」的财年（10-K 里的上年对比数 fy 也是当年），按 end 判断数值属于哪个期间"}


def xbrl_frames(concept: str, period: str, unit: str = "USD", limit: int = 20) -> dict:
    tax, tag = _split(concept)
    tax = tax or "us-gaap"
    url = f"https://data.sec.gov/api/xbrl/frames/{tax}/{tag}/{unit}/{period}.json"
    d = _fetch(url, ttl=86400)
    data = sorted(d.get("data") or [], key=lambda x: -abs(x.get("val") or 0))
    return {"concept": f"{tax}:{tag}", "period": period, "unit": unit, "count": len(data), "source": "SEC XBRL frames", "url": url,
            "data": [{"company": x.get("entityName"), "cik": str(x.get("cik")).zfill(10), "val": x.get("val"), "end": x.get("end"), "accn": x.get("accn")}
                     for x in data[:max(1, min(int(limit or 20), 100))]],
            "note": "period 例：CY2024（全年）、CY2024Q4（单季）、CY2024Q4I（时点）"}


# ====================================================================== 全文检索
def full_text_search(query: str, forms=None, start: str | None = None, end: str | None = None, symbol: str | None = None, limit: int = 10) -> dict:
    params = {"q": query}
    if forms:
        params["forms"] = forms if isinstance(forms, str) else ",".join(forms)
    if start or end:
        params.update({"dateRange": "custom", "startdt": start or "2001-01-01", "enddt": end or datetime.now().strftime("%Y-%m-%d")})
    if symbol:
        params["ciks"] = _cik(symbol)[0]
    d = _fetch("https://efts.sec.gov/LATEST/search-index", params=params, ttl=3600)
    hits = []
    for h in (d.get("hits") or {}).get("hits") or []:
        s = h["_source"]
        adsh, _, fname = h["_id"].partition(":")
        cik = (s.get("ciks") or ["0"])[0]
        hits.append({"company": "; ".join(s.get("display_names") or []), "form": s.get("form"), "file_type": s.get("file_type"),
                     "filing_date": s.get("file_date"), "period": s.get("period_ending"), "items": s.get("items"),
                     "description": s.get("file_description"), "url": _arch(cik, adsh, fname)})
    return {"query": query, "total": ((d.get("hits") or {}).get("total") or {}).get("value"), "source": "SEC EDGAR 全文检索",
            "url": "https://efts.sec.gov/LATEST/search-index", "hits": hits[:max(1, min(int(limit or 10), 50))],
            "note": "q 里用双引号做精确短语；url 可直接给 read_document"}


# ====================================================================== 业绩新闻稿
def earnings_release(symbol: str, date: str | None = None, start: str | None = None, end: str | None = None, query: str = "",
                     max_chars: int = 7000, offset: int = 0) -> dict:
    if date and not (start or end):
        d0 = datetime.fromisoformat(date[:10])
        start, end = (d0 - timedelta(days=10)).strftime("%Y-%m-%d"), (d0 + timedelta(days=10)).strftime("%Y-%m-%d")
    lst = list_filings(symbol, forms=["8-K", "8-K/A", "6-K"], start=start or "2001-01-01", end=end, limit=100)
    cands = [f for f in lst["filings"] if "2.02" in (f.get("items") or "") or f["form"] == "6-K"]
    if not cands:
        return {"company": lst["company"], "error": "这段时间没有 item 2.02（业绩）8-K；放宽 start/end，或用 list_filings 看全部 8-K"}
    if date:
        cands.sort(key=lambda f: abs((datetime.fromisoformat(f["filing_date"]) - datetime.fromisoformat(date[:10])).days))
    pick = cands[0]
    docs = filing_documents(pick["url"])["documents"]
    ex = [x for x in docs if x["type"].upper().startswith("EX-99")] or [x for x in docs if x["document"].lower().endswith((".htm", ".html", ".txt"))]
    if not ex:
        return {"company": lst["company"], "filing": pick, "error": "这份 8-K 没有 EX-99 附件", "documents": docs}
    main = ex[0]
    doc = read_document(main["url"], query=query, start=offset, max_chars=max_chars)
    return {"company": lst["company"], "symbol": symbol, "filing_date": pick["filing_date"], "report_date": pick.get("report_date"),
            "form": pick["form"], "items": pick.get("items"), "exhibit": main["type"], "exhibit_description": main["description"],
            "source": "SEC EDGAR 8-K EX-99", "url": main["url"], "as_of": pick["filing_date"],
            "other_exhibits": [{"type": x["type"], "description": x["description"], "url": x["url"]} for x in ex[1:4]],
            "other_releases": [{"filing_date": f["filing_date"], "url": f["url"]} for f in cands[1:8]],
            **{k: v for k, v in doc.items() if k in ("text", "passages", "next_start", "total_chars", "note", "warning")}}


# ====================================================================== 历史收盘价
_ALIAS = {"DXY": "DX-Y.NYB", "USDX": "DX-Y.NYB", "SPX": "^GSPC", "S&P500": "^GSPC", "SP500": "^GSPC", "NDX": "^NDX", "DJI": "^DJI", "DJIA": "^DJI",
          "HSI": "^HSI", "恒生指数": "^HSI", "VIX": "^VIX", "N225": "^N225", "日经225": "^N225", "沪深300": "000300.SS", "上证指数": "000001.SS",
          "深证成指": "399001.SZ", "创业板指": "399006.SZ", "GOLD": "GC=F", "黄金": "GC=F", "WTI": "CL=F", "BRENT": "BZ=F", "BTC": "BTC-USD",
          "DAX": "^GDAXI", "FTSE": "^FTSE", "CAC40": "^FCHI", "KOSPI": "^KS11", "TNX": "^TNX", "US10Y": "^TNX"}


def _yahoo_symbol(symbol: str) -> str:
    s = re.sub(r"^(NASDAQ|NYSE|AMEX|NYSEARCA)\s*:\s*", "", symbol.strip(), flags=re.I)
    if s.upper() in _ALIAS or s in _ALIAS:
        return _ALIAS.get(s.upper(), _ALIAS.get(s))
    if s.startswith("^") or "=" in s:
        return s.upper()
    if re.fullmatch(r"\d{6}", s):  # A股裸代码
        return s + (".SS" if s[0] in "569" else ".SZ")
    if re.fullmatch(r"\d{1,5}\.HK", s, re.I):
        return s.upper().split(".")[0].zfill(4) + ".HK"
    if re.fullmatch(r"[A-Za-z]{1,5}\.[A-Za-z]", s):  # BRK.B 这类美股类别股
        return s.upper().replace(".", "-")
    return s.upper()


def _yahoo_daily(symbol: str, d0: str, d1: str) -> tuple[list[dict], dict]:
    from zoneinfo import ZoneInfo
    sym = _yahoo_symbol(symbol)
    p1 = int(datetime.fromisoformat(d0).replace(tzinfo=timezone.utc).timestamp())
    p2 = int((datetime.fromisoformat(d1) + timedelta(days=1)).replace(tzinfo=timezone.utc).timestamp())
    j = _fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"period1": p1, "period2": p2, "interval": "1d",
                                                                                     "events": "div,splits", "includeAdjustedClose": "true"}, ttl=3600)
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise LookupError(f"Yahoo 没有 {sym} 的日线：{((j.get('chart') or {}).get('error') or {}).get('description', '')}")
    r = res[0]
    meta = r.get("meta") or {}
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
    q = (r.get("indicators") or {}).get("quote", [{}])[0]
    adj = ((r.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for i, ts in enumerate(r.get("timestamp") or []):
        c = (q.get("close") or [None])[i]
        if c is None:
            continue
        g = lambda k: (q.get(k) or [None] * (i + 1))[i]
        rows.append({"date": datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d"), "open": _r4(g("open")), "high": _r4(g("high")), "low": _r4(g("low")),
                     "close": round(c, 4), "adj_close": round(adj[i], 4) if i < len(adj) and adj[i] is not None else None, "volume": g("volume")})
    splits = sorted(((datetime.fromtimestamp(v["date"], tz).strftime("%Y-%m-%d"), v["numerator"] / v["denominator"])
                     for v in ((r.get("events") or {}).get("splits") or {}).values() if v.get("denominator")), key=lambda x: x[0])
    return rows, {"symbol": sym, "name": meta.get("longName") or meta.get("shortName"), "currency": meta.get("currency"),
                  "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"), "splits": [{"date": d, "ratio": k} for d, k in splits],
                  "first_trade_date": datetime.fromtimestamp(meta["firstTradeDate"], timezone.utc).strftime("%Y-%m-%d") if meta.get("firstTradeDate") else None}


def _r4(x):
    return round(x, 4) if isinstance(x, (int, float)) else None


def _unadjust(rows: list[dict], splits: list[dict]) -> None:
    """Yahoo 的 OHLC 已按之后的拆股调整；乘回去得到当时实际成交价（raw_*）。"""
    if not splits:
        return
    for r in rows:
        f = 1.0
        for sp in splits:
            if sp["date"] > r["date"]:
                f *= sp["ratio"]
        if f != 1.0:
            for k in ("open", "high", "low", "close"):
                if r.get(k) is not None:
                    r["raw_" + k] = round(r[k] * f, 4)


def _agg(rows: list[dict], period: str) -> list[dict]:
    key = (lambda d: datetime.fromisoformat(d).strftime("%G-W%V")) if period == "week" else (lambda d: d[:7]) if period == "month" else (lambda d: d[:4])
    out, prev = [], None
    for r in rows:
        k = key(r["date"])
        if out and out[-1]["period"] == k:
            o = out[-1]; o["end"] = r["date"]; o["close"] = r["close"]
            o["high"] = max(x for x in (o["high"], r.get("high") or r["close"]) if x is not None)
            o["low"] = min(x for x in (o["low"], r.get("low") or r["close"]) if x is not None)
        else:
            if out:
                prev = out[-1]["close"]
            out.append({"period": k, "start": r["date"], "end": r["date"], "open": r.get("open"), "high": r.get("high") or r["close"],
                        "low": r.get("low") or r["close"], "close": r["close"], "prev_close": prev})
    for o in out:
        o["change_pct"] = round((o["close"] / o["prev_close"] - 1) * 100, 4) if o.get("prev_close") else None
    return out


def price_history(symbol: str, dates=None, start: str | None = None, end: str | None = None, period: str = "") -> dict:
    """日线 OHLC。dates 查具体日子；start/end 给区间统计（最高/最低价及日期、最大单日/单周涨跌、最大回撤）；period=week/month/year 聚合。"""
    if isinstance(dates, str):
        dates = [x.strip() for x in re.split(r"[,\s]+", dates) if x.strip()]
    dates = [str(x)[:10] for x in dates or []]
    if not dates and not start:
        return {"error": "给 dates（YYYY-MM-DD 列表）或 start/end"}
    lo = min(dates + ([start] if start else []))
    hi = max(dates + ([end] if end else []) + ([start] if start and not end else []))
    lo_f = (datetime.fromisoformat(lo) - timedelta(days=10)).strftime("%Y-%m-%d")
    hi_f = (datetime.fromisoformat(hi) + timedelta(days=10)).strftime("%Y-%m-%d")
    rows, meta = _yahoo_daily(symbol, lo_f, hi_f)
    try:  # 之后所有拆股（窗口外的也要），用于还原当时实际成交价
        p1 = int(datetime.fromisoformat(lo_f).replace(tzinfo=timezone.utc).timestamp())
        j = _fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{meta['symbol']}", params={"period1": p1, "period2": int(time.time()), "interval": "3mo", "events": "splits"}, ttl=86400)
        ev = ((((j.get("chart") or {}).get("result") or [{}])[0].get("events") or {}).get("splits") or {}).values()
        meta["splits"] = sorted(({"date": datetime.fromtimestamp(v["date"], timezone.utc).strftime("%Y-%m-%d"), "ratio": v["numerator"] / v["denominator"]}
                                 for v in ev if v.get("denominator")), key=lambda x: x["date"])
    except Exception:
        pass
    _unadjust(rows, meta.get("splits"))
    out = {**meta, "source": "Yahoo Finance 日线", "url": f"https://finance.yahoo.com/quote/{meta['symbol']}/history",
           "note": "open/high/low/close 已按拆股调整、未按分红调整；有 raw_* 时那是当时实际成交价（不复权）。题目问「当时的股价/历史最高价」且有拆股/送转时用 raw_*；"
                   "问涨跌幅用 close。A股送转不在 Yahoo 拆股记录里时，绝对价位可能与交易所不复权价不同，要说明。"}
    if dates:
        by = {r["date"]: r for r in rows}
        res = []
        for d in dates:
            if d in by:
                i = rows.index(by[d])
                res.append({**by[d], "trading_day": True, "prev_close": rows[i - 1]["close"] if i else None,
                            "change_pct": round((by[d]["close"] / rows[i - 1]["close"] - 1) * 100, 4) if i else None})
            else:
                prev = next((r for r in reversed(rows) if r["date"] < d), None)
                nxt = next((r for r in rows if r["date"] > d), None)
                res.append({"date": d, "trading_day": False, "previous_trading_day": prev, "next_trading_day": nxt,
                            "note": "非交易日：「截至某日」的价格一般用前一交易日收盘；题目另有约定时按题目"})
        out["dates"] = res
    if start:
        span = [r for r in rows if start <= r["date"] <= (end or hi)]
        if span:
            hi_r = max(span, key=lambda r: r.get("high") or r["close"]); lo_r = min(span, key=lambda r: r.get("low") or r["close"])
            hc = max(span, key=lambda r: r["close"]); lc = min(span, key=lambda r: r["close"])
            pre = next((r for r in reversed(rows) if r["date"] < span[0]["date"]), None)
            daily = [(span[i]["date"], (span[i]["close"] / (span[i - 1] if i else pre or span[0])["close"] - 1) * 100) for i in range(len(span))]
            peak, mdd, mdd_at = span[0]["close"], 0.0, None
            for r in span:
                peak = max(peak, r["close"]); dd = (r["close"] / peak - 1) * 100
                if dd < mdd:
                    mdd, mdd_at = dd, r["date"]
            wk = _agg(span, "week")
            if pre and wk:
                wk[0]["prev_close"] = pre["close"]; wk[0]["change_pct"] = round((wk[0]["close"] / pre["close"] - 1) * 100, 4)
            wk_ok = [w for w in wk if w.get("change_pct") is not None]
            out["range"] = {"first": span[0], "last": span[-1], "trading_days": len(span),
                            "change_pct": round((span[-1]["close"] / span[0]["close"] - 1) * 100, 4),
                            "change_pct_from_prev_close": round((span[-1]["close"] / pre["close"] - 1) * 100, 4) if pre else None,
                            "highest_high": {"date": hi_r["date"], "high": hi_r.get("high"), "raw_high": hi_r.get("raw_high")},
                            "lowest_low": {"date": lo_r["date"], "low": lo_r.get("low"), "raw_low": lo_r.get("raw_low")},
                            "highest_close": {"date": hc["date"], "close": hc["close"]}, "lowest_close": {"date": lc["date"], "close": lc["close"]},
                            "max_daily_gain": max(daily, key=lambda x: x[1]) if daily else None, "max_daily_drop": min(daily, key=lambda x: x[1]) if daily else None,
                            "max_weekly_gain": max(wk_ok, key=lambda w: w["change_pct"]) if wk_ok else None,
                            "max_weekly_drop": min(wk_ok, key=lambda w: w["change_pct"]) if wk_ok else None,
                            "max_drawdown_pct": round(mdd, 4), "max_drawdown_trough": mdd_at,
                            "note": "周涨跌＝本周最后一个交易日收盘 / 上周最后一个交易日收盘 − 1；周以 ISO 周（周一到周五）划分"}
            if period in ("week", "month", "year"):
                agg = _agg(span, period)
                if pre and agg:
                    agg[0]["prev_close"] = pre["close"]; agg[0]["change_pct"] = round((agg[0]["close"] / pre["close"] - 1) * 100, 4)
                out["series_" + period] = agg[-400:]
            else:
                out["series"] = span if len(span) <= 120 else None
                if len(span) > 120:
                    out["series_note"] = f"区间 {len(span)} 个交易日，日线不全部列出；区间统计已在 range 里算好，需要逐周/逐月用 period=week/month，具体某天用 dates"
    out["as_of"] = rows[-1]["date"] if rows else None
    return {k: v for k, v in out.items() if v is not None}


# ====================================================================== 电话会文字稿
def transcripts(symbol: str, quarter: str = "", query: str = "", max_chars: int = 7000) -> dict:
    """Motley Fool 公开文字稿：列出该股可用的季度；给 quarter（如 Q3 2025）就读全文/按 query 选段。"""
    t = re.sub(r"^(NASDAQ|NYSE|AMEX)\s*:\s*", "", symbol.strip(), flags=re.I).lower()
    links = []
    for ex in ("nasdaq", "nyse", "nysemkt"):
        try:
            raw = _fetch(f"https://www.fool.com/quote/{ex}/{t}/", kind="text", ttl=21600)
        except Exception:
            continue
        links = sorted(set(re.findall(r"(?:earnings/call-)?transcripts/(\d{4}/\d{2}/\d{2}/[a-z0-9\-]*-" + re.escape(t) + r"-q\d-\d{4}-earnings[a-z\-]*)/?", raw)))
        if links:
            break
    if not links:
        return {"symbol": symbol, "error": "没找到这只股票的公开电话会文字稿；可改用 earnings_release 读 8-K 新闻稿（管理层指引一般也在里面）"}
    items = []
    for l in links:
        m = re.search(r"-q(\d)-(\d{4})-", l)
        items.append({"quarter": f"Q{m.group(1)} {m.group(2)}" if m else "", "date": l[:10].replace("/", "-"),
                      "url": f"https://www.fool.com/earnings/call-transcripts/{l}/"})
    items.sort(key=lambda x: x["date"], reverse=True)
    base = {"symbol": symbol, "source": "The Motley Fool 电话会文字稿", "available": items[:16],
            "note": "季度是公司自己的财年季度；文字稿为第三方整理，数字以 SEC 文件为准"}
    if not quarter:
        return base
    qm = re.search(r"q\s*(\d)\D*(\d{4})|(\d{4})\D*q\s*(\d)", quarter.lower())
    want = f"Q{qm.group(1)} {qm.group(2)}" if qm and qm.group(1) else f"Q{qm.group(4)} {qm.group(3)}" if qm else quarter
    hit = next((x for x in items if x["quarter"] == want), None)
    if not hit:
        return {**base, "error": f"没有 {want} 的文字稿"}
    raw = _fetch(hit["url"], kind="text", ttl=86400)
    body = re.search(r"(?is)<div[^>]+class=\"[^\"]*article-body[^\"]*\"[^>]*>(.*?)</div>\s*<(?:div|section)[^>]+class=\"[^\"]*(?:article-disclosure|related|footer)", raw)
    text = html_to_text(body.group(1) if body else raw)
    out = {**base, "quarter": want, "date": hit["date"], "url": hit["url"], "as_of": hit["date"], "total_chars": len(text)}
    out.pop("available")
    if query:
        from .filings import BM25, tokens
        ch = _chunks(text)
        sc = BM25([tokens(c) for _, c in ch]).score(tokens(query))
        top = sorted(range(len(ch)), key=lambda i: -sc[i])[:5]
        out["passages"] = [{"offset": ch[i][0], "text": ch[i][1][:max_chars // 5 + 400]} for i in sorted(top) if sc[i] > 0]
    else:
        out["text"] = text[:max_chars]
    return out


# ====================================================================== 工具入口
TOOL_NAMES = {"sec_filings", "sec_document", "sec_xbrl", "sec_search", "earnings_release", "price_history", "earnings_transcript"}


def run_tool(name: str, a: dict):
    sym = str(a.get("symbol") or a.get("company") or "").strip()
    if name == "sec_filings":
        if a.get("url") or a.get("accession"):
            return filing_documents(a.get("url") or a.get("accession"), sym or None)
        return list_filings(sym, a.get("forms"), a.get("start"), a.get("end"), a.get("items"), int(a.get("limit") or 20))
    if name == "sec_document":
        return read_document(a["url"], a.get("section") or "", a.get("query") or "", int(a.get("start") or 0), int(a.get("max_chars") or 7000))
    if name == "sec_xbrl":
        if a.get("frame_period"):
            return xbrl_frames(a.get("concepts") if isinstance(a.get("concepts"), str) else (a.get("concepts") or [""])[0], a["frame_period"], a.get("unit") or "USD")
        return xbrl_facts(sym, a.get("concepts"), a.get("search") or "", a.get("fy"), a.get("fp"), a.get("form"), bool(a.get("annual_only")), int(a.get("limit") or 12), a.get("end"))
    if name == "sec_search":
        return full_text_search(a["query"], a.get("forms"), a.get("start"), a.get("end"), sym or None, int(a.get("limit") or 10))
    if name == "earnings_release":
        return earnings_release(sym, a.get("date"), a.get("start"), a.get("end"), a.get("query") or "", int(a.get("max_chars") or 7000), int(a.get("start_offset") or 0))
    if name == "price_history":
        return price_history(sym, a.get("dates"), a.get("start"), a.get("end"), a.get("period") or "")
    if name == "earnings_transcript":
        return transcripts(sym, a.get("quarter") or "", a.get("query") or "")
    return {"error": f"未知工具 {name}"}


S = {"type": "string"}
TOOL_SPECS = [
    {"name": "sec_filings", "description": "SEC EDGAR 申报清单（美股）：按表格类型（10-K,10-Q,8-K,DEF 14A,S-4,SC TO-T…）、申报日期 start/end（YYYY-MM-DD）、8-K item（如 2.02 业绩、5.02 高管变动、1.01 重大协议）筛选，返回申报日、报告期、主文件链接。"
     "给 url 或 accession 则列出这份申报里的全部文件（含 EX-99.1 新闻稿、EX-2.1 合并协议等附件）。symbol 可填代码、CIK 或公司名（退市公司用公司名，如 Seagen）。",
     "parameters": {"type": "object", "properties": {"symbol": S, "forms": {"type": "array", "items": S}, "start": S, "end": S, "items": S,
                                                     "url": S, "accession": S, "limit": {"type": "integer"}}}},
    {"name": "sec_document", "description": "读 sec.gov 上任一文件原文（10-K/10-Q/8-K 主文件或附件）：表格逐行保留（单元格用 | 分隔，括号为负数）。section：cover 封面（含流通股数）/ business / risk / mdna / market_risk / financial / controls 或 Item 号；"
     "query 关键词检索返回最相关段落；不给则从 start 处顺序读，未读完会给 next_start。",
     "parameters": {"type": "object", "properties": {"url": S, "section": S, "query": S, "start": {"type": "integer"}, "max_chars": {"type": "integer"}}, "required": ["url"]}},
    {"name": "sec_xbrl", "description": "SEC XBRL 精确科目数值（公司申报原值，原始单位美元）：concepts 如 us-gaap:Revenues、us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax、us-gaap:InterestExpense、"
     "us-gaap:DepreciationDepletionAndAmortization、us-gaap:OperatingLeaseCost、us-gaap:PaymentsToAcquirePropertyPlantAndEquipment、us-gaap:WeightedAverageNumberOfSharesOutstandingBasic、dei:EntityCommonStockSharesOutstanding；"
     "可按 end（期末日期前缀，如 2024 或 2024-12）/annual_only/form 过滤；不知道科目名先用 search 关键词找。给 frame_period（如 CY2024）则返回该科目全市场横截面。",
     "parameters": {"type": "object", "properties": {"symbol": S, "concepts": {"type": "array", "items": S}, "search": S, "fy": {"type": "integer"}, "fp": S, "form": S,
                                                     "annual_only": {"type": "boolean"}, "end": S, "limit": {"type": "integer"}, "frame_period": S, "unit": S}}},
    {"name": "sec_search", "description": "SEC EDGAR 全文检索（2001 年以后所有申报及附件）：query 用双引号做精确短语，可限 forms、日期 start/end、symbol。找并购公告、某个词首次出现、某笔交易的 8-K/委托书时用。",
     "parameters": {"type": "object", "properties": {"query": S, "forms": {"type": "array", "items": S}, "start": S, "end": S, "symbol": S, "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "earnings_release", "description": "美股业绩新闻稿原文：8-K item 2.02 附的 EX-99.1（季度/全年业绩、管理层指引 guidance、Non-GAAP 与 Adjusted EBITDA 调节表、门店数等 KPI）。date 填大致发布日期（取 ±10 天内最近一份），"
     "或给 start/end 区间；query 检索段落（如 guidance、Adjusted EBITDA reconciliation）；返回 other_releases 可逐季再查。",
     "parameters": {"type": "object", "properties": {"symbol": S, "date": S, "start": S, "end": S, "query": S, "start_offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, "required": ["symbol"]}},
    {"name": "price_history", "description": "历史日线（开高低收、成交量，含不复权 raw_* 价）：dates 给一组日期返回当天 OHLC 和当日涨跌，非交易日给出前后最近交易日；"
     "start/end 返回区间统计（区间涨跌、最高价/最低价及日期、最大单日/单周涨跌及所在周、最大回撤），period=week/month/year 返回逐周/月/年收盘与涨跌。"
     "用于某日开盘/收盘/最高价、区间涨跌、历史最高价、最大单周跌幅、公告后 N 天股价变化。股票、指数、外汇、商品、加密都行：美股 AAPL、A股 600519、港股 0700.HK、"
     "指数 ^GSPC ^HSI ^N225 000300.SS、美元指数 DX-Y.NYB、汇率 USDCNY=X、期货 GC=F CL=F、比特币 BTC-USD。",
     "parameters": {"type": "object", "properties": {"symbol": S, "dates": {"type": "array", "items": S}, "start": S, "end": S,
                                                     "period": {"type": "string", "enum": ["", "week", "month", "year"]}}, "required": ["symbol"]}},
    {"name": "earnings_transcript", "description": "美股电话会文字稿（The Motley Fool 公开页面）：不给 quarter 列出可用季度；quarter 如「Q3 2025」（公司财年季度）读全文，query 检索段落（如 guidance、capex、new stores）。没有文字稿时改用 earnings_release。",
     "parameters": {"type": "object", "properties": {"symbol": S, "quarter": S, "query": S}, "required": ["symbol"]}},
]
