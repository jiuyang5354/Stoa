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


def quote_url(symbol: str, source: str = "") -> str:
    """行情出处链接：按实际数据源给对应的公开行情页（腾讯→gu.qq.com，新浪→新浪行情页），不再一律指向东方财富首页。"""
    s = symbol.upper()
    m = re.match(r"^(\d{6})\.(SS|SZ|BJ)$", s)
    if m:
        ex = {'SS': 'sh', 'SZ': 'sz', 'BJ': 'bj'}[m.group(2)]
        if "腾讯" in source:
            return f"https://gu.qq.com/{ex}{m.group(1)}"
        if "新浪" in source:
            return f"https://finance.sina.com.cn/realstock/company/{ex}{m.group(1)}/nc.shtml"
        return f"https://quote.eastmoney.com/{ex}{m.group(1)}.html"
    h = re.match(r"^0*(\d{1,5})\.HK$", s)
    if h and "腾讯" in source:
        return f"https://gu.qq.com/hk{h.group(1).zfill(5)}"
    return f"https://finance.yahoo.com/quote/{s}"


def f10_url(symbol: str) -> str:
    """财务数据出处：A股给东方财富 F10「财务分析」页（报表原值可逐期核对），港美股给 Yahoo 财务报表页。"""
    s = (symbol or "").upper()
    m = re.match(r"^(\d{6})\.(SS|SZ|BJ)$", s)
    if m:
        return f"https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?type=web&code={ {'SS': 'SH', 'SZ': 'SZ', 'BJ': 'BJ'}[m.group(2)] }{m.group(1)}#/cwfx"
    return f"https://finance.yahoo.com/quote/{s}/financials" if s else ""


# 没有专门登记分支的工具：出处名用「中文名 · 标的 · 实际数据源」，链接用结果里的 url（或按标的推出的公开页），不写内部工具名
TOOL_TITLES = {"get_market_mood": "涨停与板块资金", "price_levels": "支撑阻力（由日线收盘价计算）", "financial_bridge": "利润结构拆解（由报表计算）",
               "get_chips": "两融与股东户数", "get_corporate_events": "公司事件（分红/解禁/增减持/质押）", "get_insiders": "内部人交易",
               "get_short_volume": "做空成交", "earnings_quality": "盈利质量（由报表计算）", "reverse_dcf": "反向 DCF（由报表计算）",
               "dcf_scenarios": "情景估值（由报表计算）", "regulatory_letters": "问询函与监管措施", "trade_replay": "交易回放（由日线计算）",
               "option_payoff": "期权到期损益（按输入计算）", "inquiry_status": "研究课题进度", "task_list": "定时任务", "pending_actions": "待确认操作"}
F10_TOOLS = {"get_financials", "get_statements", "earnings_quality", "reverse_dcf", "dcf_scenarios", "financial_bridge"}
CALC_TOOLS = {"calc", "run_python", "option_payoff"}  # 由输入推导的结果：数值本身没有「涨跌方向」含义（「亏损 6000」对 -6000 不算写反）


def source_url(tool: str, sym: str, result) -> str:
    url = (result.get("url") or result.get("link") or "") if isinstance(result, dict) else ""
    if url:
        return url
    if not sym:
        return ""
    return f10_url(sym) if tool in F10_TOOLS else quote_url(sym, str(result.get("source") or "") if isinstance(result, dict) else "")


def calc_label(args: dict, result) -> str:
    """calc 出处写成可复核的算式（「计算：loss = 30000*0.2」），不写工具名。"""
    exprs = (result or {}).get("expressions") if isinstance(result, dict) else None
    if not exprs:
        e = (args or {}).get("expressions")
        exprs = e if isinstance(e, dict) else ({"result": e} if e else {})
    parts = [f"{k} = {v}" if k != "result" else str(v) for k, v in list(exprs.items())[:4]]
    txt = "；".join(parts)
    if len(txt) > 110:
        txt = txt[:108] + "…"
    ins = (result or {}).get("inputs") if isinstance(result, dict) else None
    tail = "，输入：" + "、".join(f"{k}={v}" for k, v in list(ins.items())[:6]) if ins else ""
    return f"计算：{txt}{tail[:80]}（按题设与上文出处的数字推导）" if txt else "计算（按题设与上文出处的数字推导）"


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


