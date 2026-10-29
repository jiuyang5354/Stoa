"""年报 / 定期报告解析（v0.16）：下载 → 抽文本（带页码）→ 按章节切块 → BM25 选段 → 两年对比。

来源（全部免密钥）：
  A股  巨潮资讯 cninfo（hisAnnouncement/query → static.cninfo.com.cn PDF），失败时退回东方财富公告（pdf.dfcfw.com）
  港股 港交所披露易 HKEXnews（titleSearchServlet，年报 t2code=40100、中期报告 40200）
  美股 SEC EDGAR（submissions → 10-K / 10-Q / 20-F 主文档 HTML，按分页符切成「页」）

PDF 抽文本用 pypdf（纯 Python，已进主依赖）；没有就试 pdfminer.six；都没有就返回原文链接并提示安装。
缓存：数据目录/data/filings/<市场>_<代码>/<年>_<类型>.(pdf|html) + .json（逐页文本与元数据），下次直接读。

工具：
  read_filing(symbol, year, section, query)        → 相关段落（每段带页码，引用链接 = PDF#page=N）
  compare_filings(symbol, year_a, year_b, section) → 两年同一章节的新增 / 删除 / 改写句子 + 关键句
章节：mdna 管理层讨论与分析 · risk 风险因素 · financial 主要财务数据 · segment 分部/分产品收入 · all 全文
"""
from __future__ import annotations

import difflib
import html as htmlmod
import io
import json
import math
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

CN_TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
SEC_UA = {"User-Agent": os.environ.get("SEC_USER_AGENT") or "Stoa Research stoa@example.com"}
MAX_BYTES = 60 * 1024 * 1024
MAX_PAGES = 800
T = 30
_locks: dict[str, threading.Lock] = {}
_glock = threading.Lock()

SECTIONS = {"mdna": "管理层讨论与分析", "risk": "风险因素", "financial": "主要财务数据", "segment": "分部 / 分产品收入", "all": "全文"}
SECTION_ALIAS = {"管理层讨论与分析": "mdna", "md&a": "mdna", "mda": "mdna", "经营情况": "mdna", "风险": "risk", "风险因素": "risk", "risk factors": "risk",
                 "财务": "financial", "主要财务数据": "financial", "财务数据": "financial", "分部": "segment", "分部收入": "segment", "分产品": "segment",
                 "segments": "segment", "全文": "all", "": "all"}
KINDS = {"annual": "年报", "semi": "半年报/中期报告", "q1": "一季报", "q3": "三季报", "quarter": "季报（美股 10-Q）"}


def filings_dir() -> Path:
    from .memory import home
    d = home() / "data" / "filings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock(key: str) -> threading.Lock:
    with _glock:
        return _locks.setdefault(key, threading.Lock())


def norm_section(s) -> str:
    s = str(s or "").strip().lower()
    if s in SECTIONS:
        return s
    for k, v in SECTION_ALIAS.items():
        if k and k in s:
            return v
    return "all"


# ====================================================================== 文本抽取
def pdf_backend() -> str | None:
    try:
        import pypdf  # noqa: F401
        return "pypdf"
    except ImportError:
        pass
    try:
        import pdfminer  # noqa: F401
        return "pdfminer"
    except ImportError:
        return None


def garbled(text: str) -> bool:
    """字体没有 ToUnicode 映射时，中文会抽成一堆别的 Unicode 区块的怪字。"""
    cjk = other = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            cjk += 1
        elif o > 0x24F and not (0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF or 0x2000 <= o <= 0x206F or 0x2190 <= o <= 0x25FF):
            other += 1
    return other > 30 and other / (cjk + other) > .3


def garbled_ratio(pages: list[str]) -> float:
    return sum(garbled(p or "") for p in pages) / max(1, len(pages))


def _cjk_spaces(t: str) -> str:
    return re.sub(r"(?<=[\u4e00-\u9fff，。、；：（）])[ \u3000]+(?=[\u4e00-\u9fff，。、；：（）])", "", t)


def _pdfminer_pages(data: bytes, idx: list[int]) -> dict[int, str]:
    """一次解析、只抽指定页（pdfminer 每次调用都会重读整份文件，逐页调用会慢几十倍）。"""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    want, out = set(idx), {}
    for pno, layout in zip(sorted(want), extract_pages(io.BytesIO(data), page_numbers=sorted(want))):
        out[pno] = _cjk_spaces("".join(el.get_text() for el in layout if isinstance(el, LTTextContainer)))
    return out


