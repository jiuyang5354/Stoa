"""数字强制引用（Citatio）：工具数据带来源与时间戳，回答里的关键数字必须找得到出处。

流程（agent.ask 每轮）：
  1. Sources.add(tool, args, result)：给工具结果登记来源（名称、URL、as_of），结果里加 `_cite` 编号交给模型，
     同时把结果里的每个数字记成 (数值, 来源编号)。
  2. 模型写回答时在关键数字后写 [n]（写不写都行，核对器会补）。
  3. verify(answer, sources)：抽取回答里的关键数字（价格、涨跌幅、财务数字；跳过日期、天数、股数、小整数），
     和本轮工具数字比对：原值、四舍五入、万/亿换算、小数↔百分数、两数之差、涨跌幅、乘积、(a-b)×c 都算有出处。
  4. 有找不到出处的数字：agent 让模型修正一次；还不行就在数字后标「（未核实）」。
  5. finalize()：给有出处的数字补上 [n]，按出现顺序重新编号，末尾附「来源」清单。网页把 [n] 渲染成可悬停的小角标。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
UNVERIFIED = "（未核实）"
SOURCES_HEAD = "—— 来源 ——"
UNIT = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3}
# 回答里的数字 + 可选单位
NUM_RE = re.compile(r"(?<![\w.\[/#])([-+−]?\d[\d,]*(?:\.\d+)?)(?:(\s?)(%|％|万亿|亿|万|千))?")
# 跟在数字后面就不算「行情/财务数字」的量词
SKIP_AFTER = re.compile(r"^\s?(?:个?交易日|日|天|个|周|月|年|只|条|次|股|手|轮|位|名|小时|分钟|秒|季度|季|期|家|项|点钟|号|岁|页|题|步|倍杠杆)")
SKIP_SRC = re.compile(r"^\s?(?:个?交易日|日|天|周|个月|月|年|季度|小时|分钟)")
DATEISH_AFTER = set("-/:年月日号时")
DATEISH_BEFORE = set("-/:.")
URL_RE = re.compile(r"https?://\S+")


def today() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def quote_url(symbol: str) -> str:
    s = symbol.upper()
    m = re.match(r"^(\d{6})\.(SS|SZ|BJ)$", s)
    if m:
        return f"https://quote.eastmoney.com/{ {'SS': 'sh', 'SZ': 'sz', 'BJ': 'bj'}[m.group(2)] }{m.group(1)}.html"
    return f"https://finance.yahoo.com/quote/{s}"


def _scale(tok: str, unit: str | None) -> float:
    v = float(tok.replace(",", "").replace("−", "-"))
    return v * UNIT.get(unit or "", 1)


def numbers_in(obj, out: list | None = None) -> list[float]:
    """递归取出对象里的全部数字；字符串里的「1.70万亿」同时记原值和换算后的值。"""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("_cite", "url", "link"):
                continue
            numbers_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            numbers_in(v, out)
    elif isinstance(obj, bool) or obj is None:
        pass
    elif isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        s = URL_RE.sub(" ", obj)
        s = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}:\d{2}(?::\d{2})?", " ", s)  # 日期、时刻不是数据
        for m in re.finditer(r"((?<![\d.])-?\d[\d,]*(?:\.\d+)?)\s?(万亿|亿|万|千)?", s):
            if not m.group(2) and "." not in m.group(1) and SKIP_SRC.match(s[m.end(1):m.end(1) + 6]):
                continue  # 「20 日线」「第十日」里的窗口长度不是数据
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            out.append(v)
            if m.group(2):
                out.append(v * UNIT[m.group(2)])
    return out


class Sources:
    """一轮对话里所有数据的来源登记处（线程安全：辩论里分析师并行调工具）。"""

    def __init__(self):
        self.items: list[dict] = []
        self.nums: list[tuple[float, int]] = []
        self.lock = threading.Lock()

    def new(self, name: str, url: str = "", as_of: str = "", kind: str = "tool", tool: str = "") -> int:
        with self.lock:
            for it in self.items:  # 同名同 URL 同时间的来源只登记一次
                if it["name"] == name and it["url"] == (url or "") and it["as_of"] == (as_of or ""):
                    return it["id"]
            sid = len(self.items) + 1
            self.items.append({"id": sid, "name": name, "url": url or "", "as_of": as_of or "", "kind": kind, "tool": tool})
            return sid

    def bind(self, obj, sid: int):
        vals = numbers_in(obj)
        with self.lock:
            self.nums += [(v, sid) for v in vals]

    def get(self, sid: int) -> dict | None:
        return self.items[sid - 1] if 0 < sid <= len(self.items) else None

    # -------------------------------------------------- 登记工具结果
    def add_context(self, text: str, name: str, kind: str):
        """用户的提问、记忆里的数字也算出处（用户自己说的持仓成本等）。"""
        if text and re.search(r"\d", text):
            sid = self.new(name, kind=kind)
            self.bind(text, sid)
            return sid

    def add(self, tool: str, args: dict, result):
        """登记来源，返回给模型的结果（dict 里带 _cite 编号）。不改原对象。"""
        try:
            return self._add(tool, args or {}, result)
        except Exception:
            sid = self.new(tool, as_of=today(), tool=tool)
            self.bind(result, sid)
            return result

    def _add(self, tool, args, result):
        if tool == "get_quotes" and isinstance(result, dict):
            out = {}
            for sym, v in result.items():
                if isinstance(v, dict) and v:
                    src = v.get("source") or "行情源"
                    sid = self.new(f"行情 · {sym} · {src}", quote_url(sym), str(v.get("date") or today()), tool=tool)
                    self.bind(v, sid)
                    out[sym] = {**v, "_cite": sid}
                else:
                    out[sym] = v
            return out
        if tool in ("get_news", "get_announcements", "web_search") and isinstance(result, dict):
            key = {"get_news": "news", "get_announcements": "announcements", "web_search": "results"}[tool]
            items = result.get(key)
            if isinstance(items, list):
                new = []
                for it in items[:12]:
                    if isinstance(it, dict):
                        when = str(it.get("time") or it.get("date") or "")[:16]
                        name = f"{it.get('source') or {'get_news': '新闻', 'get_announcements': '公告', 'web_search': '网页'}[tool]} · {str(it.get('title') or '')[:40]}"
                        sid = self.new(name, it.get("url") or it.get("link") or "", when, tool=tool)
                        self.bind(it, sid)
                        new.append({**it, "_cite": sid})
                    else:
                        new.append(it)
                return {**result, key: new + items[12:]}
        if tool == "get_financials" and isinstance(result, dict) and not result.get("error"):
            sym = str(result.get("symbol") or args.get("symbol") or "")
            sid = self.new(f"财报与估值 · {sym} · {result.get('source') or '财务数据'}", result.get("url") or quote_url(sym),
                           str(result.get("as_of") or today()), tool=tool)
            self.bind(result, sid)
            return {**result, "_cite": sid}
        if tool == "market_overview" and isinstance(result, dict):
            sid = self.new("指数与自选一览 · 行情源", "", str(result.get("date") or result.get("as_of") or today()), tool=tool)
            self.bind(result, sid)
            return {**result, "_cite": sid}
        if tool == "fetch_url":
            url = args.get("url", "")
            sid = self.new(f"网页 · {(result or {}).get('title', '')[:40] if isinstance(result, dict) else ''}".rstrip(" ·"), url, today(), tool=tool)
            self.bind(result, sid)
            return {**result, "_cite": sid} if isinstance(result, dict) else result
        if tool == "deep_analysis":  # 辩论内部的数据调用已经登记过；这里只登记结论本身
            sid = self.new("Agora 辩论纪要", "", today(), kind="debate", tool=tool)
            self.bind({k: v for k, v in (result or {}).items() if k != "answer"} if isinstance(result, dict) else result, sid)
            return result
        names = {"memory_links": "Stoa 记忆图谱", "session_search": "Stoa 历史对话", "call_stats": "Stoa 判断账本",
                 "record_call": "Stoa 判断账本", "alert_list": "Stoa 提醒", "watchlist": "Stoa 自选股", "run_python": "沙箱计算"}
        sid = self.new(names.get(tool, tool), "", today(), kind="memory" if tool in names else "tool", tool=tool)
        self.bind(result, sid)
        if isinstance(result, dict) and "error" not in result:
            return {**result, "_cite": sid}
        return result

    # -------------------------------------------------- 比对
    def match(self, value: float, dec: int, scale: float = 1.0) -> list[int] | None:
        """value 能否由本轮数字得出。返回来源编号（可能两个），找不到返回 None。"""
        with self.lock:
            pairs = list(dict.fromkeys(self.nums))
        if not pairs:
            return None
        tol_abs = .5 * 10 ** -dec * scale + 1e-9
        av = abs(value)

        def close(s):
            return abs(av - abs(s)) <= max(abs(s) * .005, tol_abs)

        rank = {"tool": 0, "debate": 1, "memory": 2, "user": 3}
        direct = []
        for s, sid in pairs:  # 原值、小数↔百分数；多个来源都对得上时，取最接近的、再按 工具 > 辩论 > 记忆 > 提问
            for cand in (s, s * 100 if abs(s) < 5 else None):
                if cand is not None and close(cand):
                    direct.append((abs(av - abs(cand)) > 1e-9, rank.get((self.get(sid) or {}).get("kind"), 4), sid))
        if direct:
            return [min(direct)[2]]
        uniq = {}
        for s, sid in pairs:
            uniq.setdefault(s, sid)
        vals = list(uniq.items())[:90]
        for same in (True, False):  # 差、涨跌幅、乘积；先找同一来源里的两个数
            for a, sa in vals:
                for b, sb in vals:
                    if a == b or (sa == sb) != same:
                        continue
                    if close(a - b) or (b and close((a / b - 1) * 100)) or close(a * b):
                        return sorted({sa, sb})
        try:
            import numpy as np
        except ImportError:
            return None
        arr = np.array([v for v, _ in vals])
        diff = arr[:, None] - arr[None, :]
        for c, sc in vals:  # (a-b)×c：按持仓算浮盈
            if abs(c) < 1 or float(c).is_integer() is False and abs(c) < 10:
                continue
            prod = np.abs(diff * c)
            hit = np.argwhere(np.abs(av - prod) <= np.maximum(prod * .005, tol_abs))
            if len(hit):
                i, j = hit[0]
                return sorted({vals[i][1], vals[j][1], sc})
        return None


# ====================================================================== 回答侧
def has_marker(body: str, end: int) -> bool:
    """数字后面（同一短语内）已经有 [n] 角标。"""
    return bool(re.search(r"\[\d+\]", re.split(r"[，。；,;\n]", body[end:end + 14])[0]))


def split_sources(text: str) -> tuple[str, str]:
    i = text.find(SOURCES_HEAD)
    return (text[:i].rstrip(), text[i:]) if i >= 0 else (text, "")


def strip_sources(text: str) -> str:
    return split_sources(text)[0]


def key_numbers(text: str) -> list[dict]:
    """回答里需要核对的数字（位置、原文、数值、小数位、单位倍数）。"""
    body = strip_sources(text)
    masked = URL_RE.sub(lambda m: " " * len(m.group(0)), body)
    masked = re.sub(r"\[\d+\]", lambda m: " " * len(m.group(0)), masked)
    masked = re.sub(r"\[\[[^\]]*\]\]", lambda m: " " * len(m.group(0)), masked)
    out = []
    for m in NUM_RE.finditer(masked):
        tok, unit = m.group(1), m.group(3)
        s, e = m.start(1), m.end()
        before = masked[s - 1] if s else ""
        after = masked[m.end(1):m.end(1) + 1]
        if before in DATEISH_BEFORE or after in DATEISH_AFTER:
            continue
        if not unit and SKIP_AFTER.match(masked[m.end(1):m.end(1) + 6]):
            continue
        if re.match(r"^\.?[A-Za-z]", masked[m.end(1):m.end(1) + 2] or " "):  # 10-Q、5G、0700.HK 之类
            continue
        if s and re.match(r"[A-Za-z]", before):
            continue
        has_dec = "." in tok
        try:
            v = _scale(tok, unit if unit not in ("%", "％") else None)
        except ValueError:
            continue
        if not has_dec and not unit and abs(v) < 100:  # 小整数：序号、天数、个数
            continue
        if not has_dec and not unit and re.fullmatch(r"(19|20)\d\d", tok.lstrip("+-")):  # 年份
            continue
        dec = len(tok.split(".")[1]) if has_dec else 0
        scale = UNIT.get(unit or "", 1) if unit not in ("%", "％") else 1
        out.append({"start": s, "end": e, "end_num": m.end(),
                    "text": masked[s:e], "value": v, "raw": float(tok.replace(",", "").replace("−", "-")), "dec": dec, "scale": scale})
    return out


def verify(text: str, src: Sources) -> dict:
    """返回 {ok, numbers: [{text, value, sids}], unverified: [text...]}。已标「未核实」的数字不再算错。"""
    body = strip_sources(text)
    nums = []
    for n in key_numbers(body):
        flagged = body[n["end_num"]:n["end_num"] + len(UNVERIFIED) + 4].lstrip(" [0123456789]").startswith(UNVERIFIED)
        sids = src.match(n["value"], n["dec"], n["scale"]) or (src.match(n["raw"], n["dec"]) if n["scale"] != 1 else None)
        nums.append({**n, "sids": sids, "flagged": flagged})
    bad = [n["text"] for n in nums if not n["sids"] and not n["flagged"]]
    return {"ok": not bad, "numbers": nums, "unverified": bad}


def correction_prompt(bad: list[str]) -> str:
    return ("[系统核对：你回答里的这些数字在本轮工具结果里找不到出处：" + "、".join(dict.fromkeys(bad)) +
            "。请重写回答：要么调用工具取得出处，要么删掉或改成「查不到」，不要估算或凭记忆写数字。关键数字后保留 [编号]。直接给出完整的新回答。]")


def flag(text: str, src: Sources) -> tuple[str, list[str]]:
    """修正后仍找不到出处的数字：在数字后标「（未核实）」。"""
    body, tail = split_sources(text)
    v = verify(body, src)
    bad = [n for n in v["numbers"] if not n["sids"] and not n["flagged"]]
    for n in sorted(bad, key=lambda x: -x["end_num"]):
        body = body[:n["end_num"]] + UNVERIFIED + body[n["end_num"]:]
    if bad:
        body += f"\n\n注：标「未核实」的 {len(bad)} 个数字在本轮数据里找不到出处，请勿据此决策。"
    return body + ("\n\n" + tail if tail else ""), [n["text"] for n in bad]


def finalize(text: str, src: Sources) -> tuple[str, list[dict]]:
    """补引用角标、清理无效角标、按出现顺序重新编号，末尾附来源清单。返回 (文本, 用到的来源)。"""
    body, _ = split_sources(text)
    # 模型自己写的 [n]：指向不存在的来源就删掉
    body = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if src.get(int(m.group(1))) else "", body)
    v = verify(body, src)
    for n in sorted(v["numbers"], key=lambda x: -x["end_num"]):
        if not n["sids"]:
            continue
        sids = [s for s in n["sids"] if (src.get(s) or {}).get("kind") != "user"]
        if not sids:
            continue
        rest = body[n["end_num"]:]
        if re.match(r"\s?\[\d+\]", rest):
            continue
        near = re.split(r"[，。；,;\n]", rest[:14])[0]
        if all(f"[{x}]" in near for x in sids[:2]):  # 同一短语末尾已有同一个角标
            continue
        body = body[:n["end_num"]] + "".join(f"[{s}]" for s in sids[:2]) + rest
    order = list(dict.fromkeys(int(x) for x in re.findall(r"\[(\d+)\]", body)))
    remap = {old: i + 1 for i, old in enumerate(order)}
    body = re.sub(r"\[(\d+)\]", lambda m: f"[{remap.get(int(m.group(1)), m.group(1))}]", body)
    used = [{**src.get(o), "n": remap[o]} for o in order if src.get(o)]
    if not used:
        return body, []
    lines = [SOURCES_HEAD]
    for u in used:
        meta = " · ".join(x for x in (u["name"], f"截至 {u['as_of']}" if u["as_of"] else "", u["url"]) if x)
        lines.append(f"[{u['n']}] {meta}")
    return body + "\n\n" + "\n".join(lines), used


def parse_sources(text: str) -> dict[int, str]:
    _, tail = split_sources(text)
    return {int(m.group(1)): m.group(2) for m in re.finditer(r"^\[(\d+)\]\s+(.+)$", tail, re.M)}