def numeric_in(obj, out: list | None = None) -> list[float]:
    """只取真正的数值字段（int/float），带正负号；字符串里的数字丢了上下文（「亏损 120 亿」），不算。"""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in ("_cite", "url", "link"):
                numeric_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            numeric_in(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(float(obj))
    return out


SRC_DATE_RE = re.compile(r"(20\d\d)[-/年.](\d{1,2})[-/月.](\d{1,2})|(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)")


def dates_in(obj) -> set[str]:
    """数据里出现过的日期（YYYY-MM-DD），用来核对回答里写的「10 月 29 日收盘」是不是数据那天。"""
    out = set()
    for m in SRC_DATE_RE.finditer(str(obj)):
        g = m.groups()
        y, mo, d = (g[0], g[1], g[2]) if g[0] else (g[3], g[4], g[5])
        try:
            out.add(datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return out


FALLBACK = {"营业收入": "营收", "营业收入同比%": "营收同比%"}
DATA_LABELS = {"get_fund_flow": "资金流", "get_northbound": "北向资金", "get_dragon_tiger": "龙虎榜", "get_consensus": "分析师一致预期",
               "get_intraday": "分钟线", "get_options": "期权",
               "sec_filings": "SEC 申报清单", "sec_document": "SEC 原文", "sec_xbrl": "SEC XBRL", "sec_search": "SEC 全文检索",
               "earnings_release": "业绩新闻稿", "price_history": "历史行情", "macro_series": "宏观数据 FRED", "cn_data": "国内结构化数据", "get_statements": "原始报表", "earnings_transcript": "电话会文字稿", "volume_profile": "成交分布"}


def report_period(text: str, basis: str = "") -> tuple | None:
    year = re.search(r"20\d\d", text)
    if not year:
        return None
    if re.search(r"全年|年报|年度|12-31", text):
        part = "FY"
    elif re.search(r"前三季度|三季报", text):
        part = "YTD3"
    elif re.search(r"上半年|半年报|中报", text):
        part = "H1"
    elif re.search(r"第三季度|三季度|Q3|09-30", text, re.I):
        part = "Q3" if basis != "cumulative" else "YTD3"
    elif re.search(r"第二季度|二季度|Q2|06-30", text, re.I):
        part = "Q2" if basis != "cumulative" else "H1"
    elif re.search(r"第一季度|一季度|一季报|Q1|03-31", text, re.I):
        part = "Q1"
    else:
        return None
    return year.group(), part


SAME_DAY_MOVE = re.compile(r"(?:今天|今日|当日|单日|盘中|收盘)\s*(?:大|暴|小|微|收|上|下)?(?:涨|跌)(?:了|幅)?|(?:单日|当日|今日|今天)涨跌幅?|收(?:涨|跌)")


def name_aliases(sym: str, name: str = "") -> set[str]:
    """一只标的在回答里可能被叫的名字：数据里的名称及其简称（贵州茅台 → 茅台、腾讯控股 → 腾讯），
    加上内置常用名里指向同一代码的（茅台/贵州茅台 → 600519.SS）。只取一一对应的名字，不猜代词。"""
    out = set()
    try:
        from .graph import aliases
        if name and name != sym:
            out |= aliases(name)
    except Exception:
        pass
    try:
        from .debate import ALIAS
        out |= {k for k, v in ALIAS.items() if v.upper() == sym.upper()}
    except Exception:
        pass
    try:  # 点踩后经用户批准入库的简称（learn.py）
        from .learn import terms
        out |= {k for k, v in (terms().get("alias") or {}).items() if v.upper() == sym.upper()}
    except Exception:
        pass
    return {a for a in out if len(a) >= 2}


class Sources:
    """一轮对话里所有数据的来源登记处（线程安全：辩论里分析师并行调工具）。"""

    def __init__(self):
        self.items: list[dict] = []
        self.nums: list[tuple[float, int]] = []
        self.snums: list[tuple[float, int]] = []  # 只含数值字段（带符号），核对涨跌方向用
        self.dates: set[str] = set()
        self.quotes: list[dict] = []  # 保留标的、字段和日期，避免把另一只股票的数字当出处
        self.financials: list[dict] = []
        self.lock = threading.Lock()

    def new(self, name: str, url: str = "", as_of: str = "", kind: str = "tool", tool: str = "") -> int:
        with self.lock:
            for it in self.items:  # 同名同 URL 同时间的来源只登记一次
                if it["name"] == name and it["url"] == (url or "") and it["as_of"] == (as_of or ""):
                    return it["id"]
            sid = len(self.items) + 1
            self.items.append({"id": sid, "name": name, "url": url or "", "as_of": as_of or "", "kind": kind, "tool": tool})
            if kind != "user":
                self.dates |= dates_in(as_of or "")
            return sid

    def bind(self, obj, sid: int):
        vals = numbers_in(obj)
        sv = numeric_in(obj)
        it = self.get(sid) or {}
        ds = dates_in(obj) if it.get("kind") not in ("user",) else set()
        with self.lock:
            self.nums += [(v, sid) for v in vals]
            self.snums += [(v, sid) for v in sv]
            self.dates |= ds

    def get(self, sid: int) -> dict | None:
        return self.items[sid - 1] if 0 < sid <= len(self.items) else None

    # -------------------------------------------------- 登记工具结果
    def add_context(self, text: str, name: str, kind: str):
        """用户的提问、记忆里的数字也算出处（用户自己说的持仓成本等）。"""
        if text and re.search(r"\d", text):
            sid = self.new(name, kind=kind)
            self.bind(text, sid)
            pcts = pct_decimals(text)  # 题设「收益率 4%」→ 回答里写 0.04 也算出自题设
            if pcts:
                with self.lock:
                    self.nums += [(v, sid) for v in pcts]
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
        if tool in NO_NUMBERS:  # 技能说明、写记忆的回执：里面的数字不是数据，不能当出处
            return result
        if tool == "get_quotes" and isinstance(result, dict):
            out = {}
            for sym, v in result.items():
                if isinstance(v, dict) and v:
                    src = v.get("source") or "行情源"
                    sid = self.new(f"行情 · {sym} · {src}", v.get("url") or quote_url(sym, str(src)), str(v.get("date") or today()), tool=tool)
                    self.bind(v, sid)
                    aliases = {sym, str(v.get("name") or "")} | name_aliases(sym, str(v.get("name") or ""))
                    if "." in sym:
                        aliases.add(sym.split(".")[0])
                    with self.lock:
                        self.quotes.append({"sid": sid, "symbol": sym, "aliases": aliases - {""}, "data": dict(v)})
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
            sid = self.new(f"财报与估值 · {sym} · {result.get('source') or '财务数据'}", result.get("url") or f10_url(sym),
                           str(result.get("as_of") or today()), tool=tool)
            self.bind(result, sid)
            aliases = ({sym, str(result.get("name") or "")} | name_aliases(sym, str(result.get("name") or ""))) - {""}
            currency = (result.get("valuation") or {}).get("货币") or result.get("currency")
            rows = [(result.get("valuation") or result, None)]
            rows += [(row, report_period(str(row.get("报告期") or ""), "cumulative")) for row in result.get("main_indicators") or []]
            rows += [(row, report_period(str(row.get("period") or ""), "quarter")) for row in result.get("quarterly") or []]
            rows += [(row, report_period(str(row.get("period") or ""), "cumulative")) for row in result.get("unified") or []]
            with self.lock:
                self.financials += [{"sid": sid, "symbol": sym, "aliases": aliases, "data": dict(row),
                                     "period": period, "currency": currency, "kind": "financial"} for row, period in rows]
            return {**result, "_cite": sid}
        if tool in DATA_LABELS and isinstance(result, dict) and not result.get("error"):
            sym = str(result.get("symbol") or args.get("symbol") or "")
            name = " · ".join(x for x in (DATA_LABELS[tool], sym, str(result.get("source") or "")) if x)
            sid = self.new(name, source_url(tool, sym, result), str(result.get("as_of") or today()), tool=tool)
            self.bind({k: v for k, v in result.items() if k not in ("sources", "rule_note", "note")}, sid)
            return {**result, "_cite": sid}
        if tool == "read_filing" and isinstance(result, dict) and isinstance(result.get("passages"), list):
            title = f"{result.get('title') or '定期报告'}"
            new = []
            for ps in result["passages"]:
                sid = self.new(f"{title} · 第 {ps.get('page')} 页 · {result.get('source') or ''}".rstrip(" ·"), ps.get("url") or result.get("url") or "",
                               str(result.get("as_of") or ""), tool=tool)
                self.bind(ps.get("text") or "", sid)
                new.append({**ps, "_cite": sid})
            return {**result, "passages": new}
        if tool == "sweep_filings" and isinstance(result, dict) and isinstance(result.get("docs"), list):
            # 每份报告一个出处（带页码链接）；矩阵里的数字/摘句绑定到各自出处，行尾加「出处」列；文档元数据不进模型上下文
            out = {k: v for k, v in result.items() if k != "docs"}
            cols = list(result.get("columns") or [])
            rows = [list(r) for r in result.get("rows") or []]
            if rows and len(rows) == len(result["docs"]):
                out["columns"] = cols + ["出处"]
                for r, d in zip(rows, result["docs"]):
                    pg = min((d.get("pages") or {}).values() or [0]) or ""
                    sid = self.new(f"{d.get('title') or (str(d.get('year')) + ' 年报')} · {d.get('symbol')}{f' · 第 {pg} 页' if pg else ''} · {d.get('source') or ''}".rstrip(" ·"),
                                   d.get("page_url") or d.get("url") or "", str(d.get("as_of") or ""), tool=tool)
                    self.bind(d.get("values") or {}, sid)
                    r.append(f"[{sid}]")
                out["rows"] = rows
            return out
        if tool == "compare_filings" and isinstance(result, dict) and not result.get("error"):
            ya, yb = result.get("year_a"), result.get("year_b")
            out = dict(result)
            for key, yr in (("added", yb), ("removed", ya), ("modified", yb)):
                new = []
                for it in result.get(key) or []:
                    pg = it.get("page") or it.get("page_b")
                    when = result.get("as_of_a") if key == "removed" else result.get("as_of")
                    sid = self.new(f"{yr} 年报 · {result.get('symbol')} · 第 {pg} 页 · {result.get('source') or ''}".rstrip(" ·"), it.get("url") or "",
                                   str(when or ""), tool=tool)
                    self.bind(it.get("text") or it.get("after") or "", sid)
                    if key == "modified":
                        self.bind(it.get("before") or "", sid)
                    new.append({**it, "_cite": sid})
                out[key] = new
            sid = self.new(f"年报对比 · {result.get('symbol')} · {ya} vs {yb} · {result.get('section_name') or ''}", result.get("url") or "", str(result.get("as_of") or ""), tool=tool)
            self.bind({"counts": result.get("counts"), "ratio": result.get("unchanged_ratio")}, sid)
            out["_cite"] = sid
            return out
        if tool == "market_overview" and isinstance(result, dict):
            sid = self.new(f"指数与自选一览 · {result.get('source') or '腾讯证券 / 东方财富行情'}", result.get("url") or "", str(result.get("date") or result.get("as_of") or today()), tool=tool)
            self.bind(result, sid)
            return {**result, "_cite": sid}
        if tool == "fetch_url":
            url = args.get("url", "")
            sid = self.new(f"网页 · {(result or {}).get('title', '')[:40] if isinstance(result, dict) else ''}".rstrip(" ·"), url, today(), tool=tool)
            self.bind(result, sid)
            return {**result, "_cite": sid} if isinstance(result, dict) else result
        if tool == "earnings_report" and isinstance(result, dict) and not result.get("error"):
            # 研报里每行数据带 cite（研报自己的来源序号）→ 换成本轮的来源编号
            smap = {}
            for i, it in enumerate(result.get("sources") or [], 1):
                smap[i] = self.new(it.get("name") or "财报事件研报", it.get("url") or "", it.get("as_of") or "", tool=tool)
            rows = list(((result.get("vs_consensus") or {}).get("rows") or [])) + list(result.get("yoy") or []) + list(result.get("guidance") or [])
            if result.get("consensus"):
                rows.append(result["consensus"])
            for r in rows:
                if isinstance(r, dict) and r.get("cite") in smap:
                    self.bind({k: v for k, v in r.items() if k != "cite"}, smap[r["cite"]])
            sid = self.new(f"财报事件研报 · {result.get('report_id') or ''}".rstrip(" ·"), "", today(), kind="memory", tool=tool)
            self.bind({k: v for k, v in result.items() if k in ("vs_consensus", "thesis_verdict")}, sid)
            return {**result, "_cite": sid, "_cite_map": {str(k): v for k, v in smap.items()}}
        if tool == "deep_analysis":  # 辩论内部的数据调用已经登记过；这里只登记结论本身
            sid = self.new("Agora 辩论纪要", "", today(), kind="debate", tool=tool)
            self.bind({k: v for k, v in (result or {}).items() if k != "answer"} if isinstance(result, dict) else result, sid)
            return result
        names = {"memory_links": "你在 Stoa 的笔记与记忆（非市场数据）", "session_search": "你和 Stoa 的历史对话", "search_memory": "你在 Stoa 的笔记与记忆（非市场数据）",
                 "earnings_calendar": "财报日历（交易所/公司公告披露日）", "call_stats": "你的判断记录（Stoa 账本）",
                 "record_call": "你的判断记录（Stoa 账本）", "alert_list": "你设的提醒", "watchlist": "你的自选股"}
        sym = str((result.get("symbol") if isinstance(result, dict) else "") or (args or {}).get("symbol") or "")
        if tool == "calc":
            sid = self.new(calc_label(args, result), "", today(), tool=tool)
            if isinstance(result, dict):  # 结果 + 输入 + 算式里的常数都算这次计算的出处
                self.bind({"results": result.get("results"), "inputs": result.get("inputs")}, sid)
                self.bind(" ".join(str(v) for v in (result.get("expressions") or {}).values()), sid)
                return {**result, "_cite": sid} if "error" not in result else result
        elif tool == "run_python":
            sid = self.new("计算：沙箱 Python（代码与输出见本回答）", "", today(), tool=tool)
        elif tool in names:
            sid = self.new(names[tool], "", today(), kind="memory", tool=tool)
        else:
            title = TOOL_TITLES.get(tool) or ("外部工具 · " + tool.split("__")[1] if tool.startswith("mcp__") and tool.count("__") >= 2 else "数据")
            srcname = str(result.get("source") or "") if isinstance(result, dict) else ""
            name = " · ".join(x for x in (title, sym, srcname) if x)
            asof = str((result.get("as_of") or result.get("date") or "") if isinstance(result, dict) else "") or today()
            sid = self.new(name, source_url(tool, sym, result) if (srcname or sym) else "", asof, tool=tool)
        self.bind(result, sid)
        if isinstance(result, dict) and "error" not in result:
            return {**result, "_cite": sid}
        return result

    # -------------------------------------------------- 比对
    def quote_scope(self, body: str, pos: int) -> list[dict]:
        """只绑定同一句中最近明确出现的代码/名称；不猜代词、简称或跨段落主语。"""
        prefix = re.split(r"[。；;\n]|(?<!\d)\.(?=\s|$)", body[:pos])[-1]
        with self.lock:
            quotes = list(self.quotes) + list(self.financials)
        hits = []
        for q in quotes:
            for alias in q["aliases"]:
                pattern = r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])"
                hits.extend((m.end(), q["symbol"]) for m in re.finditer(pattern, prefix, re.I))
        if not hits:
            return []
        symbol = max(hits)[1]
        return [q for q in quotes if q["symbol"] == symbol]

    def financial_match(self, body: str, n: dict, scope: list[dict]) -> tuple[bool, list[int] | None]:
        prefix = re.split(r"[。；;\n]", body[:n["start"]])[-1]
        fields = {
            "市盈率TTM": (r"市盈率(?:TTM)?|\bPE\b", ("市盈率TTM", "pe")),
            "预期市盈率": (r"预期市盈率|预测市盈率", ("预期市盈率",)),
            "ROE%": (r"\bROE\b|净资产收益率", ("ROE%", "roe")),
            "营收": (r"营业总收入|营收|\brevenue\b", ("营业总收入", "Total Revenue", "revenue")),
            "营业收入": (r"营业收入", ("营业收入", "operating_revenue")),
            "净利润": (r"归母净利润|净利润|\bnet income\b", ("归母净利润", "Net Income", "net_profit")),
            "市净率": (r"市净率|\bPB\b", ("市净率",)),
            "市值": (r"市值|market cap", ("市值", "market_cap")),
            "每股收益": (r"每股收益|\bEPS\b", ("每股收益", "Diluted EPS", "eps")),
            "股息率%": (r"股息率|分红收益率", ("股息率%",)),
            "净利率%": (r"净利率", ("净利率%", "net_margin")),
            "毛利率%": (r"毛利率", ("毛利率%", "gross_margin")),
            "营收同比%": (r"营收同比|营业总收入同比|(?:营收|营业总收入)(?:同比)?(?:增长|下降|下滑|增速|减少)", ("营收同比%", "营业总收入同比%", "revenue_yoy")),
            "营业收入同比%": (r"营业收入同比|营业收入(?:同比)?(?:增长|下降|下滑|增速|减少)", ("营业收入同比%",)),
            "净利同比%": (r"净利同比|净利润同比", ("净利同比%", "盈利同比%", "net_profit_yoy")),
            "资产负债率%": (r"资产负债率", ("资产负债率%", "debt_ratio")),
            "自由现金流": (r"自由现金流", ("自由现金流",)),
            "现金": (r"现金余额|现金总额", ("现金",)),
            "总负债": (r"总负债|债务总额", ("总负债",)),
        }
        clause = re.split(r"[，,|]", prefix)[-1]  # 指标名要在同一小句里：「市盈率 18.6 倍，……还差 17.6」的 17.6 不是市盈率
        hits = [(m.end(), m.end() - m.start(), key) for key, (pattern, _) in fields.items()
                for m in re.finditer(pattern, clause, re.I)]
        if not hits or re.search(r"相比|相差|还差|假设|如果|若", clause):
            return False, None
        key = max(hits)[2]
        period = report_period(prefix)
        currency_text = body[n["end_num"]:n["end_num"] + 5]
        currency = next((v for word, v in (("美元", "USD"), ("港元", "HKD"), ("人民币", "CNY")) if word in currency_text), None)
        out = []
        rows = [row for row in scope if row.get("kind") == "financial" and not (period and row["period"] != period)
                and not (currency and row.get("currency") != currency)]
        cand = fields[key][1]
        # 「营业收入」与「营业总收入」按字面区分：本轮数据里有营业收入就只认营业收入；数据源只给了营业总收入（港美股、旧数据）才退回去比
        if key in FALLBACK and not any(row["data"].get(f) is not None for row in rows for f in cand):
            cand = cand + fields[FALLBACK[key]][1]
        for row in rows:
            for field in cand:
                raw = row["data"].get(field)
                if raw is None:
                    continue
                if isinstance(raw, str):
                    match = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*(万亿|亿|万|千)?\s*(?:人民币|港元|美元|元)?\s*", raw)
                    if not match:
                        continue
                    value = _scale(match[1], match[2])
                elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    value = float(raw) * (100 if field == "roe" and abs(raw) < 1 else 1)
                else:
                    continue
                tol = .5 * 10 ** -n["dec"] * n["scale"] + 1e-9
                if abs(n["value"] - value) <= max(abs(value) * .005, tol):
                    out.append(row["sid"])
        return True, list(dict.fromkeys(out)) or None

    def quote_match(self, body: str, n: dict, scope: list[dict]) -> tuple[bool, list[int] | None]:  # noqa: E301
        """明确的行情指标按字段核对；推导、建议和无法识别的表述仍走通用数字核对。"""
        prefix = re.split(r"[，。；,;\n|]", body[:n["start"]])[-1]
        if re.search(r"较|相比|高于|低于|还差|相差|浮盈|收益|目标|建议|假设|如果|若|\b(?:above|below|target|if)\b", prefix, re.I):
            return False, None
        fields = {
            "close": r"收盘(?:价)?|现价|\bclose\b",
            "chg_pct": r"单日涨跌幅|涨幅|跌幅|上涨|下跌|(?:今天|今日|当日|单日)\s*(?:大|暴|小|微)?(?:涨|跌)(?:了)?|\b(?:daily change|change)\b",
            "chg_5d": r"5\s*(?:日|天)涨跌幅",
            "chg_20d": r"20\s*(?:日|天)涨跌幅",
            "ma20": r"20\s*日(?:均线|线)|\bMA20\b",
            "ma60": r"60\s*日(?:均线|线)|\bMA60\b",
            "rsi": r"\bRSI\b",
            "high_52w": r"52\s*周(?:高点|最高(?:价)?)",
            "low_52w": r"52\s*周(?:低点|最低(?:价)?)",
        }
        hits = [(m.end(), m.end() - m.start(), field) for field, pattern in fields.items()
                for m in re.finditer(pattern, prefix, re.I)]
        if not hits:
            return False, None
        field = max(hits)[2]
        want = _want_sign(body, n)
        target = abs(n["value"]) * want if want else n["value"]
        tol = .5 * 10 ** -n["dec"] * n["scale"] + 1e-9
        sids = []
        for q in scope:
            value = q["data"].get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if abs(target - value) <= max(abs(value) * .005, tol):
                    sids.append(q["sid"])
        return True, list(dict.fromkeys(sids)) or None

    def same_day_move(self, body: str, n: dict) -> bool:
        """数字前同一小句里写着「今天/当日 涨/跌」：说的是某只标的的当日涨跌幅，不是推算出来的比例。"""
        prefix = re.split(r"[，。；,;\n|]", body[:n["start"]])[-1]
        if re.search(r"较|相比|高于|低于|距|离|还差|相差|浮盈|收益|目标|空间|假设|如果|若|\b(?:above|below|target|if)\b", prefix, re.I):
            return False
        return bool(SAME_DAY_MOVE.search(prefix[-12:]))

    def direction_conflict(self, value: float, dec: int, scale: float, want: int, sids: set[int] | None = None) -> bool:
        """回答说「涨 2.35%」而数据里同一个数是 -2.35（或反过来）：只有反号的直接匹配、没有同号的，算方向写反。"""
        tol = .5 * 10 ** -dec * scale + 1e-9
        av = abs(value)
        with self.lock:
            nums = [(v, s) for v, s in self.nums if sids is None or s in sids]
            snums = [(v, s) for v, s in self.snums if sids is None or s in sids]

        def hits(pairs):
            out = []
            for s, _ in pairs:
                for c in (s, s * 100 if abs(s) < 5 else None):
                    if c is not None and abs(av - abs(c)) <= max(abs(c) * .005, tol):
                        out.append(c)
            return out
        if any(c == 0 or (c > 0) == (want > 0) for c in hits(nums)):
            return False
        return any(c != 0 and (c > 0) != (want > 0) for c in hits(snums))

    def match(self, value: float, dec: int, scale: float = 1.0, pct: bool | None = None, sids: set[int] | None = None,
              derived: bool = True) -> list[int] | None:
        """value 能否由本轮数字得出。返回来源编号（可能两个），找不到返回 None。derived=False 只认直接出现的数。"""
        with self.lock:
            pairs = list(dict.fromkeys((v, s) for v, s in self.nums if sids is None or s in sids))
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
                    it = self.get(sid) or {}
                    direct.append((abs(av - abs(cand)) > 1e-9, rank.get(it.get("kind"), 4), it.get("tool") in LOW_PRIORITY, sid))
        if direct:
            return [min(direct)[-1]]
        if not derived:
            return None
        uniq = {}
        for s, sid in pairs:
            uniq.setdefault(s, sid)
        vals = list(uniq.items())[:90]
        for same in (True, False):  # 差、涨跌幅、乘积；先找同一来源里的两个数
            for a, sa in vals:
                for b, sb in vals:
                    if a == b or (sa == sb) != same:
                        continue
                    if close(a - b):
                        return sorted({sa, sb})
                    # 涨跌幅：只认带 % 的数字、两数量级相近；乘积：只认不带 % 的数字、其中一个是整数数量（股数）
                    if pct is not False and b and .25 < a / b < 4 and close((a / b - 1) * 100):
                        return sorted({sa, sb})
                    if not pct and float(b).is_integer() and abs(b) >= 10 and close(a * b):
                        return sorted({sa, sb})
        try:
            import numpy as np
        except ImportError:
            return None
        arr = np.array([v for v, _ in vals])
        diff = arr[:, None] - arr[None, :]
        if pct:
            return None
        for c, sc in vals:  # (a-b)×c：按持仓算浮盈（c 是整数股数）
            if not float(c).is_integer() or abs(c) < 10:
                continue
            prod = np.abs(diff * c)
            hit = np.argwhere(np.abs(av - prod) <= np.maximum(prod * .005, tol_abs))
            if len(hit):
                i, j = hit[0]
                return sorted({vals[i][1], vals[j][1], sc})
        return None


