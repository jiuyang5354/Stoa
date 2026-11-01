"""v0.38 长文档完整读取（盲评 v1 Q31：股东信是 PDF，fetch_url 不读 PDF、且只给前 6000 字 → 要点抽取不全）。

- load(url)      取全文：HTML 正文（同 research.fetch_url 的清洗）或 PDF（filings.pdf_pages 逐页，最多 400 页），返回文本与页边界。
- digest(...)    全文分段（约 6000 字一段、200 字重叠），每段各抽一次要点（模型并行，最多 3 路；没有模型时按规则抽：带数字/人名职位/
                 关键词的句子 + 每段首句），按段合并，返回 coverage（共几段、读了几段、总字数）——保证从头到尾每一段都读过，不只读开头。
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

CHUNK = 6000
OVERLAP = 200
MAX_CHUNKS = 40
MAP_PROMPT = ("下面是一份长文档的第 {i}/{n} 段（{where}）。逐条列出这一段里所有实质要点：经营与财务数字、资本配置/回购/投资动作、人事任免与职位变化、"
              "对未来的判断与风险、明确的原话观点。每条一行、以「- 」开头、≤70 字，数字、人名、职位、日期照抄原文，不要加原文没有的内容，不要写总结套话。"
              "{focus}这一段没有实质内容就只写「- （本段无实质要点）」。\n\n<<<原文\n{text}\n原文>>>")


def load(url: str, max_pages: int = 400) -> dict:
    from . import research
    r = research.fetch_url(url, max_chars=2_000_000, pages=True)
    if r.get("error"):
        return r
    if r.get("pdf_pages") is not None:
        pages = r["pdf_pages"][:max_pages]
        text, bounds = "", []
        for i, p in enumerate(pages):
            bounds.append((len(text), i + 1))
            text += (p or "").strip() + "\n\n"
        return {"url": r["url"], "title": r.get("title") or "", "text": text, "pages": len(pages), "bounds": bounds, "kind": "pdf"}
    return {"url": r["url"], "title": r.get("title") or "", "text": r.get("text") or "", "pages": None, "bounds": [], "kind": "html"}


def chunks(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> list[tuple[int, str]]:
    out, i = [], 0
    while i < len(text):
        j = min(len(text), i + size)
        if j < len(text):  # 尽量断在段落/句末
            k = max(text.rfind("\n", i + size // 2, j), text.rfind("。", i + size // 2, j), text.rfind(". ", i + size // 2, j))
            if k > i:
                j = k + 1
        out.append((i, text[i:j]))
        if j >= len(text):
            break
        i = max(j - overlap, i + 1)
    return out


def _page_at(bounds, pos) -> int | None:
    pg = None
    for start, p in bounds:
        if start <= pos:
            pg = p
        else:
            break
    return pg


KEY = re.compile(r"\d|董事长|主席|首席执行官|CEO|chair|chief executive|succeed|retire|appoint|named|repurchase|buyback|回购|收购|acquisition|"
                 r"operating earnings|insurance|float|cash|treasury|dividend|风险|risk|outlook|展望|we (?:will|expect|believe)", re.I)


def rule_points(text: str, limit: int = 12) -> list[str]:
    """无模型时的规则抽取：每段首句 + 含数字/职位/关键词的句子（去重，按原文顺序）。"""
    sents = [s.strip() for s in re.split(r"(?<=[。！？.!?])\s+|\n+", text) if len(s.strip()) >= 25]
    pick, seen = [], set()
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    firsts = {re.split(r"(?<=[。.!?])\s", p.strip())[0][:200] for p in paras}
    for s in sents:
        s2 = s[:200]
        if (s2 in firsts or KEY.search(s)) and s2 not in seen:
            seen.add(s2)
            pick.append(s2)
        if len(pick) >= limit:
            break
    return pick


def digest(url: str = "", text: str = "", focus: str = "", llm=None, title: str = "") -> dict:
    doc = {"url": url, "title": title, "text": text, "bounds": [], "pages": None, "kind": "text"}
    if url and not text:
        doc = load(url)
        if doc.get("error"):
            return doc
    body = doc.get("text") or ""
    if len(body.strip()) < 200:
        return {"url": doc.get("url"), "error": "没有读到正文（可能是扫描件或需要登录），换一个来源（公司官网 PDF / SEC 原文）"}
    parts = chunks(body)
    total = len(parts)
    if total > MAX_CHUNKS:  # 超长：等距抽 MAX_CHUNKS 段并如实说明
        step = total / MAX_CHUNKS
        parts = [parts[int(i * step)] for i in range(MAX_CHUNKS)]
    n = len(parts)
    foc = f"特别留意：{focus}。" if focus else ""

    def one(ix):
        i, (pos, seg) = ix
        pg = _page_at(doc.get("bounds") or [], pos)
        where = f"约第 {pg} 页" if pg else f"第 {pos}–{pos + len(seg)} 字"
        items = None
        if llm is not None:
            try:
                t = llm.chat([{"role": "user", "content": MAP_PROMPT.format(i=i + 1, n=n, where=where, focus=foc, text=seg)}], None, temperature=0, timeout=180)
                txt = (t.get("content") or "") if isinstance(t, dict) else str(t)
                items = [re.sub(r"^\s*[-•*]\s*", "", l).strip() for l in txt.splitlines() if re.match(r"\s*[-•*]\s*\S", l)]
                items = [x for x in items if x and "本段无实质要点" not in x]
            except Exception:
                items = None
        mode = "model" if items is not None else "rule"
        if items is None:
            items = rule_points(seg)
        return {"part": i + 1, "where": where, **({"page": pg} if pg else {}), "points": items, "by": mode}

    with ThreadPoolExecutor(3 if llm is not None else 1) as ex:
        res = list(ex.map(one, enumerate(parts)))
    res.sort(key=lambda r: r["part"])
    heads = [l.strip() for l in body.splitlines() if 4 <= len(l.strip()) <= 60 and (l.strip().isupper() or re.match(r"^(?:[一二三四五六七八九十]+、|\d+[.、]\s)", l.strip()))][:40]
    return {"url": doc.get("url"), "title": doc.get("title"), "source": "原文全文分段抽取", "kind": doc.get("kind"), "pages": doc.get("pages"),
            "total_chars": len(body), "coverage": {"parts": total, "read": n, "complete": n == total,
                                                    "note": "全文每一段都抽过要点" if n == total else f"文档过长，等距读了 {n}/{total} 段，回答里要说明"},
            "outline": heads, "parts": res,
            "note": "按 parts 顺序整理成完整摘要：每个主题都要覆盖到，后半部分的要点不要漏；引用写「约第 N 页」。"}


TOOL_NAMES = {"doc_digest"}


def run_tool(name: str, a: dict, agent=None):
    llm = None
    if agent is not None and os.getenv("STOA_MOCK_LLM") != "1":
        try:
            llm = agent._sweep_llm()
        except Exception:
            llm = None
    return digest(a.get("url") or "", a.get("text") or "", a.get("focus") or "", llm)


TOOL_SPECS = [
    {"name": "doc_digest", "description": "长文档完整读取：股东信、年报全文、研报、长网页或 PDF（url）——分段读完全文，每段抽要点（数字、资本配置、人事变动、展望与风险），返回各段要点与覆盖率。"
     "要「总结/提炼要点」一份长文档时用它（fetch_url 只适合短网页，只给开头）。focus 可写关注点。",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "focus": {"type": "string"}}, "required": ["url"]}},
]