def pdf_pages(data: bytes, max_pages: int = MAX_PAGES) -> list[str]:
    """PDF → 逐页文本。pypdf 抽出乱码的页用 pdfminer（装了的话）重抽。没有解析库时抛 ImportError。"""
    be = pdf_backend()
    if be == "pypdf":
        import logging
        import pypdf
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        rd = pypdf.PdfReader(io.BytesIO(data))
        out = []
        for i, p in enumerate(rd.pages):
            if i >= max_pages:
                break
            try:
                out.append(p.extract_text() or "")
            except Exception:
                out.append("")
        bad = [i for i, t in enumerate(out) if garbled(t)]
        if bad and len(bad) >= len(out) * .1:
            try:
                fixed = _pdfminer_pages(data, bad)
                for i, t in fixed.items():
                    if t.strip() and not garbled(t):
                        out[i] = t
            except ImportError:
                pass
        return out
    if be == "pdfminer":
        from pdfminer.high_level import extract_text
        txt = extract_text(io.BytesIO(data), maxpages=max_pages)
        return [_cjk_spaces(x) for x in txt.split("\f")]
    raise ImportError("需要 PDF 解析库：pip install pypdf（或 pdfminer.six）")


PAGE_BREAK = re.compile(r"<(?:hr|div|p)[^>]*page-break-(?:before|after)\s*:\s*always[^>]*>", re.I)


def html_text(fragment: str) -> str:
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", fragment)
    s = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", " \t ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmlmod.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in s.split("\n")]
    return "\n".join(l for l in lines if l)


def html_pages(raw: str, target_chars: int = 3500) -> list[str]:
    """SEC HTML → 「页」：优先按分页符切；没有分页符就按约 3500 字切。"""
    parts = PAGE_BREAK.split(raw)
    pages = [html_text(p) for p in parts] if len(parts) > 3 else []
    pages = [p for p in pages if p.strip()]
    if len(pages) < 3:
        txt = html_text(raw)
        pages, cur = [], ""
        for para in txt.split("\n"):
            if len(cur) + len(para) > target_chars and cur:
                pages.append(cur)
                cur = ""
            cur += para + "\n"
        if cur.strip():
            pages.append(cur)
    return pages[:MAX_PAGES]


# ====================================================================== 找报告
def _cninfo_org(code: str) -> str | None:
    cache = filings_dir() / "cninfo_orgs.json"
    orgs = {}
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        try:
            orgs = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            orgs = {}
    if not orgs:
        try:
            r = requests.get("https://www.cninfo.com.cn/new/data/szse_stock.json", headers=UA, timeout=T)
            orgs = {x["code"]: x["orgId"] for x in r.json().get("stockList") or []}
            cache.write_text(json.dumps(orgs), encoding="utf-8")
        except Exception:
            pass
    return orgs.get(code) or (("gssh0" if code.startswith("6") else "gssz0") + code)


CNINFO_CAT = {"annual": "category_ndbg_szsh", "semi": "category_bndbg_szsh", "q1": "category_yjdbg_szsh", "q3": "category_sjdbg_szsh"}
CN_TITLE = {"annual": "年年度报告", "semi": "年半年度报告", "q1": "年第一季度报告", "q3": "年第三季度报告"}
BAD_TITLE = re.compile(r"摘要|英文|English|取消|更正前|已取消|补充|问询|说明")


def _pick_title(items: list[dict], year: int, kind: str, key: str) -> dict | None:
    want = f"{year}{CN_TITLE[kind]}"
    good = [x for x in items if want in re.sub(r"\s+", "", x[key]) and not BAD_TITLE.search(x[key])]
    if not good:
        return None
    # 有「修订版」时取最新
    return sorted(good, key=lambda x: ("修订" in x[key], x.get("time", "")), reverse=True)[0]