# ====================================================================== 回答侧
NO_NUMBERS = {"skill_view", "skills_list", "skill_manage", "memory", "alert_add", "alert_remove"}
LOW_PRIORITY = {"get_quotes", "market_overview"}  # 同一个数多处对得上时，行情一览最后考虑
UNIT_AFTER = re.compile(r"(?:港元|美元|人民币|元|千升|千克|吨|户|股|家|人次|人|辆|台|张|手)")


def _round(tok: str) -> bool:
    """有效数字不超过两位（7%、30%、1.1、0.8、89%）：多半是假设或阈值，凑巧和别处数字相等的概率高，不自动加角标。"""
    d = re.sub(r"[^\d]", "", tok).lstrip("0").rstrip("0")
    return len(d) <= 2


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
        if not has_dec and not unit and re.fullmatch(r"\d{5,6}", tok) and ((before and before in "（(") or (after and after in "）).") or tok.startswith("0")):
            continue  # 股票代码 600519、00700
        dec = len(tok.split(".")[1]) if has_dec else 0
        scale = UNIT.get(unit or "", 1) if unit not in ("%", "％") else 1
        out.append({"start": s, "end": e, "end_num": m.end(),
                    "text": masked[s:e], "value": v, "raw": float(tok.replace(",", "").replace("−", "-")), "dec": dec, "scale": scale})
    return out


