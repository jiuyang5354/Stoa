"""长对话 token 预算与压缩（v0.17）。

- est_tokens：有 tiktoken 用 cl100k_base，没有就按字符估算（中日韩字 ≈ 1 token/字，其余 ≈ 4 字符/token）。
- budget：模型上下文窗口（providers.CONTEXT，默认 32k，LLM_CONTEXT 覆盖）× CONTEXT_RATIO（默认 0.6），
  留出输出和工具定义的空间；CONTEXT_BUDGET 可直接指定。
- 超预算时把较早的消息滚动摘要成「会话纪要」（sessions.db 的 session_digest），保留最近几轮原文 + 纪要 + 关键数字/用户决定；
  纪要由模型写（失败或无模型时用规则摘要），关键数字和用户决定总是规则抽取、原样保留，并带上当时的来源名（引用元数据）。
- clip_tool：工具结果过长时按结构收缩（列表取前几条、长文本截断），来源元数据（_cite / url / source / as_of / date / warnings…）原样保留，
  不再从 JSON 中间硬切，v0.15 的引用核对照常工作（来源是按完整结果登记的）。
"""
from __future__ import annotations

import json
import os
import re

DIGEST_TAG = "[压缩会话纪要]"
KEEP_TURNS = 3          # 压缩时至少保留最近几轮原文（不够放时逐步减到只留当前这一轮）
MAX_FACTS = 40
META_KEYS = {"_cite", "url", "link", "source", "as_of", "date", "time", "symbol", "warnings", "error", "page", "year", "kind",
             "title", "name", "section", "period", "unit"}
DECIDE = re.compile(r"决定|打算|计划|记住|记一下|以后|不要|别再|止损|止盈|目标价|持有|持仓|买了|卖了|加仓|减仓|清仓|成本|仓位|偏好|不碰|设在|定在|底线")
_ENC = {"enc": None, "tried": False}


def est_tokens(text) -> int:
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    if not _ENC["tried"]:
        _ENC["tried"] = True
        try:
            import tiktoken  # 可选依赖，不进 Windows 包
            _ENC["enc"] = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENC["enc"] = None
    if _ENC["enc"] is not None:
        try:
            return len(_ENC["enc"].encode(text, disallowed_special=()))
        except Exception:
            pass
    cjk = len(re.findall(r"[\u3000-\u9fff\uff00-\uffef]", text))
    return cjk + (len(text) - cjk + 3) // 4


def msg_tokens(m: dict) -> int:
    n = 4 + est_tokens(m.get("content") or "")
    for c in m.get("tool_calls") or []:
        n += 8 + est_tokens(c.get("function", {}).get("arguments") or "") + est_tokens(c.get("function", {}).get("name") or "")
    return n


def total(messages: list[dict]) -> int:
    return sum(msg_tokens(m) for m in messages)


def tools_tokens(tools: list[dict] | None) -> int:
    return est_tokens(json.dumps(tools or [], ensure_ascii=False)) if tools else 0


def budget(llm, tools=None) -> int:
    """输入预算（token）：窗口 × 比例 − 工具定义，最少 2000。"""
    if os.getenv("CONTEXT_BUDGET"):
        try:
            return max(1000, int(os.getenv("CONTEXT_BUDGET")))
        except ValueError:
            pass
    ctx = getattr(llm, "context", None) or 32000
    try:
        ratio = float(os.getenv("CONTEXT_RATIO") or 0.6)
    except ValueError:
        ratio = 0.6
    return max(2000, int(ctx * ratio) - tools_tokens(tools))


# ---------------------------------------------------------------- 关键数字与决定
def _sentences(text: str) -> list[str]:
    return [s.strip(" -*•\t") for s in re.split(r"(?<=[。！？!?；;\n])", text or "") if s.strip(" -*•\t")]


def facts_from(rows: list[dict]) -> list[str]:
    """从原文里抽关键数字和用户决定。rows：[{role, text, time?}]，assistant 的 text 可以带「—— 来源 ——」清单，用来把 [n] 换成来源名。"""
    from .cite import parse_sources, strip_sources
    user, bot = [], []
    for r in rows:
        txt = r.get("text") or r.get("content") or ""
        if r.get("role") == "user":
            if txt.startswith("[系统"):
                continue
            for s in _sentences(txt):
                if DECIDE.search(s) or re.search(r"\d", s):
                    user.append(f"用户{('（' + r['time'][:10] + '）') if r.get('time') else ''}：{s[:140]}")
        elif r.get("role") == "assistant":
            srcs = parse_sources(txt)
            body = re.sub(r"\[\[(?:debate|cards):[^\]]*\]\]", "", strip_sources(txt))
            for s in _sentences(body):
                if not re.search(r"\d+(?:\.\d+)?", s) or len(s) < 6:
                    continue
                refs = [int(x) for x in re.findall(r"\[(\d+)\]", s)]
                if not refs and not DECIDE.search(s):
                    continue  # 没出处、也不是决定的数字（多半是推算/口水话）不进纪要
                names = [re.sub(r"\s*·\s*https?://\S+", "", srcs[n]).strip() for n in refs if n in srcs]
                plain = re.sub(r"\[\d+\]", "", s)[:140]
                bot.append(f"Stoa：{plain}" + (f"（来源：{'；'.join(dict.fromkeys(names))}）" if names else ""))
    out = list(dict.fromkeys(user)) + list(dict.fromkeys(bot))
    return out[:MAX_FACTS]