def find_a(code: str, year: int, kind: str) -> dict:
    tries = []
    try:
        org = _cninfo_org(code)
        span = f"{year + 1}-01-01~{year + 1}-12-31" if kind == "annual" else f"{year}-01-01~{year + 1}-06-30"
        r = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query", headers={**UA, "Referer": "https://www.cninfo.com.cn/", "X-Requested-With": "XMLHttpRequest"},
                          data={"stock": f"{code},{org}", "tabName": "fulltext", "pageSize": 30, "pageNum": 1, "column": "sse" if code.startswith("6") else "szse",
                                "category": CNINFO_CAT[kind], "plate": "", "seDate": span, "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}, timeout=T)
        items = [{"title": re.sub(r"<[^>]+>", "", a["announcementTitle"]), "time": datetime.fromtimestamp(a["announcementTime"] / 1000, CN_TZ).strftime("%Y-%m-%d"),
                  "url": "https://static.cninfo.com.cn/" + a["adjunctUrl"]} for a in (r.json().get("announcements") or []) if a.get("adjunctUrl")]
        hit = _pick_title(items, year, kind, "title")
        if hit:
            return {**hit, "source": "巨潮资讯 cninfo", "page_url": f"https://www.cninfo.com.cn/new/disclosure/stock?stockCode={code}&orgId={org}", "format": "pdf"}
        tries.append("巨潮资讯：没有找到对应报告")
    except Exception as e:
        tries.append(f"巨潮资讯：{str(e)[:60]}")
    try:  # 东方财富公告中心：定期报告
        items = []
        for page in (1, 2, 3):
            r = requests.get("https://np-anotice-stock.eastmoney.com/api/security/ann", headers=UA, timeout=T,
                             params={"sr": -1, "page_size": 100, "page_index": page, "ann_type": "A", "client_source": "web", "stock_list": code, "f_node": 1, "s_node": 0})
            lst = (r.json().get("data") or {}).get("list") or []
            items += [{"title": a["title"], "time": a["display_time"][:10], "url": f"https://pdf.dfcfw.com/pdf/H2_{a['art_code']}_1.pdf",
                       "page_url": f"https://data.eastmoney.com/notices/detail/{code}/{a['art_code']}.html"} for a in lst]
            if _pick_title(items, year, kind, "title") or not lst:
                break
        hit = _pick_title(items, year, kind, "title")
        if hit:
            return {**hit, "source": "东方财富公告中心", "format": "pdf"}
        tries.append("东方财富：没有找到对应报告")
    except Exception as e:
        tries.append(f"东方财富：{str(e)[:60]}")
    return {"error": f"找不到 {code} {year} 年的{KINDS[kind]}", "tried": tries}


HK_T2 = {"annual": "40100", "semi": "40200", "q1": "40300", "q3": "40300"}


def find_hk(code5: str, year: int, kind: str, lang: str = "zh") -> dict:
    try:
        r = requests.get("https://www1.hkexnews.hk/search/prefix.do", params={"callback": "cb", "lang": "ZH", "type": "A", "name": code5, "market": "SEHK"}, headers=UA, timeout=T)
        info = json.loads(r.text[r.text.index("(") + 1:r.text.rindex(")")])
        sid = next((x["stockId"] for x in info.get("stockInfo") or [] if x["code"] == code5), None)
        if not sid:
            return {"error": f"披露易找不到股票 {code5}"}
        y0, y1 = (year + 1, year + 1) if kind == "annual" else (year, year + 1)
        r = requests.get("https://www1.hkexnews.hk/search/titleSearchServlet.do", headers=UA, timeout=T, params={
            "sortDir": "0", "sortByOptions": "DateTime", "category": "0", "market": "SEHK", "stockId": str(sid), "documentType": "-1",
            "fromDate": f"{y0}0101", "toDate": f"{y1}1231", "title": "", "searchType": "1", "t1code": "40000", "t2Gcode": "-2",
            "t2code": HK_T2[kind], "rowRange": "50", "lang": lang})
        rows = json.loads(r.json().get("result") or "[]")
    except Exception as e:
        return {"error": f"披露易查询失败：{str(e)[:80]}"}
    words = {"annual": ("年報", "年度報告", "Annual Report"), "semi": ("中期報告", "中期报告", "Interim Report"), "q1": ("季度", "Quarterly"), "q3": ("季度", "Quarterly")}[kind]
    cand = []
    for x in rows:
        t = htmlmod.unescape(x.get("TITLE") or "")
        if str(year) in t and any(w.lower() in t.lower() for w in words) and (x.get("FILE_TYPE") or "").upper() == "PDF":
            d = x.get("DATE_TIME", "")
            dt = f"{d[6:10]}-{d[3:5]}-{d[0:2]}" if re.match(r"\d\d/\d\d/\d{4}", d) else d
            cand.append({"title": t, "time": dt, "url": "https://www1.hkexnews.hk" + x["FILE_LINK"], "size": x.get("FILE_INFO")})
    if not cand:
        return {"error": f"披露易没有找到 {code5} {year} 年的{KINDS[kind]}", "tried": [f"t2code={HK_T2[kind]} {y0}–{y1}"]}
    hit = sorted(cand, key=lambda c: c["time"])[-1]
    return {**hit, "source": "港交所披露易 HKEXnews", "page_url": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh", "format": "pdf"}


def find_us(ticker: str, year: int, kind: str) -> dict:
    from .research import _cik
    hit = _cik(ticker)
    if not hit:
        return {"error": f"SEC 里找不到 {ticker}"}
    cik, title = hit
    r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_UA, timeout=T)
    j = r.json()
    forms = ("10-K", "20-F", "40-F", "10-K/A") if kind == "annual" else ("10-Q",)
    cand = []

    def scan(rec):
        for i, f in enumerate(rec.get("form") or []):
            if f in forms:
                rd = rec["reportDate"][i] or rec["filingDate"][i]
                cand.append({"form": f, "report_date": rd, "time": rec["filingDate"][i], "acc": rec["accessionNumber"][i], "doc": rec["primaryDocument"][i]})

    scan(j["filings"]["recent"])
    if not any(c["report_date"][:4] == str(year) for c in cand):
        for fobj in j["filings"].get("files") or []:  # 更早的申报在分页文件里
            try:
                scan(requests.get(f"https://data.sec.gov/submissions/{fobj['name']}", headers=SEC_UA, timeout=T).json())
            except Exception:
                pass
            if any(c["report_date"][:4] == str(year) for c in cand):
                break
    mine = [c for c in cand if c["report_date"][:4] == str(year) and not c["form"].endswith("/A")] or [c for c in cand if c["report_date"][:4] == str(year)]
    if not mine:
        return {"error": f"SEC 没有 {ticker} 报告期在 {year} 年的 {'/'.join(forms)}", "tried": ["data.sec.gov submissions"]}
    c = sorted(mine, key=lambda c: c["report_date"])[-1]
    acc = c["acc"].replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{c['doc']}"
    return {"title": f"{title} {c['form']}（报告期 {c['report_date']}）", "time": c["time"], "url": url, "source": "SEC EDGAR", "format": "html",
            "page_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={c['form'].split('/')[0]}", "form": c["form"], "report_date": c["report_date"]}


def locate(symbol: str, year: int, kind: str = "annual") -> dict:
    from .marketdata import parse
    p = parse(symbol)
    kind = kind if kind in KINDS else "annual"
    if p["market"] == "A":
        return find_a(p["code"], year, kind if kind != "quarter" else "q3")
    if p["market"] == "HK":
        return find_hk(p["code"], year, kind if kind != "quarter" else "semi")
    if p["market"] == "US":
        return find_us(p["code"], year, "annual" if kind == "annual" else "quarter")
    return {"error": "指数没有定期报告"}


# ====================================================================== 下载 + 缓存
def _download(url: str, fmt: str) -> bytes:
    hdr = SEC_UA if "sec.gov" in url else UA
    r = requests.get(url, headers=hdr, timeout=60, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(256 * 1024):
        buf.write(chunk)
        if buf.tell() > MAX_BYTES:
            raise ValueError(f"文件超过 {MAX_BYTES // 1024 // 1024}MB，已放弃")
    data = buf.getvalue()
    if fmt == "pdf" and not data.startswith(b"%PDF"):
        raise ValueError("下载的不是 PDF（可能被拦截或链接失效）")
    return data


def load(symbol: str, year: int, kind: str = "annual", fetch: bool = True) -> dict:
    """返回 {meta, pages} 或 {error}。fetch=False 时只读缓存（辩论里用，避免卡住）。"""
    from .marketdata import parse
    p = parse(symbol)
    d = filings_dir() / f"{p['market']}_{p['code']}"
    stem = f"{int(year)}_{kind}"
    js = d / f"{stem}.json"
    if js.exists():
        try:
            return json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not fetch:
        return {"error": "本地还没有缓存这份报告", "cached": False}
    with _lock(str(js)):
        if js.exists():
            return json.loads(js.read_text(encoding="utf-8"))
        meta = locate(symbol, int(year), kind)
        if meta.get("error"):
            return meta
        d.mkdir(parents=True, exist_ok=True)
        raw_path = d / f"{stem}.{meta['format']}"
        try:
            data = raw_path.read_bytes() if raw_path.exists() else _download(meta["url"], meta["format"])
            raw_path.write_bytes(data)
        except Exception as e:
            return {"error": f"下载失败：{str(e)[:100]}", "url": meta["url"], "source": meta["source"]}
        t0 = time.time()
        try:
            if meta["format"] == "pdf":
                pages = pdf_pages(data)
            else:
                pages = html_pages(data.decode("utf-8", errors="ignore"))
        except ImportError as e:
            return {"error": f"{e}；可以先打开原文链接阅读", "url": meta["url"], "source": meta["source"], "missing_dependency": "pypdf"}
        except Exception as e:
            return {"error": f"解析失败：{str(e)[:100]}", "url": meta["url"], "source": meta["source"]}
        pages = strip_headers(pages)
        if meta["format"] == "pdf" and garbled_ratio(pages) > .3:
            alt = find_hk(p["code"], int(year), kind, lang="en") if p["market"] == "HK" else {"error": "无英文版"}
            if alt.get("error"):
                return {"error": "PDF 的中文字体没有编码映射，抽出来是乱码；装上 pdfminer.six（pip install pdfminer.six）可以正确抽取，或打开原文阅读",
                        "url": meta["url"], "source": meta["source"], "missing_dependency": "pdfminer.six"}
            try:
                data2 = _download(alt["url"], "pdf")
                pages2 = strip_headers(pdf_pages(data2))
            except Exception as e:
                return {"error": f"中文版乱码、英文版下载失败：{str(e)[:80]}", "url": meta["url"], "source": meta["source"]}
            (d / f"{stem}_en.pdf").write_bytes(data2)
            meta = {**alt, "note": "中文版 PDF 抽不出正确文字（字体无编码映射，装 pdfminer.six 可解决），已改用英文版"}
            pages = pages2
        if sum(len(x.strip()) for x in pages) < 200:
            return {"error": "PDF 里抽不出文字（可能是扫描件），可以打开原文阅读", "url": meta["url"], "source": meta["source"]}
        meta.update({"symbol": p["yahoo"], "year": int(year), "kind": kind, "pages": len(pages), "parsed_seconds": round(time.time() - t0, 1),
                     "parser": pdf_backend() if meta["format"] == "pdf" else "html", "cached_at": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")})
        rec = {"meta": meta, "pages": pages, "sections": find_sections(pages)}
        js.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return rec


def strip_headers(pages: list[str]) -> list[str]:
    """去掉每页重复的页眉页脚（出现在一半以上页面的首尾行）和「17 / 143」式页码。"""
    if len(pages) < 6:
        return pages
    cnt = Counter()
    for p in pages:
        ls = [l.strip() for l in (p or "").split("\n") if l.strip()]
        cnt.update(set(re.sub(r"\d+", "#", l) for l in ls[:2] + ls[-2:]))
    rep = {k for k, v in cnt.items() if v >= len(pages) * .5 and len(k) < 80}
    out = []
    for p in pages:
        ls = [l for l in (p or "").split("\n")]
        keep = [l for i, l in enumerate(ls) if not ((i < 3 or i >= len(ls) - 3) and (re.sub(r"\d+", "#", l.strip()) in rep or re.fullmatch(r"\s*-?\s*\d+\s*(?:/\s*\d+)?\s*-?\s*", l)))]
        out.append("\n".join(keep))
    return out


# ====================================================================== 章节
HEAD_RE = {
    "mdna": [r"^第[一二三四五六七八九十]+节\s*(?:管理层讨论与分析|经营情况讨论与分析|董事会报告)", r"^(?:管理層討論及分析|管理层讨论与分析)\s*$",
             r"^item\s*7\.?\s*management.s discussion", r"^management(?:.s)? discussion and analysis\b", r"^(?:管理層討論及分析|管理层讨论与分析)"],
    "risk": [r"可能面[对临]的风险", r"^(?:\(?[一二三四五六七八九十]+\)?[、.]?\s*)?(?:重大)?风险因素", r"主要風險及不確定(?:因素|性)", r"^風險(?:管理|因素)",
             r"^item\s*1a\.?\s*risk factors", r"^risk factors\s*$", r"^principal risks", r"^risk management\b"],
    "financial": [r"主要会计数据和财务指标", r"^(?:五年)?財務(?:摘要|概要)", r"^财务摘要", r"^item\s*8\.?\s*financial statements", r"^financial (?:highlights|summary)",
                  r"^selected (?:consolidated )?financial data"],
    "segment": [r"主营业务分行业、分产品、分地区", r"^分产品情况|^主营业务分产品", r"^分部資料|^分部资料|^分部信息|^經營分部", r"segment information", r"reportable segments?"],
}
SPAN = {"mdna": 40, "risk": 4, "financial": 3, "segment": 3}
CHAPTER_RE = re.compile(r"^(?:第[一二三四五六七八九十]+节|item\s*\d+[a-z]?\.?\s|part\s+i+\b)", re.I | re.M)


def is_toc(text: str) -> bool:
    t = text[:4000]
    if re.search(r"^\s*(?:目\s*录|目\s*錄|contents|table of contents)\s*$", t, re.I | re.M):
        return True
    heads = len(CHAPTER_RE.findall(t))
    dots = len(re.findall(r"\.{5,}|…{3,}", t))
    return heads >= 5 or dots >= 6


def find_sections(pages: list[str]) -> dict:
    """每个章节 → [起页, 止页]（1 起算，含）。找不到的章节不给。"""
    out = {}
    lines = [[l.strip() for l in (p or "").split("\n") if l.strip()] for p in pages]
    toc = [is_toc(p or "") for p in pages]
    for sec, pats in HEAD_RE.items():
        start, head = None, ""
        for pat in pats:
            rx = re.compile(pat, re.I)
            for i, ls in enumerate(lines):
                if toc[i]:
                    continue
                h = next((l for l in ls[:80] if rx.search(l) and len(l) < (150 if l.isascii() else 40)), None)
                if h:
                    start, head = i, h
                    break
            if start is not None:
                break
        if start is None:
            continue
        span = 40 if re.match(r"item\s*\d", head, re.I) else SPAN[sec]  # 10-K 的 Item 一直到下一个 Item
        end = min(len(pages) - 1, start + span - 1)
        if sec in ("mdna", "risk") or span > 3:  # 到下一章为止
            for j in range(start + 1, min(len(pages), start + span)):
                if not toc[j] and any(CHAPTER_RE.match(l) and len(l) < 80 for l in lines[j][:8]):
                    if sec == "risk" and re.match(r"^item\s*1b", " ".join(lines[j][:8]), re.I) is None and j - start < 2:
                        continue
                    end = j - 1 if j - 1 >= start else start
                    break
        out[sec] = [start + 1, end + 1, head]
    return out


# ====================================================================== 检索
def tokens(text: str) -> list[str]:
    text = text.lower()
    out = re.findall(r"[a-z][a-z\-']+|\d+(?:\.\d+)?", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        out += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return out


def span_pages(pages: list[str], lo: int, hi: int, head: str = "") -> list[tuple[int, str]]:
    """章节范围内的 (页码, 文本)：第一页从标题行开始；遇到下一章（第X节 / Item N）就截止。"""
    out = []
    for pno in range(lo, hi + 1):
        txt = pages[pno - 1] or ""
        if pno == lo and head and head in txt:
            txt = txt[txt.index(head) + len(head):]
            txt = head + txt
        body = txt[len(head):] if pno == lo and head and txt.startswith(head) else txt
        sub = re.match(r"^\s*[（(][一二三四五六七八九十]+[)）]", head or "")  # 「(四)可能面对的风险」这类小节：到下一个同级或上级标号为止
        stop = r"(?im)^\s*(第[一二三四五六七八九十]+节|item\s*\d+[a-z]?\.)\s*\S"
        if sub:
            stop = r"(?m)(?:^|\s)(?:[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、|第[一二三四五六七八九十]+节)\s*\S"
        m = re.search(stop, body)
        if m and head and not body[m.start():].strip().startswith(head):
            cut = (len(txt) - len(body)) + m.start()
            if txt[:cut].strip():
                out.append((pno, txt[:cut]))
            break
        out.append((pno, txt))
    return out


def chunks(pages: list[str], lo: int = 1, hi: int | None = None, size: int = 520, head: str = "") -> list[dict]:
    """把 [lo, hi] 页切成约 size 字的段，段不跨页。head 给定时第一页从标题行开始、到下一章为止。"""
    hi = hi or len(pages)
    out = []
    for pno, txt in span_pages(pages, lo, hi, head):
        txt = re.sub(r"[ \t]+", " ", re.sub(r"[√□]\s*适用\s*[√□]\s*不适用", "", txt))
        paras = [x.strip() for x in re.split(r"\n(?=\S)", txt) if x.strip()]
        cur = ""
        for para in paras:
            if len(cur) + len(para) > size and len(cur) > 80:
                out.append({"page": pno, "text": cur.strip()})
                cur = ""
            cur += para + ("\n" if len(para) < 40 else " ")
        if cur.strip():
            out.append({"page": pno, "text": cur.strip()})
    return [c for c in out if len(c["text"]) > 20]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = .72):
        self.docs, self.k1, self.b = docs, k1, b
        self.N = len(docs) or 1
        self.avg = sum(len(d) for d in docs) / self.N if docs else 1
        df = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + .5) / (n + .5)) for t, n in df.items()}
        self.tf = [Counter(d) for d in docs]

    def score(self, q: list[str]) -> list[float]:
        out = []
        for d, tf in zip(self.docs, self.tf):
            s, L = 0.0, len(d) or 1
            for t in set(q):
                f = tf.get(t)
                if f:
                    s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * L / self.avg))
            out.append(s)
        return out