UP_WORDS = re.compile(r"(?:上涨|涨了|涨幅|大涨|收涨|上升|增长|增加|提升|升至?|增|涨|流入|净买入|盈利)[^，。；,;\n\d]{0,3}$")
DOWN_WORDS = re.compile(r"(?:下跌|跌了|跌幅|大跌|收跌|下降|下滑|回落|减少|降至?|减|跌|流出|净卖出|亏损|亏)[^，。；,;\n\d]{0,3}$")
ANS_DATE_RE = re.compile(r"(20\d\d)\s?[-/年]\s?(\d{1,2})\s?[-/月]\s?(\d{1,2})\s?日?|(?<![\d年])(\d{1,2})\s?月\s?(\d{1,2})\s?日")


def _want_sign(body: str, n: dict) -> int:
    """数字自带 +/- 号，或紧挨着「涨/跌/增/减/流入/流出」这类词，返回期望符号；否则 0。"""
    tok = n["text"].lstrip()
    if tok[:1] in "-−":
        return -1
    if tok[:1] == "+":
        return 1
    pre = body[max(0, n["start"] - 8):n["start"]]
    if DOWN_WORDS.search(pre):
        return -1
    if UP_WORDS.search(pre):
        return 1
    return 0


def answer_dates(body: str) -> list[dict]:
    """回答里写到具体某天的日期（只认年月日或月日），带位置。月日没写年份时取离今天最近的那一年。"""
    now = datetime.now(CN_TZ).replace(tzinfo=None)
    out = []
    for m in ANS_DATE_RE.finditer(body):
        g = m.groups()
        try:
            if g[0]:
                d = datetime(int(g[0]), int(g[1]), int(g[2]))
            else:
                cands = []
                for y in (now.year - 1, now.year, now.year + 1):
                    try:
                        cands.append(datetime(y, int(g[3]), int(g[4])))
                    except ValueError:
                        pass
                d = min(cands, key=lambda x: abs((x - now).days))
        except (ValueError, TypeError):
            continue
        out.append({"start": m.start(), "end_num": m.end(), "text": m.group(0), "date": d})
    return out