def rule_digest(old: str, rows: list[dict]) -> str:
    """没有模型可用时的规则摘要：问过什么 + 每次回答的第一句结论。"""
    from .cite import strip_sources
    asks, ends = [], []
    for r in rows:
        t = " ".join((r.get("text") or "").split())
        if r.get("role") == "user" and not t.startswith("[系统"):
            asks.append(t.rstrip("；;。？?！! ")[:50])
        elif r.get("role") == "assistant":
            first = _sentences(re.sub(r"\[\d+\]|\[\[[^\]]*\]\]", "", strip_sources(r.get("text") or "")))
            if first:
                ends.append(first[0].rstrip("；;。，, ")[:80])
    parts = []
    if old:
        parts.append(old.split("\n关键数字与用户决定")[0].strip())
    if asks:
        parts.append("用户问过：" + "；".join(asks[-12:]))
    if ends:
        parts.append("Stoa 的结论：" + "；".join(ends[-8:]))
    return "\n".join(parts)[:1600]


def make_digest(llm, old: dict | None, rows: list[dict]) -> tuple[str, list[str]]:
    """滚动摘要：旧纪要 + 新归档的原文 → 新纪要。返回（纪要正文，关键数字与决定）。"""
    old_text = (old or {}).get("text", "")
    old_body = old_text.split("\n关键数字与用户决定")[0].strip()
    facts = list(dict.fromkeys(((old or {}).get("facts") or []) + facts_from(rows)))[-MAX_FACTS:]
    convo = "\n".join(f"{'用户' if r['role'] == 'user' else 'Stoa'}：{' '.join((r.get('text') or '').split())[:600]}" for r in rows)
    prompt = (f"{DIGEST_TAG} 把下面较早的对话压缩成一份「会话纪要」（≤500 字，中文，条目式）：用户关心的股票与问题、Stoa 给过的结论与判断、"
              "用户做出的决定和偏好、还没解决的问题。数字原样保留，不要编造，不要写客套话。\n\n"
              + (f"已有纪要：\n{old_body}\n\n" if old_body else "") + f"新归档的对话：\n{convo[:12000]}")
    body = ""
    if llm is not None:
        try:
            body = (llm.chat([{"role": "user", "content": prompt}], None, temperature=0.1, timeout=60).get("content") or "").strip()
        except TypeError:
            body = (llm.chat([{"role": "user", "content": prompt}], None).get("content") or "").strip()
        except Exception:
            body = ""
    if not body or len(body) < 10:
        body = rule_digest(old_text, rows)
    text = body[:2000] + ("\n关键数字与用户决定：\n" + "\n".join(f"- {f}" for f in facts) if facts else "")
    return text, facts


def digest_block(d: dict | None) -> str:
    if not d or not d.get("text"):
        return ""
    return (f"\n\n## 会话纪要（此前 {d.get('n_msgs') or ''} 条对话已归档为纪要，更早的原文不在上下文里；用户问到早先说过的内容时以此为准，"
            f"需要原话可用 session_search）\n{d['text']}")


# ---------------------------------------------------------------- 工具结果收缩
def _shrink(o, lc: int, sc: int, depth: int = 0):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k in META_KEYS and not isinstance(v, (dict, list)):
                out[k] = v if not isinstance(v, str) or k in ("url", "link") or len(v) <= 160 else v[:160] + "…"
            elif k == "warnings":
                out[k] = v
            else:
                out[k] = _shrink(v, lc, sc, depth + 1)
                if isinstance(v, list) and len(v) > lc:  # 省略条数写在旁边的键里，不往列表里塞字符串，结构不变
                    out[f"_{k}_omitted"] = len(v) - lc
        return out
    if isinstance(o, list):
        return [_shrink(x, lc, sc, depth + 1) for x in o[:lc]]
    if isinstance(o, str) and len(o) > sc:
        return o[:sc] + "…"
    return o


def clip_tool(result, limit: int = 6000) -> str:
    """工具结果转 JSON 字符串，超过 limit 字就按结构收缩，保留来源元数据。"""
    s = json.dumps(result, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s
    try:
        obj = json.loads(s)
    except Exception:
        return s[:limit]
    wrapped = not isinstance(obj, dict)
    if wrapped:
        obj = {"items": obj}
    for lc, sc in ((12, 600), (8, 400), (6, 260), (4, 160), (3, 100), (2, 60), (1, 40)):
        o = _shrink(obj, lc, sc)
        o["_truncated"] = f"原始结果 {len(s)} 字，已按结构收缩到每组前 {lc} 条、长文本 {sc} 字；来源编号与链接保留"
        s2 = json.dumps(o, ensure_ascii=False, default=str)
        if len(s2) <= limit:
            return s2
    meta = {k: v for k, v in obj.items() if k in META_KEYS}
    meta["_truncated"] = f"原始结果 {len(s)} 字，太长只保留来源信息；需要细节请缩小查询范围"
    return json.dumps(meta, ensure_ascii=False, default=str)[:max(limit, 400)]