DEFAULT_Q = {"mdna": "经营 收入 利润 增长 下降 原因 主要 业务 revenue growth decline drivers outlook",
             "risk": "风险 不确定 下滑 竞争 监管 政策 价格 risk uncertain adverse regulation competition",
             "financial": "营业收入 净利润 每股收益 增减 总资产 现金流 revenue net income earnings per share",
             "segment": "分产品 分地区 分部 收入 毛利率 占比 segment revenue margin"}


def page_url(meta: dict, page: int, text: str = "") -> str:
    if meta.get("format") == "pdf":
        return f"{meta['url']}#page={page}"
    words = re.sub(r"\s+", " ", text).strip().split(" ")[:6]
    return meta["url"] + ("#:~:text=" + quote(" ".join(words)) if words and words[0] else "")


def _clip(t: str, n: int = 600) -> str:
    t = re.sub(r"\s*\n\s*", " ", t).strip()
    return t if len(t) <= n else t[:n].rstrip() + "…"


def read_filing(symbol: str, year: int | None = None, section: str = "all", query: str = "", kind: str = "annual", k: int = 4) -> dict:
    year = int(year or (datetime.now(CN_TZ).year - 1))
    sec = norm_section(section)
    rec = load(symbol, year, kind)
    if rec.get("error"):
        return {"symbol": symbol, "year": year, **rec}
    meta, pages = rec["meta"], rec["pages"]
    span = (rec.get("sections") or {}).get(sec) if sec != "all" else None
    warn = []
    if sec != "all" and not span:
        warn.append(f"没有定位到「{SECTIONS[sec]}」章节，改在全文里检索")
    lo, hi = (span or (1, len(pages)))[:2]
    cs = chunks(pages, lo, hi, head=span[2] if span and len(span) > 2 else "")
    q = tokens((query or "") + " " + (DEFAULT_Q.get(sec, "") if not query else ""))
    if not cs:
        return {"symbol": symbol, "year": year, "error": "这一部分没有可读文字", "url": meta["url"]}
    sc = BM25([tokens(c["text"]) for c in cs]).score(q) if q else [0.0] * len(cs)
    if not query:  # 没问题时：章节开头的段落略加权，保证给出概览
        sc = [s + max(0, 1.5 - i * .15) for i, s in enumerate(sc)]
    order = sorted(range(len(cs)), key=lambda i: -sc[i])[:max(1, min(int(k or 4), 8))]
    picked = sorted(order, key=lambda i: (cs[i]["page"], i))
    passages = [{"page": cs[i]["page"], "text": _clip(cs[i]["text"]), "score": round(sc[i], 2), "url": page_url(meta, cs[i]["page"], cs[i]["text"])} for i in picked]
    if query and max(sc) <= 0:
        warn.append("关键词在这一部分没有命中，给出的是章节开头的段落")
    return {"symbol": meta.get("symbol", symbol), "year": year, "kind": kind, "title": meta.get("title"), "source": meta["source"], "url": meta["url"],
            "as_of": meta.get("time"), "section": sec, "section_name": SECTIONS[sec], "section_pages": [lo, hi] if span else None, "pages_total": meta.get("pages"),
            "query": query, "passages": passages, "warnings": warn + ([meta["note"]] if meta.get("note") else []),
            "note": "每段带页码；引用时写「第 N 页」，链接可直接跳到 PDF 对应页"}