def date_mismatches(body: str, src: Sources) -> list[dict]:
    """离数据日期很近（±10 天）却又不是任何一个数据日期的「某月某日」：多半是把收盘日写错了一天。今天的日期、远处的历史日期不管。"""
    with src.lock:
        ds = set(src.dates)
    if not ds:
        return []
    today_s = today()
    bad = []
    for a in answer_dates(body):
        scope = src.quote_scope(body, a["start"])
        scoped_dates = set().union(*(dates_in(q["data"].get("date", "")) for q in scope)) if scope else set()
        dates = scoped_dates or ds
        k = a["date"].strftime("%Y-%m-%d")
        if k in dates or (not scope and k == today_s):
            continue
        near = min(abs((a["date"] - datetime.strptime(x, "%Y-%m-%d")).days) for x in dates)
        if 0 < near <= 10:
            bad.append(a)
    return bad


PCT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s?[%％]")
ASSUME_RE = re.compile(r"假设|假定|设定|不妨设|例如|举例|示例|比如|例子|assum|suppose|for example|e\.g\.", re.I)
CODE_RE = re.compile(r"(?<![\d.])\d{4,6}\.(?:HK|SS|SZ|BJ|SH)\b|(?<![\d.])[036]\d{5}(?![\d.])", re.I)


