"""Memory Graph：把散在各处的记忆按「股票 / 主题 / 市场」串起来。

节点：
  user / note   USER.md、MEMORY.md 里的条目
  skill         自己写的技能
  call          判断账本里的一次判断（对 / 错 / 未到期）
  chat          网页对话（按标题）
  symbol        股票或指数（集线器）
  theme         板块、宏观主题（集线器）
  market        A股 / 港股 / 美股（集线器）
边：条目、技能、判断、对话 → 它提到的股票和主题；股票 → 所属市场。

没有用模型抽取，全靠规则：已知代码 + 名字（自选、指数、账本里出现过的）、裸 6 位 A 股代码、
x.HK、$TSLA 这类写法，以及一张主题词表。便宜、确定、离线可用。
"""
from __future__ import annotations

import re

import yaml

from .memory import Memory, config_path

THEMES = {
    "半导体": ["半导体", "芯片", "晶圆", "光刻", "存储", "semiconductor", "chip"],
    "AI": ["AI", "人工智能", "大模型", "算力", "GPU", "数据中心"],
    "新能源": ["新能源", "光伏", "锂电", "储能", "电动车", "风电", "EV"],
    "白酒消费": ["白酒", "消费", "食品饮料", "零售"],
    "医药": ["医药", "创新药", "医疗", "生物科技", "CXO", "biotech"],
    "银行金融": ["银行", "券商", "保险", "金融", "红利", "高股息"],
    "地产": ["地产", "房地产", "物业", "楼市"],
    "互联网": ["互联网", "平台", "游戏", "电商", "社交"],
    "汽车": ["汽车", "整车", "智驾", "自动驾驶"],
    "资源能源": ["石油", "原油", "煤炭", "有色", "黄金", "铜", "稀土"],
    "军工": ["军工", "国防"],
    "宏观利率": ["降息", "加息", "美联储", "利率", "LPR", "国债", "收益率", "通胀", "CPI", "Fed", "FOMC"],
    "汇率": ["汇率", "人民币", "美元指数", "港币"],
    "政策": ["政策", "国常会", "证监会", "两会", "刺激", "关税"],
    "风控仓位": ["止损", "止盈", "仓位", "加仓", "减仓", "回撤", "风险偏好"],
    "技术面": ["RSI", "均线", "MACD", "突破", "支撑", "压力位", "放量", "缩量"],
    "财报": ["财报", "业绩", "营收", "净利", "毛利", "ROE", "市盈率", "估值", "PE"],
}

US_STOP = {"A", "I", "AI", "PE", "ROE", "RSI", "MA", "CPI", "GDP", "EV", "ETF", "IPO", "CEO", "USD", "HK", "US", "OK",
           "MACD", "LPR", "FOMC", "FED", "GPU", "CXO", "PMI", "EPS", "YOY", "QOQ", "API", "LLM", "MCP", "SEC", "F10"}


def market_of(sym: str) -> str:
    s = sym.upper()
    if s.endswith((".SS", ".SZ", ".BJ")):
        return "A股"
    if s.endswith(".HK") or s in ("^HSI", "^HSCE"):
        return "港股"
    return "美股"


def _known(mem: Memory) -> dict[str, str]:
    """symbol -> 名字，来自配置（指数、自选）和账本。"""
    out = {}
    try:
        cfg = yaml.safe_load(config_path().read_text(encoding="utf-8")) or {}
        for group in (cfg.get("indices") or {}).values():
            for x in group or []:
                out[str(x["symbol"]).upper()] = x.get("name") or x["symbol"]
        for x in cfg.get("watchlist") or []:
            out[str(x["symbol"]).upper()] = x.get("name") or x["symbol"]
    except Exception:
        pass
    for c in mem.recent_calls(2000):
        s = str(c["symbol"]).upper()
        if c.get("name") and (s not in out or out[s] == s):
            out[s] = c["name"]
        out.setdefault(s, s)
    return out


PROVINCES = ("贵州", "江苏", "浙江", "广东", "上海", "北京", "山西", "四川", "安徽", "福建", "湖南", "湖北", "山东", "河南", "云南", "天津", "重庆")


