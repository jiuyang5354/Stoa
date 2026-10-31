"""sweep_filings（v0.37）：N 家公司 × M 年 × 一个章节的批量定期报告矩阵，一轮出表。

关键设计：子文档不进主上下文。每份报告在子流程里先抽成一行结构化事实（约 60 token），主模型只看到矩阵。
- 下载/解析/缓存复用 filings.load（首次十几秒，二次走缓存），ThreadPoolExecutor(6) 并行；单份失败进 coverage.failed，不整体失败。
- 数字字段（营收、净利润、毛利率、EPS…）先用规则/正则在财务章节抽，数字必须原样出现在该页，宁少勿乱；
  营业收入 / 营业总收入、归母净利润 / 净利润按字面区分，取到的是哪个口径就写哪个。
- 文本字段（渠道库存表述、风险关键词…）先 BM25 定位章节内最相关的段落；有模型时每份文档一次小抽取（要求原文摘句，
  摘句必须是原文子串，否则退回规则摘句），没有模型就用规则摘句。每个值都带页码，人可核对。
- 返回 columns + rows（可直接 write_file 成 xlsx；export=true 时这里直接写好返回下载链接）。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import filings as fl

MAX_DOCS = 30
WORKERS = 6
TEXT_CHARS = 60

# 数字字段：字面 → 正则（按优先级）。营业收入与营业总收入分开，避免口径混用
NUMERIC = {
    "营业总收入": [r"营业总收入"],
    "营业收入": [r"营业收入(?!.{0,3}(?:占比|比重))"],
    "营收": [r"营业总收入", r"营业收入", r"\b(?:total )?revenues?\b"],
    "归母净利润": [r"归属于(?:上市公司|母公司)(?:股东|所有者)的净利润(?!.{0,4}扣除)", r"归母净利润"],
    "净利润": [r"(?<!归属于上市公司股东的)(?<!扣除非经常性损益后的)净利润", r"\bnet income\b"],
    "扣非净利润": [r"扣除非经常性损益后的净利润", r"扣非净利润"],
    "毛利率": [r"毛利率", r"\bgross margin\b"],
    "净利率": [r"净利率", r"\bnet margin\b"],
    "每股收益": [r"基本每股收益", r"每股收益", r"\b(?:diluted )?(?:earnings per share|EPS)\b"],
    "经营现金流": [r"经营活动产生的现金流量净额", r"\bnet cash provided by operating activities\b"],
    "总资产": [r"总资产", r"\btotal assets\b"],
    "研发投入": [r"研发投入", r"研发费用", r"\bresearch and development\b"],
    "合同负债": [r"合同负债"],
    "存货": [r"存货"],
}
ALIAS = {"revenue": "营收", "营业额": "营收", "收入": "营收", "net income": "净利润", "net profit": "净利润", "gross margin": "毛利率", "eps": "每股收益",
         "ocf": "经营现金流", "经营活动现金流": "经营现金流", "r&d": "研发投入", "研发": "研发投入"}
NUM = r"(-?\d[\d,]*(?:\.\d+)?)\s*(万亿元|亿元|万元|千元|元|%|billion|million|thousand)?"


def field_key(name: str) -> str | None:
    n = name.strip()
    low = n.lower()
    if n in NUMERIC:
        return n
    if low in ALIAS:
        return ALIAS[low]
    for k in sorted(NUMERIC, key=len, reverse=True):
        if k in n:
            return k
    return None


def _num_in(pages: list[str], key: str, prefer) -> dict | None:
    order = list(range(len(pages)))
    if prefer:
        lo, hi = prefer[0] - 1, prefer[1]
        order = list(range(max(0, lo), min(hi, len(pages)))) + [i for i in order if not (lo <= i < hi)]
    for pat in NUMERIC[key]:
        rx = re.compile(f"({pat})" + r"[^\d\n\-]{0,24}?" + NUM, re.I)
        for i in order:
            m = rx.search(pages[i])
            if not m:
                continue
            raw, unit = m.group(2), (m.group(3) or "")
            if key in ("毛利率", "净利率") and unit not in ("%", ""):
                continue
            if raw.replace(",", "") in {"1", "2", "3", "4", "5"} and not unit:  # 序号、目录页码之类
                continue
            return {"value": raw.replace(",", "") + unit, "label": m.group(1).strip(), "page": i + 1}
    return None


def _best_passage(pages, span, query: str) -> dict | None:
    lo, hi = (span or (1, len(pages)))[:2]
    cs = fl.chunks(pages, lo, hi, head=span[2] if span and len(span) > 2 else "")
    if not cs:
        return None
    q = fl.tokens(query)
    sc = fl.BM25([fl.tokens(c["text"]) for c in cs]).score(q) if q else [0.0] * len(cs)
    i = max(range(len(cs)), key=lambda j: sc[j])
    if q and sc[i] <= 0:
        return None
    return {"page": cs[i]["page"], "text": cs[i]["text"], "score": sc[i]}


HEADING = re.compile(r"第[一二三四五六七八九十]+节\s*\S+|[一二三四五六七八九十]+、\S{2,24}?(?:情况|分析|概述)|\(?[（(][一二三四五六七八九十\d]+[)）]\S{0,16}\s|Item\s*\d+[A-Z]?\.[^.]{0,80}?(?=[A-Z][a-z])")


def _rule_quote(text: str, query: str) -> str:
    text = HEADING.sub(" ", re.sub(r"\s*\n\s*", " ", text))
    sents = [s.strip() for s in fl.SENT_SPLIT.split(text) if s and s.strip()]
    if not sents:
        return ""
    q = set(fl.tokens(query))
    best = max(sents, key=lambda s: len(q & set(fl.tokens(s))))
    return best[:TEXT_CHARS] + ("…" if len(best) > TEXT_CHARS else "")


def _llm_quotes(llm, fields: list[str], passages: dict) -> dict:
    """每份文档一次小抽取：只许摘原文，不许改写；摘句不是原文子串就丢掉（调用方退回规则摘句）。"""
    blob = "\n\n".join(f"【{f}】第 {p['page']} 页：{p['text'][:700]}" for f, p in passages.items() if p)
    if not blob:
        return {}
    prompt = ("从下面的年报段落里，为每个字段摘出最能回答它的一句原文（不超过 60 字，必须逐字照抄原文，不得改写、不得总结）；"
              "段落里没有相关内容就填空字符串。只输出 JSON 对象，键为字段名。\n字段：" + "、".join(fields) + "\n\n" + blob)
    try:
        r = llm.chat([{"role": "system", "content": "你是严格的信息抽取器，只摘原文。"}, {"role": "user", "content": prompt}], None)
        txt = (r.get("content") if isinstance(r, dict) else str(r)) or ""
        m = re.search(r"\{.*\}", txt, re.S)
        got = json.loads(m.group(0)) if m else {}
    except Exception:
        return {}
    out = {}
    for f in fields:
        v = str(got.get(f) or "").strip().strip("「」\"' ")
        p = passages.get(f)
        if v and p and re.sub(r"\s+", "", v) in re.sub(r"\s+", "", p["text"]):
            out[f] = v[:TEXT_CHARS]
    return out


def _name(meta: dict, symbol: str) -> str:
    t = str(meta.get("title") or "")
    m = re.match(r"^\s*([^\d（(]{2,16}?)\s*(?:股份有限公司)?\s*(?:19|20)\d\d", t)
    return (m.group(1).strip() if m else "") or symbol


def one_doc(symbol: str, year: int, section: str, extract: list, kind: str = "annual", llm=None, fetch: bool = True) -> dict:
    rec = fl.load(symbol, int(year), kind, fetch=fetch)
    if rec.get("error"):
        return {"symbol": symbol, "year": int(year), "error": str(rec["error"])[:120]}
    meta, pages, secs = rec["meta"], rec["pages"], rec.get("sections") or {}
    span = secs.get(section) if section != "all" else None
    fin_span = secs.get("financial")
    vals, pages_used, missing, notes = {}, {}, [], []
    text_fields, passages = [], {}
    for f in extract:
        k = field_key(f)
        if k:
            hit = _num_in(pages, k, fin_span or span)
            if hit:
                vals[f] = hit["value"]
                pages_used[f] = hit["page"]
                if hit["label"] != f and k == "营收":
                    notes.append(f"{f}取「{hit['label']}」")
            else:
                missing.append(f)
        else:
            text_fields.append(f)
            passages[f] = _best_passage(pages, span, f + " " + fl.DEFAULT_Q.get(section, ""))
    if text_fields:
        got = _llm_quotes(llm, text_fields, passages) if llm is not None else {}
        for f in text_fields:
            p = passages.get(f)
            q = got.get(f) or (_rule_quote(p["text"], f) if p else "")
            if q:
                vals[f] = "「" + q + "」"
                pages_used[f] = p["page"]
            else:
                missing.append(f)
    first = min(pages_used.values()) if pages_used else (span[0] if span else 1)
    return {"symbol": meta.get("symbol", symbol), "name": _name(meta, symbol), "year": int(year), "values": vals, "pages": pages_used,
            "missing": missing, "notes": notes, "title": meta.get("title"), "source": meta.get("source"), "url": meta.get("url"), "as_of": meta.get("time"),
            "page_url": fl.page_url(meta, first), "section_found": bool(span) or section == "all", "llm": bool(text_fields and llm is not None)}


def _warm_one(args):
    symbol, year, kind, home = args
    import os as _os
    if home:
        _os.environ["STOCK_AGENT_HOME"] = home
    try:
        rec = fl.load(symbol, int(year), kind)
        return None if not rec.get("error") else str(rec["error"])[:100]
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:100]}"


def _warm(pairs, kind: str):
    """首次解析是 CPU 密集（pypdf 每份 5–30 秒），线程受 GIL 限制：没缓存的报告先用多进程并行下载+解析写缓存，
    之后抽取全部走缓存。SWEEP_PROCESSES=0 关闭（测试或受限环境）；进程池起不来就退回线程。"""
    import os
    if (os.getenv("SWEEP_PROCESSES") or "auto").lower() in ("0", "off", "false"):
        return
    cold = [p for p in pairs if fl.load(p[0], int(p[1]), kind, fetch=False).get("error")]
    if len(cold) < 2:
        return
    try:
        from concurrent.futures import ProcessPoolExecutor
        home = os.getenv("STOCK_AGENT_HOME") or ""
        with ProcessPoolExecutor(max_workers=max(2, min(WORKERS, os.cpu_count() or 2))) as ex:
            list(ex.map(_warm_one, [(s, y, kind, home) for s, y in cold], timeout=600))
    except Exception:
        return  # 退回线程：one_doc 里照常 load


def sweep_filings(symbols, years, section: str = "mdna", extract=None, max_docs: int = 12, kind: str = "annual", llm=None,
                  fetch: bool = True, export: bool = False, session: str | None = None) -> dict:
    if isinstance(symbols, str):
        symbols = [s for s in re.split(r"[,\s，、]+", symbols) if s]
    if isinstance(years, (int, str)):
        years = [int(y) for y in re.findall(r"\d{4}", str(years))]
    if isinstance(extract, str):
        extract = [x for x in re.split(r"[,，、;；]+", extract) if x.strip()]
    symbols = list(dict.fromkeys(str(s).strip() for s in symbols or [] if str(s).strip()))
    years = sorted({int(y) for y in years or []})
    sec = fl.norm_section(section)
    extract = [str(x).strip() for x in (extract or []) if str(x).strip()][:8] or ["营收", "净利润", fl.SECTIONS.get(sec, "要点")]
    if not symbols or not years:
        return {"error": "给 symbols（代码列表）和 years（年份列表）"}
    pairs = [(s, y) for s in symbols for y in years]
    cap = max(1, min(int(max_docs or 12), MAX_DOCS))
    todo, skipped = pairs[:cap], pairs[cap:]

    if fetch:
        _warm([p for p in todo], kind)

    def run(p):
        try:
            return one_doc(p[0], p[1], sec, extract, kind, llm, fetch)
        except Exception as e:
            return {"symbol": p[0], "year": p[1], "error": f"{type(e).__name__}: {str(e)[:100]}"}

    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(run, todo))
    ok = [r for r in res if "error" not in r]
    failed = [f"{r['symbol']} {r['year']}：{r['error']}" for r in res if "error" in r]
    columns = ["代码", "名称", "年份", *extract, "页码(按列)", "缺失"]
    rows, docs = [], []
    for r in ok:
        pg = "p." + "/".join(str(r["pages"].get(f, "-")) for f in extract)  # 与抽取列一一对应
        rows.append([r["symbol"], r["name"], r["year"], *[r["values"].get(f, "") for f in extract], pg, "、".join(r["missing"])])
        docs.append({k: r[k] for k in ("symbol", "year", "title", "source", "url", "page_url", "as_of", "pages", "values")})
    warns = []
    if skipped:
        warns.append(f"超过 max_docs={cap}，有 {len(skipped)} 份没读：" + "、".join(f"{s} {y}" for s, y in skipped[:6]) + "（可分批再调或调大 max_docs，最多 30）")
    nf = [f"{r['symbol']} {r['year']}" for r in ok if not r["section_found"]]
    if nf:
        warns.append("这些报告没定位到章节、改在全文检索：" + "、".join(nf[:6]))
    notes = sorted({n for r in ok for n in r["notes"]})
    out = {"section": sec, "section_name": fl.SECTIONS[sec], "kind": kind, "columns": columns, "rows": rows,
           "coverage": {"requested": len(pairs), "fetched": len(ok), "failed": failed, "skipped": [f"{s} {y}" for s, y in skipped]},
           "docs": docs, "extract_mode": "数字=规则正则（数字原样出现在该页）；文本=" + ("模型逐字摘句（非原文子串则退回规则）" if any(r["llm"] for r in ok) else "规则摘句"),
           "note": "每格都带页码，可点来源核对；空格=没抽到（宁少勿乱），不要自己补数。要 Excel 直接把 columns+rows 交给 write_file(format=xlsx)。"}
    if notes:
        out["caliber"] = notes
    if warns:
        out["warnings"] = warns
    if export and rows:
        from . import exports
        links = [d["page_url"] for d in docs]
        w = exports.write(f"批量年报矩阵-{fl.SECTIONS[sec]}", "xlsx", columns=columns + ["链接"], rows=[r + [u] for r, u in zip(rows, links)], session=session)
        if w.get("markdown_link"):
            out["markdown_link"] = w["markdown_link"]
        elif w.get("error"):
            out.setdefault("warnings", []).append("导出失败：" + str(w["error"])[:80])
    return out


TOOL_NAMES = {"sweep_filings"}
TOOL_SPECS = [
    {"name": "sweep_filings",
     "description": "批量读定期报告并按同一张表抽取：N 家公司 × M 年 × 同一章节一次完成（并行、走缓存），每份只回一行（数字规则抽取、文本原文摘句，都带页码）。"
                    "横向对比多家公司同一章节的说法时用，不要逐份 read_filing。返回 columns+rows，可直接交给 write_file 出 Excel（或 export=true）。",
     "parameters": {"type": "object", "properties": {
         "symbols": {"type": "array", "items": {"type": "string"}, "description": "如 [\"600519.SS\",\"000858.SZ\"]"},
         "years": {"type": "array", "items": {"type": "integer"}, "description": "报告期年份，如 [2023,2024,2025]"},
         "section": {"type": "string", "enum": ["mdna", "risk", "financial", "segment", "all"]},
         "extract": {"type": "array", "items": {"type": "string"}, "description": "列名，如 [\"营收\",\"毛利率\",\"渠道库存表述\"]"},
         "max_docs": {"type": "integer", "description": "默认 12，最多 30"},
         "kind": {"type": "string", "enum": ["annual", "semi", "q1", "q3", "quarter"]},
         "export": {"type": "boolean"}}, "required": ["symbols", "years"]}},
]