def pct_decimals(text: str) -> list[float]:
    """「4%」→ 0.04：百分数写成小数也算同一个数（只用于题设与回答里写明的假设）。"""
    out = []
    for m in PCT_RE.finditer(text or ""):
        try:
            out.append(round(float(m.group(1)) / 100, 10))
        except ValueError:
            pass
    return out


def assumption_values(body: str) -> list[float]:
    """回答自己写明的假设（「假设：面值 100、票息 5、收益率 4%」及紧跟其后的列表/表格行）里的数字：是题设输入，不是事实陈述，
    不需要数据出处；推导时可作为输入。只认带假设词的行，以「：」结尾时连同下面的列表项（到空行或标题为止）。"""
    vals, lines, carry = [], body.split("\n"), False
    for ln in lines:
        st = ln.strip()
        if not st or st.startswith("#"):
            carry = False
            continue
        hit = bool(ASSUME_RE.search(st))
        if hit or (carry and re.match(r"^(?:[-*•|]|\d+[.、)])", st)):
            seg = CODE_RE.sub(" ", re.sub(r"\[\d+\]", " ", st))  # 股票代码不是假设数字
            vals += numbers_in(seg) + pct_decimals(seg)
            if hit:
                carry = st.rstrip("*").endswith(("：", ":"))
        else:
            carry = False
    return vals