def aliases(name: str) -> set[str]:
    """贵州茅台 → 茅台，腾讯控股 → 腾讯，美团-W → 美团。"""
    out = {name}
    n = re.sub(r"[-－](W|SW|S|B)$", "", name)
    out.add(n)
    for suf in ("控股", "集团", "股份", "有限公司", "科技"):
        if n.endswith(suf) and len(n) - len(suf) >= 2:
            out.add(n[: -len(suf)])
    if len(n) == 4 and n[:2] in PROVINCES:
        out.add(n[2:])
    return {a for a in out if len(a) >= 2}


class Linker:
    def __init__(self, known: dict[str, str]):
        self.known = known
        names = sorted(((a, s) for s, n in known.items() if n and n != s for a in aliases(n)), key=lambda x: -len(x[0]))
        self.names = names
        self.theme_re = {t: re.compile("|".join(re.escape(w) if not w.isascii() else rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])" for w in ws), re.I)
                         for t, ws in THEMES.items()}

    def symbols(self, text: str) -> set[str]:
        out = set()
        up = text.upper()
        for m in re.finditer(r"(?<![\d.])(\d{6})\.(SS|SZ|BJ|SH)(?![A-Z])", up):
            out.add(f"{m.group(1)}.{'SS' if m.group(2) == 'SH' else m.group(2)}")
        for m in re.finditer(r"(?<![\d.])(\d{1,5})\.HK\b", up):
            out.add(f"{int(m.group(1)):04d}.HK")
        for m in re.finditer(r"(?<![\d.A-Z])([036]\d{5})(?![\d.])", up):  # 裸 6 位代码
            code = m.group(1)
            out.add(code + (".SS" if code[0] == "6" else ".SZ"))
        for m in re.finditer(r"\$([A-Z]{1,5})\b", up):
            out.add(m.group(1))
        for s in self.known:
            if s.startswith("^") or "." in s:
                if s in up:
                    out.add(s)
            elif s not in US_STOP and re.search(rf"(?<![A-Z0-9$.^]){re.escape(s)}(?![A-Z0-9])", up):
                out.add(s)
        rest = text
        for n, s in self.names:
            if n in rest:
                out.add(s)
                rest = rest.replace(n, " ")
        return out

    def themes(self, text: str) -> set[str]:
        return {t for t, r in self.theme_re.items() if r.search(text)}


def _short(t: str, n: int = 46) -> str:
    t = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", " ".join(t.split()))
    return t if len(t) <= n else t[:n - 1] + "…"


def _date(t: str) -> str:
    m = re.match(r"^\[(\d{4}-\d{2}-\d{2})\]", t.strip())
    return m.group(1) if m else ""


def build(mem: Memory | None = None, with_chats: bool = True) -> dict:
    from . import skills

    mem = mem or Memory()
    known = _known(mem)
    L = Linker(known)
    nodes, edges, seen = {}, set(), set()

    def node(nid, **kw):
        if nid not in nodes:
            nodes[nid] = {"id": nid, **kw}
        return nid

    def link(a, b, kind):
        if a != b and (a, b) not in seen and (b, a) not in seen:
            seen.add((a, b))
            edges.add((a, b, kind))

    def hubs(src, text, extra_syms=()):
        for s in set(L.symbols(text)) | set(extra_syms):
            sid = node("sym:" + s, type="symbol", label=known.get(s, s), symbol=s)
            link(src, sid, "mentions")
            mk = market_of(s)
            link(sid, node("mkt:" + mk, type="market", label=mk), "in")
        for t in L.themes(text):
            link(src, node("thm:" + t, type="theme", label=t), "about")

    for target, typ in (("user", "user"), ("memory", "note")):
        for i, e in enumerate(mem.entries(target)):
            nid = node(f"{typ}:{i}", type=typ, label=_short(e), text=e, date=_date(e))
            hubs(nid, e)

    for sk in skills.list_skills():
        try:
            body = skills.view(sk["name"], count=False).get("content", "")
        except Exception:
            body = ""
        nid = node("skill:" + sk["name"], type="skill", label=sk["name"], text=sk["description"] + "\n\n" + body[:1500],
                   date=str(sk.get("updated", "")), uses=sk.get("uses", 0))
        hubs(nid, sk["name"] + " " + sk["description"] + " " + body)

    for c in mem.recent_calls(400):
        s = str(c["symbol"]).upper()
        v = c.get("verdict") or ("未到期" if c.get("status") == "open" else "")
        nid = node(f"call:{c['id']}", type="call", label=f"{c.get('name') or s} {c['view']} {c['horizon_days']}天",
                   text=f"{c['ts'][:16]} 判断 {c.get('name') or s} {c['view']}（{c['horizon_days']} 天，当时 {c['price']}）\n理由：{c.get('reason') or ''}"
                        + (f"\n结果 {c['result_pct']:+.2f}% → {v}" if c.get("result_pct") is not None else ""),
                   date=str(c["ts"])[:10], verdict=v)
        hubs(nid, c.get("reason") or "", [s])

    if with_chats:
        for ss in mem.sessions(limit=80):
            sid = ss.get("id")
            if not sid:
                continue
            text = " ".join(h["text"] for h in mem.history("web-" + sid, limit=60) if h["role"] == "user")
            if not text.strip():
                continue
            nid = node("chat:" + sid, type="chat", label=_short(ss.get("title") or text, 30), text=_short(text, 400),
                       date=str(ss.get("updated") or ss.get("ts") or "")[:10], session=sid)
            hubs(nid, text)

    # 只出现一次、又没连到别的东西的孤立集线器去掉，图更干净
    deg = {}
    for a, b, _ in edges:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    for n in nodes.values():
        n["deg"] = deg.get(n["id"], 0)
    kinds = {}
    for n in nodes.values():
        kinds[n["type"]] = kinds.get(n["type"], 0) + 1
    return {"nodes": list(nodes.values()), "edges": [{"s": a, "t": b, "kind": k} for a, b, k in sorted(edges)], "counts": kinds}