# ====================================================================== 两年对比
SENT_SPLIT = re.compile(r"(?<=[。；！？!?;])|(?<=\.)\s+(?=[A-Z(])")
KEY_WORDS = re.compile(r"风险|下降|下滑|不确定|监管|竞争|关税|诉讼|减值|压力|波动|制裁|出口|管制|政策|需求|库存|价格|汇率|"
                       r"risk|regulat|tariff|competition|declin|uncertain|export|litigation|impair|supply|demand|sanction|geopolit", re.I)


def sentences(pages: list[str], lo: int, hi: int, head: str = "") -> list[dict]:
    out = []
    for pno, raw in span_pages(pages, lo, hi, head):
        raw = re.sub(r"[√□]\s*适用\s*[√□]\s*不适用", "", raw)
        txt = re.sub(r"\s*\n\s*", "", raw) if re.search(r"[\u4e00-\u9fff]", raw) else re.sub(r"\s+", " ", raw)
        for s in SENT_SPLIT.split(txt):
            s = s.strip()
            digits = sum(ch.isdigit() for ch in s)
            if 6 <= len(s) <= 400 and not re.fullmatch(r"[\d\s,.%()\-—/|]+", s) and "适用" not in s and "適用" not in s and digits / len(s) < .3:
                out.append({"page": pno, "text": s})
    return out