def _overlay(src: "Sources", extra: list[tuple[float, int]], label: str = "") -> "Sources":
    """临时副本：额外数字（回答里的假设、前文已核实的数）参与推导，不改原登记处。假设用一个不在清单里的编号，不会生成角标。"""
    import copy
    s2 = copy.copy(src)
    s2.lock = threading.Lock()
    s2.items = list(src.items)
    s2.nums = list(src.nums) + list(extra)
    return s2


ASSUME_SID = 10 ** 6  # 「回答中写明的假设」：不是登记的来源，finalize 不会给它加角标


def verify(text: str, src: Sources) -> dict:
    """返回 {ok, numbers: [{text, value, sids}], unverified: [text...], why: {text: 原因}}。已标「未核实」的数字不再算错。
    三类错：数字找不到出处；数字对得上但涨跌方向写反；日期离数据日只差几天却对不上。"""
    body = strip_sources(text)
    nums, why = [], {}
    av = assumption_values(body)
    gsrc = _overlay(src, [(v, ASSUME_SID) for v in av]) if av else src  # 通用核对（非指定标的的数）可用回答里写明的假设
    for n in key_numbers(body):
        flagged = body[n["end_num"]:n["end_num"] + len(UNVERIFIED) + 4].lstrip(" [0123456789]").startswith(UNVERIFIED)
        p = n["text"].rstrip().endswith(("%", "％"))
        scope = src.quote_scope(body, n["start"])
        allowed = {q["sid"] for q in scope} if scope else None
        scoped, sids = src.financial_match(body, n, scope) if scope else (False, None)
        if scope and not scoped:
            scoped, sids = src.quote_match(body, n, scope)
        move = p and not scope and bool(src.quotes) and src.same_day_move(body, n)
        if move:  # 「今天大涨 8.4%」却没写是哪只：按本轮每只标的的当日涨跌幅核对，或者是某个数据里原样出现的数（指数涨跌），不认两数凑出的比例
            with src.lock:
                allq = list(src.quotes)
            scoped, sids = src.quote_match(body, n, allq)
            if not sids:
                scoped = True
                sids = src.match(n["value"], n["dec"], n["scale"], p, None, derived=False)
        if not scoped and not move:
            ms = gsrc if allowed is None else src
            sids = ms.match(n["value"], n["dec"], n["scale"], p, allowed) or (ms.match(n["raw"], n["dec"], 1, p, allowed) if n["scale"] != 1 else None)
        want = _want_sign(body, n)
        derived = bool(sids) and all(s == ASSUME_SID or (src.get(s) or {}).get("tool") in CALC_TOOLS for s in sids)
        if sids and want and not derived and src.direction_conflict(n["value"], n["dec"], n["scale"], want, allowed):
            why[n["text"]] = "涨跌方向和数据相反"
            sids = None
        elif not sids and not flagged:
            why[n["text"]] = "数字与该标的的指标、报告期或计价不符" if scoped else "找不到出处"
        nums.append({**n, "sids": sids, "flagged": flagged, "_scoped": bool(scope) or move, "_p": p, "_allowed": allowed})
    # 第二遍：由前文已核实的数再推一步（3 万持仓 = 1 万 × 3 已核实 → 亏损 6000 = 3 万 × 20%），只用于没绑定具体标的的数
    ok = [(x["value"], x["sids"][0]) for x in nums if x["sids"]]
    if ok and any(not x["sids"] and not x["flagged"] and not x["_scoped"] for x in nums):
        s3 = _overlay(gsrc, ok)
        for x in nums:
            if x["sids"] or x["flagged"] or x["_scoped"] or x["_allowed"] is not None:
                continue
            sids = s3.match(x["value"], x["dec"], x["scale"], x["_p"], None)
            if sids:
                x["sids"] = sids
                why.pop(x["text"], None)
    for x in nums:
        for k in ("_scoped", "_p", "_allowed"):
            x.pop(k, None)
    for d in date_mismatches(body, src):
        flagged = body[d["end_num"]:d["end_num"] + len(UNVERIFIED) + 4].lstrip(" [0123456789]").startswith(UNVERIFIED)
        nums.append({**d, "sids": None, "flagged": flagged, "date_only": True})
        if not flagged:
            why[d["text"]] = "日期和数据日期对不上（数据日期：" + "、".join(sorted(src.dates)[-3:]) + "）"
    bad = [n["text"] for n in nums if not n["sids"] and not n["flagged"]]
    return {"ok": not bad, "numbers": nums, "unverified": bad, "why": why}