def recall(query: str, mem: Memory | None = None, limit: int = 30) -> dict:
    """顺着图找和某只股票 / 主题有关的一切：记忆条目、技能、判断（含对错）、对话。"""
    mem = mem or Memory()
    g = build(mem)
    nodes = {n["id"]: n for n in g["nodes"]}
    known = _known(mem)
    q = query.strip()
    L = Linker(known)
    hubs_ = {"sym:" + s for s in L.symbols(q)} | {"thm:" + t for t in L.themes(q)}
    hubs_ |= {n["id"] for n in g["nodes"] if n["type"] in ("symbol", "theme", "market") and q and (q.lower() == n["label"].lower() or q.upper() == n.get("symbol", "") or (len(q) >= 2 and q in n["label"]))}
    hubs_ = {h for h in hubs_ if h in nodes}
    if not hubs_:
        return {"query": q, "found": False, "hint": "图里没有这个股票或主题。可以换成代码（600519.SS / 0700.HK / NVDA）或中文名再试。",
                "themes": sorted(THEMES)}
    adj = {}
    for e in g["edges"]:
        adj.setdefault(e["s"], set()).add(e["t"])
        adj.setdefault(e["t"], set()).add(e["s"])
    hit = set()
    for h in hubs_:
        hit |= adj.get(h, set())
    hit = {i for i in hit if nodes[i]["type"] not in ("symbol", "theme", "market")}
    items = [nodes[i] for i in hit]
    items.sort(key=lambda n: n.get("date", ""), reverse=True)
    out = {"query": q, "found": True, "hubs": [nodes[h]["label"] for h in hubs_]}
    order = (("user", "关于用户"), ("note", "笔记"), ("skill", "技能"), ("call", "判断"), ("chat", "对话"))
    for typ, title in order:
        rows = [n for n in items if n["type"] == typ][:(10 if typ in ("call", "chat") else limit)]
        if rows:
            out[title] = [(n.get("text") or n["label"])[:400] for n in rows]
    calls = [n for n in items if n["type"] == "call" and n.get("verdict") in ("对", "错")]
    if calls:
        right = sum(n["verdict"] == "对" for n in calls)
        out["判断战绩"] = f"已复盘 {len(calls)} 次，对 {right} 次（{right * 100 // len(calls)}%）"
    related = {}
    for i in hit:
        for j in adj.get(i, ()):
            if j not in hubs_ and nodes[j]["type"] in ("symbol", "theme"):
                related[nodes[j]["label"]] = related.get(nodes[j]["label"], 0) + 1
    if related:
        out["常一起出现"] = [k for k, _ in sorted(related.items(), key=lambda x: -x[1])[:8]]
    return out