def _norm(s: str) -> str:
    s = re.sub(r"\d[\d,.]*", "#", s.lower())
    return re.sub(r"[\s，,。.;；:：、“”\"'()（）]", "", s)


def _grams(s: str) -> set:
    return {s[i:i + 3] for i in range(max(1, len(s) - 2))}


def diff_sentences(a: list[dict], b: list[dict], same: float = .86, near: float = .55) -> dict:
    """a=旧年、b=新年。返回 added / removed / modified（数字不同但句子相同算 modified）。"""
    na = [(_norm(x["text"]), x) for x in a]
    nb = [(_norm(x["text"]), x) for x in b]
    ga = [_grams(n) for n, _ in na]
    gb = [_grams(n) for n, _ in nb]

    def best(n, g, pool, pgs):
        top, bi = 0.0, -1
        cand = sorted(range(len(pool)), key=lambda j: -len(g & pgs[j]) / (len(g | pgs[j]) or 1))[:6]
        for j in cand:
            r = difflib.SequenceMatcher(None, n, pool[j][0], autojunk=False).ratio()
            if r > top:
                top, bi = r, j
        return top, bi

    added, modified, matched_a = [], [], set()
    for (n, x), g in zip(nb, gb):
        r, j = best(n, g, na, ga) if na else (0, -1)
        if r >= same:
            matched_a.add(j)
            if x["text"] != na[j][1]["text"] and re.sub(r"\d", "", x["text"]) == re.sub(r"\d", "", na[j][1]["text"]):
                modified.append({"before": na[j][1]["text"], "after": x["text"], "page_a": na[j][1]["page"], "page_b": x["page"], "similarity": round(r, 2)})
        elif r >= near:
            matched_a.add(j)
            modified.append({"before": na[j][1]["text"], "after": x["text"], "page_a": na[j][1]["page"], "page_b": x["page"], "similarity": round(r, 2)})
        else:
            added.append({**x, "similarity": round(r, 2)})
    removed = []
    for i, ((n, x), g) in enumerate(zip(na, ga)):
        if i in matched_a:
            continue
        r, _ = best(n, g, nb, gb) if nb else (0, -1)
        if r < near:
            removed.append({**x, "similarity": round(r, 2)})
    return {"added": added, "removed": removed, "modified": modified}