def correction_prompt(bad: list[str], why: dict | None = None) -> str:
    why = why or {}
    items = [f"{b}（{why[b]}）" if why.get(b) and why[b] != "找不到出处" else b for b in dict.fromkeys(bad)]
    return ("[系统核对：你回答里的这些数字或日期和本轮工具结果对不上：" + "、".join(items) +
            "。请重写回答：找不到出处的要么调用工具取得出处，要么删掉或改成「查不到」；方向写反的按数据改正涨跌；日期以工具结果里的数据日期为准。"
            "不要估算或凭记忆写数字。关键数字后保留 [编号]。直接给出完整的新回答。]")


def flag(text: str, src: Sources) -> tuple[str, list[str]]:
    """修正后仍找不到出处的数字：在数字后标「（未核实）」。"""
    body, tail = split_sources(text)
    v = verify(body, src)
    bad = [n for n in v["numbers"] if not n["sids"] and not n["flagged"]]
    for n in sorted(bad, key=lambda x: -x["end_num"]):
        body = body[:n["end_num"]] + UNVERIFIED + body[n["end_num"]:]
    if bad:
        body += f"\n\n注：标「未核实」的 {len(bad)} 处数字或日期和本轮数据对不上，请勿据此决策。"
    return body + ("\n\n" + tail if tail else ""), [n["text"] for n in bad]


PAGE_RE = re.compile(r"第\s?(\d{1,4})\s?页")


def page_cites(body: str, src: Sources) -> str:
    """年报段落：回答里的「第 N 页」后面补上指向该页的来源角标（链接到 PDF#page=N）。"""
    pages = {}
    for it in src.items:
        if it.get("tool") in ("read_filing", "compare_filings"):
            m = PAGE_RE.search(it["name"])
            if m:
                yr = re.match(r"(\d{4}) 年报", it["name"])
                pages.setdefault(int(m.group(1)), []).append((yr.group(1) if yr else "", it["id"]))
    if not pages:
        return body
    out, last = [], 0
    for m in PAGE_RE.finditer(body):
        cand = pages.get(int(m.group(1)))
        if not cand:
            continue
        before = body[max(0, m.start() - 12):m.start()]
        yr = re.search(r"(20\d\d)\s?年", before)
        sid = next((i for y, i in cand if yr and y == yr.group(1)), cand[0][1])
        if re.match(r"\s?\[\d+\]", body[m.end():]) or re.match(r"[^\n]{0,3}\[\d+\]", body[m.end():m.end() + 4]):
            continue
        out.append(body[last:m.end()] + f"[{sid}]")
        last = m.end()
    return "".join(out) + body[last:]


TAG_DATA = re.compile(r"^(\s*(?:[-*•]|\d+[.、)])?\s*)(〔数据〕|\[Data\])", re.M)


def fix_tags(body: str) -> str:
    """〔数据〕标签也要被核对：那一行里没有 [n] 出处角标，就降成〔推断〕（英文 [Data] → [Inference]）。"""
    out = []
    for line in body.split("\n"):
        m = TAG_DATA.match(line)
        if m and not re.search(r"\[\d+\]", line[m.end():]):
            line = line[:m.start(2)] + ("〔推断〕" if m.group(2) == "〔数据〕" else "[Inference]") + line[m.end(2):]
        out.append(line)
    return "\n".join(out)


def finalize(text: str, src: Sources) -> tuple[str, list[dict]]:
    """补引用角标、清理无效角标、按出现顺序重新编号，末尾附来源清单。返回 (文本, 用到的来源)。"""
    body, _ = split_sources(text)
    # 模型自己写的 [n]：指向不存在的来源就删掉
    body = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if src.get(int(m.group(1))) else "", body)
    v = verify(body, src)
    for n in sorted(v["numbers"], key=lambda x: -x["end_num"]):
        if not n["sids"] or n.get("date_only") or n.get("flagged"):  # 已标「未核实」的数不再补角标
            continue
        sids = [s for s in n["sids"] if (src.get(s) or {}).get("kind") not in ("user", None)]
        if not sids:
            continue
        at = n["end_num"]
        if src.quote_scope(body, n["start"]):
            marker = re.match(r"(\s*(?:港元|美元|人民币|元|倍)?\s*)\[(\d+)\]", body[at:])
            if marker and int(marker.group(2)) not in sids:
                body = body[:at] + marker.group(1) + body[at + marker.end():]
        if _round(n["text"]):
            continue
        cu = UNIT_AFTER.match(body[at:at + 3]) if n["scale"] != 1 else None  # 「4.66 亿元」「235.4 万千升」角标放在单位后
        if cu:
            at += cu.end()
        bei = re.match(r"\s?倍(?!杠杆)", body[at:]) if n["scale"] == 1 and not n["text"].rstrip().endswith(("%", "％")) else None
        if bei:  # 「16.79 倍」角标放在「倍」后
            at += bei.end()
        rest = body[at:]
        if re.match(r"\s?\[\d+\]", rest) or re.match(r"\s?\[\d+\]", body[n["end_num"]:]):
            continue
        near = re.split(r"[，。；,;\n|、]", rest[:14])[0]
        if re.search(r"\[\d+\]", near):  # 同一短语末尾模型已经标过角标（如「16.79 倍[29]」）
            continue
        body = body[:at] + "".join(f"[{s}]" for s in sids[:2]) + rest
    body = page_cites(body, src)
    body = fix_tags(body)
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