def key_rank(items: list[dict], key: str = "text", n: int = 8) -> list[dict]:
    def score(x):
        t = x[key] if key in x else x.get("after", "")
        kw = len(KEY_WORDS.findall(t))
        L = len(t)
        return kw * 2 + (1 if 25 <= L <= 220 else 0) + (1 - x.get("similarity", 0))
    return sorted(items, key=score, reverse=True)[:n]


def compare_filings(symbol: str, year_a: int | None = None, year_b: int | None = None, section: str = "risk", kind: str = "annual", fetch: bool = True, n: int = 8) -> dict:
    yb = int(year_b or (datetime.now(CN_TZ).year - 1))
    ya = int(year_a or yb - 1)
    if ya > yb:
        ya, yb = yb, ya
    sec = norm_section(section)
    if sec == "all":
        sec = "risk"
    ra, rb = load(symbol, ya, kind, fetch), load(symbol, yb, kind, fetch)
    for y, r in ((ya, ra), (yb, rb)):
        if r.get("error"):
            return {"symbol": symbol, "year_a": ya, "year_b": yb, "section": sec, "error": f"{y} 年报告：{r['error']}", "url": r.get("url")}
    warn = []
    spans = []
    for y, r in ((ya, ra), (yb, rb)):
        sp = (r.get("sections") or {}).get(sec)
        if not sp:
            warn.append(f"{y} 年报告没有定位到「{SECTIONS[sec]}」章节，用全文前后各 6 页兜底，结果可能不准")
            sp = [1, min(6, len(r["pages"]))]
        spans.append(sp)
    sa, sb = sentences(ra["pages"], *spans[0][:2], *(spans[0][2:3])), sentences(rb["pages"], *spans[1][:2], *(spans[1][2:3]))
    spans = [x[:2] for x in spans]
    if not sa or not sb:
        return {"symbol": symbol, "year_a": ya, "year_b": yb, "section": sec, "error": "章节里没有可比的文字"}
    d = diff_sentences(sa, sb)
    ma, mb = ra["meta"], rb["meta"]
    fmt = lambda xs, meta: [{"page": x["page"], "text": _clip(x["text"], 260), "url": page_url(meta, x["page"], x["text"])} for x in xs]
    ratio = 1 - (len(d["added"]) + len(d["removed"])) / max(1, len(sa) + len(sb))
    return {"symbol": mb.get("symbol", symbol), "section": sec, "section_name": SECTIONS[sec], "year_a": ya, "year_b": yb,
            "source": mb["source"], "url": mb["url"], "url_a": ma["url"], "as_of": mb.get("time"), "as_of_a": ma.get("time"),
            "pages_a": spans[0], "pages_b": spans[1], "sentences_a": len(sa), "sentences_b": len(sb),
            "counts": {"added": len(d["added"]), "removed": len(d["removed"]), "modified": len(d["modified"])}, "unchanged_ratio": round(ratio, 2),
            "added": fmt(key_rank(d["added"], n=n), mb), "removed": fmt(key_rank(d["removed"], n=n), ma),
            "modified": [{"before": _clip(x["before"], 200), "after": _clip(x["after"], 200), "page_a": x["page_a"], "page_b": x["page_b"],
                          "url": page_url(mb, x["page_b"], x["after"])} for x in key_rank(d["modified"], key="after", n=min(5, n))],
            "warnings": warn + ([f"两年「{SECTIONS[sec]}」的文字完全相同（共 {len(sb)} 句），没有新增或删除"] if not (d["added"] or d["removed"] or d["modified"]) else []),
            "note": f"added = {yb} 年新出现的句子（页码是 {yb} 年报告），removed = {ya} 年有、{yb} 年删掉的（页码是 {ya} 年报告）；按风险关键词排序取关键句"}


def cached_list() -> list[dict]:
    out = []
    for f in sorted(filings_dir().glob("*/*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))["meta"]
            out.append({k: m.get(k) for k in ("symbol", "year", "kind", "title", "source", "pages", "time", "url")})
        except Exception:
            pass
    return out
