"""财报事件（v0.17）：跟踪持仓与自选股的财报日，财报前提醒，财报后自动生成事件研报。

跟踪谁：USER.md 里提到持有/持仓/成本的股票 + config.yaml watchlist（指数不跟）。
财报日：A股 东方财富「业绩预约披露」RPT_PUBLIC_BS_APPOIN（预约日、变更日、实际披露日、是否已披露）；
        美股/港股 Yahoo earnings dates（日期、EPS 一致预期、实际 EPS、超预期幅度）。存 data/events.json。
调度：复用 alerts.loop 的常驻线程，每轮 tick()：日历 12 小时刷新一次（EARNINGS_REFRESH_H）；
      财报前 N 天（earnings.remind_days，默认 3）推一条提醒，带市场预期与上次判断回顾；
      财报日起 5 天内一旦确认已发布（或已过财报日一天），生成事件研报 data/reports/events/<代码>-<日期>.md（+ 同名 .json 给网页），
      推送到已开的渠道并写进提醒事件（网页弹出、桌面窗口通知）。
研报内容：实际 vs 一致预期（美/港：季度 EPS；A股：前 N 季累计 EPS 占全年一致预期的完成度 vs 去年同期完成度）、关键指标同比、
          管理层指引（公告/新闻里的指引表述；有模型时请模型据此概括）、和用户持仓逻辑对照（USER.md 持仓、账本里该股的历史判断理由、
          最近一次 Agora 结论）——加强 / 削弱 / 被证伪 / 基本不变；可选跑一次 Agora 辩论（earnings.debate: true）。
所有数字带来源（名字 + 链接 + 截至），研报末尾列出。EARNINGS=off 关闭。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from .memory import Memory, home

CN = timezone(timedelta(hours=8))
LOCK = threading.Lock()
WINDOW_DAYS = 5          # 财报日之后几天内还会补生成研报
HOLD_RE = re.compile(r"持有|持仓|仓位|成本|买了|买入|重仓|建仓")
GUIDE_RE = re.compile(r"指引|展望|预计|预告|guidance|outlook|forecast|expects?", re.I)
RESULT_RE = re.compile(r"业绩|季度报告|年度报告|半年度报告|季报|年报|中报|results|10-Q|10-K|8-K|earnings", re.I)
VERDICTS = ("加强", "削弱", "被证伪", "基本不变")


def _now() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d")


def path():
    return home() / "data" / "events.json"


def reports_dir():
    d = home() / "data" / "reports" / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cfg() -> dict:
    try:
        from .agent import load_cfg
        c = load_cfg().get("earnings") or {}
    except Exception:
        c = {}
    return {"remind_days": int(os.getenv("EARNINGS_REMIND_DAYS") or c.get("remind_days") or 3),
            "debate": str(os.getenv("EARNINGS_DEBATE") or c.get("debate") or "").lower() in ("1", "true", "on", "yes"),
            "refresh_h": float(os.getenv("EARNINGS_REFRESH_H") or c.get("refresh_hours") or 12),
            "enabled": (os.getenv("EARNINGS") or str(c.get("enabled", "on"))).lower() not in ("off", "0", "false", "no")}


def load() -> dict:
    p = path()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            for k, v in (("items", {}), ("reminded", []), ("reported", {}), ("reports", [])):
                d.setdefault(k, v)
            return d
        except Exception:
            pass
    return {"updated": None, "items": {}, "reminded": [], "reported": {}, "reports": []}


def save(d: dict):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d["reports"] = d["reports"][-200:]
    d["reminded"] = d["reminded"][-500:]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def market(sym: str) -> str:
    s = sym.upper()
    return "A" if s.endswith((".SS", ".SZ", ".BJ")) else "HK" if s.endswith(".HK") else "US"


# ---------------------------------------------------------------- 跟踪谁
def _linker(mem):
    from . import graph
    return graph.Linker(graph._known(mem))


def tracked(mem: Memory) -> dict[str, dict]:
    """{symbol: {name, why: [持仓/自选]}}；指数（^ 开头、指数代码）不跟。"""
    out: dict[str, dict] = {}
    try:
        from .agent import load_cfg
        c = load_cfg()
        idx = {str(x["symbol"]).upper() for xs in (c.get("indices") or {}).values() for x in xs or []}
        for w in c.get("watchlist") or []:
            s = str(w["symbol"]).upper()
            if not s.startswith("^") and s not in idx:
                out.setdefault(s, {"name": w.get("name") or s, "why": []})["why"].append("自选")
    except Exception:
        idx = set()
    try:
        L = _linker(mem)
        for e in mem.entries("user"):
            from .memory import position_state
            if HOLD_RE.search(e) and position_state(e) == "current":
                for s in L.symbols(e):
                    if not s.startswith("^") and s not in idx:
                        it = out.setdefault(s, {"name": L.known.get(s, s), "why": []})
                        if "持仓" not in it["why"]:
                            it["why"].insert(0, "持仓")
    except Exception:
        pass
    return out


def thesis(mem: Memory, sym: str) -> dict:
    """用户对这只股票的持仓逻辑：USER.md 里提到它的条目、账本里对它的判断（含理由与对错）、最近一次 Agora 结论。"""
    try:
        L = _linker(mem)
        lines = [e for e in mem.entries("user") if sym in L.symbols(e)]
        notes = [e for e in mem.entries("memory") if sym in L.symbols(e)]
    except Exception:
        lines, notes = [], []
    calls = [c for c in mem.recent_calls(2000) if str(c["symbol"]).upper() == sym.upper()][-6:]
    deb = None
    try:
        from . import debate
        deb = next((d for d in debate.listing(200) if str(d.get("symbol") or "").upper() == sym.upper()), None)
    except Exception:
        pass
    holds = any(HOLD_RE.search(x) for x in lines)
    last_view = calls[-1]["view"] if calls else None
    norm = {"看涨": "看多", "看多": "看多", "看跌": "看空", "看空": "看空"}
    direction = norm.get(last_view, "中性") if last_view else ("看多" if holds else None)
    if deb and deb.get("view") in ("看多", "看空") and not calls:
        direction = deb["view"]
    return {"user": lines, "notes": notes, "calls": calls, "debate": deb, "holds": holds, "direction": direction}


# ---------------------------------------------------------------- 数据（可替换，测试用 fixture）
class Fetch:
    """所有联网取数集中在这里；测试时换成读 fixture 的子类。每个方法失败返回 None / [] 并带 error。"""

    def calendar(self, sym: str) -> dict:
        return calendar_a(sym) if market(sym) == "A" else calendar_yahoo(sym)

    def financials(self, sym: str) -> dict:
        from . import research
        return research.financials(sym)

    def quarterly(self, sym: str) -> list[dict]:
        """最近 5 个季度利润表（新→旧），算同比用。"""
        import yfinance as yf
        q = yf.Ticker(sym).quarterly_income_stmt
        if q is None or q.empty:
            return []
        rows = [r for r in ("Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Diluted EPS") if r in q.index]
        out = []
        for c in list(q.columns)[:5]:
            out.append({"period": str(c.date()), **{r: (None if q.at[r, c] is None or (isinstance(q.at[r, c], float) and math.isnan(q.at[r, c])) else float(q.at[r, c])) for r in rows}})
        return out

    def consensus(self, sym: str) -> dict:
        from . import marketdata
        return marketdata.consensus(sym)

    def announcements(self, sym: str) -> list[dict]:
        from . import research
        return (research.announcements(sym, limit=15) or {}).get("announcements") or []

    def news(self, sym: str) -> list[dict]:
        from . import research
        return (research.news(sym, 10) or {}).get("news") or []


def _req(url, **kw):
    import requests
    kw.setdefault("timeout", 20)
    kw.setdefault("headers", {"User-Agent": "Mozilla/5.0 Stoa"})
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r


def calendar_a(sym: str) -> dict:
    """东方财富 业绩预约披露：每个报告期一行（预约日 → 变更日 → 实际披露日）。"""
    code = sym.split(".")[0]
    url = "https://data.eastmoney.com/bbsj/yysj.html"
    r = _req("https://datacenter-web.eastmoney.com/api/data/v1/get",
             params={"reportName": "RPT_PUBLIC_BS_APPOIN", "columns": "ALL", "filter": f'(SECURITY_CODE="{code}")',
                     "sortColumns": "REPORT_DATE", "sortTypes": "-1", "pageSize": 6})
    rows = ((r.json() or {}).get("result") or {}).get("data") or []
    events = []
    for x in rows:
        d = x.get("ACTUAL_PUBLISH_DATE") or x.get("THIRD_CHANGE_DATE") or x.get("SECOND_CHANGE_DATE") or x.get("FIRST_CHANGE_DATE") \
            or x.get("APPOINT_PUBLISH_DATE") or x.get("FIRST_APPOINT_DATE")
        if not d:
            continue
        events.append({"date": str(d)[:10], "period": (x.get("REPORT_TYPE_NAME") or "").replace(" ", ""), "report_date": str(x.get("REPORT_DATE") or "")[:10],
                       "published": str(x.get("IS_PUBLISH")) == "1" or bool(x.get("ACTUAL_PUBLISH_DATE")),
                       "changed": bool(x.get("FIRST_CHANGE_DATE")), "name": x.get("SECURITY_NAME_ABBR")})
    return {"symbol": sym, "events": events, "source": "东方财富 · 业绩预约披露", "url": url, "as_of": _now()}


def calendar_yahoo(sym: str) -> dict:
    import yfinance as yf
    t = yf.Ticker(sym)
    df = t.get_earnings_dates(limit=8)
    events = []
    if df is not None and not df.empty:
        for idx, row in df.iterrows():
            def f(k):
                v = row.get(k)
                try:
                    v = float(v)
                    return None if math.isnan(v) else round(v, 4)
                except (TypeError, ValueError):
                    return None
            # 财报时间按交易所时区给出；日期取当地日期（美股盘后发布 → 北京时间次日早上）
            events.append({"date": str(idx.date()), "time_local": idx.strftime("%H:%M %Z"), "period": "季度财报",
                           "eps_est": f("EPS Estimate"), "eps_actual": f("Reported EPS"), "surprise_pct": f("Surprise(%)"),
                           "published": f("Reported EPS") is not None})
    extra = {}
    try:
        c = t.calendar or {}
        extra = {"revenue_est": c.get("Revenue Average"), "eps_est_range": [c.get("Earnings Low"), c.get("Earnings High")]}
    except Exception:
        pass
    events.sort(key=lambda e: e["date"], reverse=True)
    return {"symbol": sym, "events": events, "source": "Yahoo Finance · Earnings", "url": f"https://finance.yahoo.com/calendar/earnings?symbol={sym}",
            "as_of": _now(), **extra}


# ---------------------------------------------------------------- 日历
def refresh(mem: Memory | None = None, fetch: Fetch | None = None, force: bool = False) -> dict:
    mem = mem or Memory()
    fetch = fetch or Fetch()
    c = cfg()
    with LOCK:
        d = load()
    if not force and d.get("updated"):
        try:
            age = datetime.now(CN) - datetime.strptime(d["updated"], "%Y-%m-%d %H:%M").replace(tzinfo=CN)
            if age < timedelta(hours=c["refresh_h"]) and set(d["items"]) >= set(tracked(mem)):
                return d
        except Exception:
            pass
    tr = tracked(mem)
    items = {}
    today = _today()
    for sym, info in tr.items():
        it = {"symbol": sym, "name": info["name"], "why": info["why"], "market": market(sym), "checked": _now()}
        try:
            cal = fetch.calendar(sym)
            evs = cal.get("events") or []
            it.update(source=cal.get("source"), url=cal.get("url"), as_of=cal.get("as_of"))
            if evs and evs[0].get("name") and it["name"] == sym:
                it["name"] = evs[0]["name"]
            fut = sorted([e for e in evs if e["date"] >= today], key=lambda e: e["date"])
            past = sorted([e for e in evs if e["date"] < today], key=lambda e: e["date"], reverse=True)
            it["next"] = fut[0] if fut else None
            it["last"] = past[0] if past else None
            it["events"] = (fut[:1] + past[:3])
            for k in ("revenue_est", "eps_est_range"):
                if cal.get(k) is not None:
                    it[k] = cal[k]
        except Exception as e:
            it["error"] = f"财报日取不到：{str(e)[:100]}"
            old = d["items"].get(sym) or {}
            for k in ("next", "last", "events", "source", "url", "as_of"):
                if old.get(k):
                    it[k] = old[k]
        items[sym] = it
    with LOCK:
        cur = load()
        cur["items"] = items
        cur["updated"] = _now()
        save(cur)
    return cur


def calendar_view(mem: Memory | None = None, refresh_now: bool = False, fetch: Fetch | None = None) -> dict:
    """给 agent / 网页：按财报日排序的跟踪列表 + 最近的事件研报。"""
    mem = mem or Memory()
    d = refresh(mem, fetch, force=refresh_now)
    today = datetime.now(CN).date()
    rows = []
    for it in d["items"].values():
        n = it.get("next")
        days = (datetime.strptime(n["date"], "%Y-%m-%d").date() - today).days if n else None
        rows.append({"symbol": it["symbol"], "name": it["name"], "why": "、".join(it.get("why") or []), "next_date": n and n["date"],
                     "days_left": days, "period": n and n.get("period"), "eps_est": n and n.get("eps_est"),
                     "last_date": (it.get("last") or {}).get("date"), "last_surprise_pct": (it.get("last") or {}).get("surprise_pct"),
                     "source": it.get("source"), "url": it.get("url"), "as_of": it.get("as_of"), "error": it.get("error")})
    rows.sort(key=lambda r: (r["next_date"] is None, r["next_date"] or ""))
    return {"tracked": rows, "reports": d["reports"][-10:][::-1], "updated": d.get("updated"),
            "note": "跟踪 USER.md 里的持仓 + 自选股；财报前提醒天数见 config.yaml earnings.remind_days" if rows else
                    "还没有可跟踪的股票：在 USER.md 记下持仓（如「持有腾讯 0700.HK 200 股」）或把股票加进自选。"}


# ---------------------------------------------------------------- 研报
def _num(v):
    try:
        v = float(v)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _big(v) -> str:
    if isinstance(v, str) and v.strip():
        return v  # research.financials 已格式化成「891.23亿」
    v = _num(v)
    if v is None:
        return "—"
    a = abs(v)
    return f"{v / 1e8:.2f} 亿" if a >= 1e8 else f"{v / 1e4:.2f} 万" if a >= 1e4 else f"{v:g}"


def _pct(a, b):
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100 * (1 if b > 0 else -1), 2)


class _Src:
    def __init__(self):
        self.items = []

    def add(self, name, url="", as_of="") -> int:
        for i, it in enumerate(self.items, 1):
            if it["name"] == name and it["url"] == (url or ""):
                return i
        self.items.append({"name": name, "url": url or "", "as_of": as_of or ""})
        return len(self.items)


def vs_consensus(sym: str, event: dict, fin: dict, cons: dict, src: _Src) -> dict:
    """实际 vs 一致预期。美/港：季度 EPS（Yahoo）；A股：累计 EPS 占全年一致预期的完成度，对比去年同期完成度。"""
    out = {"rows": [], "verdict": None, "method": ""}
    if event.get("eps_actual") is not None and event.get("eps_est") is not None:
        s = src.add(f"Yahoo Finance · {sym} 财报 EPS", f"https://finance.yahoo.com/quote/{sym}/analysis", event.get("date"))
        sp = event.get("surprise_pct")
        if sp is None:
            sp = _pct(event["eps_actual"], event["eps_est"])
        out["rows"].append({"指标": "季度 EPS", "实际": event["eps_actual"], "一致预期": event["eps_est"], "差异%": sp, "cite": s})
        out["method"] = "季度 EPS 实际值 vs 发布前的分析师一致预期（Yahoo）"
        out["surprise_pct"] = sp
        out["verdict"] = "超预期" if sp is not None and sp > 2 else "不及预期" if sp is not None and sp < -2 else "符合预期"
        return out
    mi = (fin or {}).get("main_indicators") or []
    eps_y = [e for e in (cons or {}).get("eps") or [] if e.get("kind") == "预测" or e.get("avg") is not None]
    if market(sym) == "A" and mi and eps_y:
        cur = mi[0]
        per = str(cur.get("报告期") or "")
        m = re.match(r"(\d{4})(.*)", per)
        if m:
            y, suf = int(m.group(1)), m.group(2)
            ly = next((x for x in mi if str(x.get("报告期")) == f"{y - 1}{suf}"), None)
            ly_annual = next((x for x in mi if str(x.get("报告期")) in (f"{y - 1}年报", f"{y - 1}年度")), None)
            fy = next((e for e in eps_y if int(e.get("year") or 0) == y), None)
            s_f = src.add(f"{(fin or {}).get('source') or '东方财富 F10'} · {sym} 主要指标", (fin or {}).get("url") or f"https://emweb.securities.eastmoney.com/PC_HSF10/FinanceAnalysis/Index?code={sym}", per)
            s_c = src.add(f"{cons.get('source') or '一致预期'} · {sym}", cons.get("url") or "", cons.get("as_of") or "")
            if fy and _num(cur.get("每股收益")) is not None and _num(fy.get("eps")):
                done = round(_num(cur["每股收益"]) / _num(fy["eps"]) * 100, 1)
                out["rows"].append({"指标": f"{per} 累计 EPS / {y} 全年一致预期 EPS", "实际": cur["每股收益"], "一致预期": fy["eps"], "完成度%": done, "cite": s_c})
                out["completion"] = done
                if ly and ly_annual and _num(ly.get("每股收益")) and _num(ly_annual.get("每股收益")):
                    last = round(_num(ly["每股收益"]) / _num(ly_annual["每股收益"]) * 100, 1)
                    out["rows"].append({"指标": f"去年同期完成度（{y - 1}{suf} / {y - 1} 全年）", "实际": ly["每股收益"], "一致预期": ly_annual["每股收益"], "完成度%": last, "cite": s_f})
                    out["completion_last"] = last
                    out["verdict"] = "超预期" if done > last + 3 else "不及预期" if done < last - 3 else "符合预期"
                    out["surprise_pct"] = round(done - last, 1)
                out["method"] = "A股按「累计 EPS 占全年一致预期的完成度」对比去年同期完成度（±3 个百分点内算符合）"
    if not out["rows"]:
        out["method"] = "没取到可比的一致预期（实际值或预期缺失），这一节只能留空"
    return out


def yoy(sym: str, fin: dict, quarterly: list[dict], src: _Src) -> list[dict]:
    rows = []
    mi = (fin or {}).get("main_indicators") or []
    if market(sym) == "A" and mi:
        cur = mi[0]
        s = src.add(f"{(fin or {}).get('source') or '东方财富 F10'} · {sym} 主要指标", (fin or {}).get("url") or f"https://emweb.securities.eastmoney.com/PC_HSF10/FinanceAnalysis/Index?code={sym}", str(cur.get("报告期") or ""))
        for k, yk in (("营业总收入", "营收同比%"), ("归母净利润", "净利同比%"), ("扣非净利润", None), ("毛利率%", None), ("每股收益", None)):
            if cur.get(k) is not None:
                rows.append({"指标": k, "本期": cur[k] if "%" in k or k == "每股收益" else _big(cur[k]), "同比%": cur.get(yk) if yk else None, "期": cur.get("报告期"), "cite": s})
        return rows
    if len(quarterly) >= 5:
        a, b = quarterly[0], quarterly[4]
        s = src.add(f"Yahoo Finance · {sym} 季度利润表", f"https://finance.yahoo.com/quote/{sym}/financials", a["period"])
        for k, label in (("Total Revenue", "营收"), ("Gross Profit", "毛利"), ("Operating Income", "经营利润"), ("Net Income", "净利润"), ("Diluted EPS", "摊薄 EPS")):
            if a.get(k) is not None:
                rows.append({"指标": label, "本期": a[k] if k == "Diluted EPS" else _big(a[k]), "同比%": _pct(a.get(k), b.get(k)), "期": a["period"], "cite": s})
    return rows


def guidance(anns: list[dict], news: list[dict], event_date: str, src: _Src) -> list[dict]:
    """公告/新闻里提到指引、展望的条目（财报日前 3 天以后的）。"""
    out = []
    lo = (datetime.strptime(event_date, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d") if event_date else ""
    for kind, items in (("公告", anns), ("新闻", news)):
        for it in items or []:
            t = str(it.get("title") or "")
            when = str(it.get("date") or it.get("time") or "")[:10]
            if when and lo and when < lo:
                continue
            if GUIDE_RE.search(t) or (kind == "公告" and RESULT_RE.search(t)):
                s = src.add(f"{it.get('source') or kind} · {t[:40]}", it.get("url") or "", when)
                out.append({"kind": kind, "title": t, "date": when, "url": it.get("url"), "guidance": bool(GUIDE_RE.search(t)), "cite": s})
    return out[:8]


def judge_thesis(th: dict, vc: dict, growth: list[dict]) -> dict:
    """规则判断持仓逻辑：超/不及预期 ±1，营收与利润同比方向 ±1；看多逻辑下两项都差且利润大幅下滑算「被证伪」。"""
    if not th.get("direction") or th["direction"] == "中性":
        return {"verdict": "基本不变" if th.get("calls") or th.get("user") else None,
                "reason": "没有明确的看多/看空逻辑可对照" if not (th.get("calls") or th.get("user")) else "过去的判断是震荡/中性，这次财报不改变方向性结论",
                "checks": []}
    score, checks = 0, []
    v = vc.get("verdict")
    if v == "超预期":
        score += 1; checks.append(f"业绩{v}（{vc.get('surprise_pct'):+}{'%' if vc.get('completion') is None else ' 个百分点'}）" if vc.get("surprise_pct") is not None else f"业绩{v}")
    elif v == "不及预期":
        score -= 1; checks.append(f"业绩{v}（{vc.get('surprise_pct'):+}{'%' if vc.get('completion') is None else ' 个百分点'}）" if vc.get("surprise_pct") is not None else f"业绩{v}")
    g = {r["指标"]: _num(r.get("同比%")) for r in growth if _num(r.get("同比%")) is not None}
    rev = next((g[k] for k in ("营业总收入", "营收") if k in g), None)
    prof = next((g[k] for k in ("归母净利润", "净利润") if k in g), None)
    for label, val in (("营收同比", rev), ("利润同比", prof)):
        if val is None:
            continue
        checks.append(f"{label} {val:+.2f}%")
        score += 1 if val > 3 else -1 if val < -3 else 0
    premise = []
    for c in th.get("calls") or []:
        rs = c.get("reason") or ""
        if re.search(r"增长|增速|需求|订单|放量|复苏|景气|growth|demand", rs, re.I) and rev is not None:
            premise.append(f"{c['ts'][:10]} 判断的理由「{rs[:40]}」{'得到营收增长的印证' if rev > 3 else '与营收同比下滑相矛盾' if rev < -3 else '营收持平，尚无定论'}")
    sign = 1 if th["direction"] == "看多" else -1
    s = score * sign
    strong_bad = (prof is not None and prof * sign < -20) and v == ("不及预期" if sign > 0 else "超预期")
    verdict = "被证伪" if strong_bad and s <= -2 else "削弱" if s < 0 else "加强" if s >= 1 else "基本不变"
    reason = f"你的逻辑偏{th['direction']}；" + ("、".join(checks) if checks else "缺少可对照的数据") + f"，合计 {s:+d} 分 → {verdict}"
    return {"verdict": verdict, "reason": reason, "checks": checks, "premise": premise, "score": s}


def _llm_ready(llm) -> bool:
    return llm is not None and getattr(llm, "ready", False) and repr(llm) != "mock"


def llm_layer(llm, facts: dict) -> dict | None:
    """有模型时：只基于给定数据写一句话结论、指引概括、逻辑判断理由。数字必须来自 facts，否则丢弃用规则版。"""
    if not _llm_ready(llm):
        return None
    prompt = ("你是严谨的卖方分析师。只根据下面 JSON 里的数据，为用户写财报事件点评，输出 JSON："
              '{"headline": "一句话结论（≤40字）", "guidance": "管理层指引变化概括（只用 guidance 条目的标题内容，没有就写「未见指引表述」）", '
              '"thesis_verdict": "加强|削弱|被证伪|基本不变", "thesis_reason": "≤120字，对照用户的持仓逻辑", "watch": ["接下来盯的 2-3 件事"]}。'
              "不得出现 JSON 里没有的数字；规则判断 rule_thesis 只能改成更保守的结论。\n\n" + json.dumps(facts, ensure_ascii=False, default=str)[:9000])
    try:
        t = llm.chat([{"role": "user", "content": prompt}], None, temperature=0.1, timeout=90).get("content") or ""
        j = json.loads(re.search(r"\{.*\}", t, re.S).group(0))
    except Exception:
        return None
    if j.get("thesis_verdict") not in VERDICTS:
        return None
    from .cite import Sources, verify
    src = Sources()
    sid = src.new("facts")
    src.bind(facts, sid)
    txt = " ".join(str(j.get(k) or "") for k in ("headline", "guidance", "thesis_reason")) + " " + " ".join(map(str, j.get("watch") or []))
    if not verify(txt, src)["ok"]:
        return None
    return j


def make_report(sym: str, mem: Memory | None = None, event: dict | None = None, fetch: Fetch | None = None, llm=None,
                with_debate: bool = False, name: str | None = None, push=None) -> dict:
    """生成事件研报，写 md + json，返回摘要。event 缺省时取最近一次已过的财报日。"""
    mem = mem or Memory()
    fetch = fetch or Fetch()
    sym = sym.strip().upper()
    src = _Src()
    errors = []

    def safe(fn, *a, default=None):
        try:
            return fn(*a)
        except Exception as e:
            errors.append(f"{getattr(fn, '__name__', 'fetch')}：{str(e)[:80]}")
            return default

    cal = safe(fetch.calendar, sym, default={}) or {}
    evs = cal.get("events") or []
    if event is None:
        past = sorted([e for e in evs if e["date"] <= _today()], key=lambda e: e["date"], reverse=True)
        event = past[0] if past else (evs[0] if evs else {"date": _today(), "period": "最近一期财报"})
    if cal.get("source"):
        src.add(f"{cal['source']} · {sym}", cal.get("url") or "", cal.get("as_of") or "")
    fin = safe(fetch.financials, sym, default={}) or {}
    qtr = safe(fetch.quarterly, sym, default=[]) if market(sym) != "A" else []
    cons = safe(fetch.consensus, sym, default={}) or {}
    anns = safe(fetch.announcements, sym, default=[]) or []
    news = safe(fetch.news, sym, default=[]) or []
    tn = tracked(mem).get(sym, {}).get("name")
    name = name or (tn if tn and tn != sym else None) or next((e.get("name") for e in evs if e.get("name")), None) or fin.get("name") or sym
    vc = vs_consensus(sym, event, fin, cons, src)
    growth = yoy(sym, fin, qtr or [], src)
    guide = guidance(anns, news, event.get("date") or "", src)
    if (event.get("period") in (None, "", "季度财报", "最近一期财报")) and growth and growth[0].get("期"):
        event = {**event, "period": f"{growth[0]['期']} 季度"}
    th = thesis(mem, sym)
    tj = judge_thesis(th, vc, growth)
    target = (cons or {}).get("target") or {}
    cons_view = None
    if target or cons.get("ratings"):
        s = src.add(f"{cons.get('source') or '一致预期'} · {sym}", cons.get("url") or "", cons.get("as_of") or "")
        cons_view = {"target": target, "ratings": cons.get("ratings"), "upside_pct": cons.get("upside_pct"), "price_now": cons.get("price_now"), "cite": s,
                     "warnings": cons.get("warnings") or []}
    facts = {"symbol": sym, "name": name, "event": event, "vs_consensus": vc, "yoy": growth, "guidance": guide, "consensus": cons_view,
             "thesis": {"user": th["user"], "calls": [{k: c.get(k) for k in ("ts", "view", "reason", "verdict", "price")} for c in th["calls"]],
                        "debate": th["debate"], "direction": th["direction"]}, "rule_thesis": tj}
    lj = llm_layer(llm, facts)
    deb = None
    if with_debate:
        deb = _run_debate(sym, mem)
    verdict = tj.get("verdict")
    if lj and verdict:
        # 模型只能更保守：加强 → 基本不变/削弱 可以，削弱 → 加强 不行
        rank = {"加强": 0, "基本不变": 1, "削弱": 2, "被证伪": 3}
        if rank[lj["thesis_verdict"]] >= rank[verdict]:
            verdict = lj["thesis_verdict"]
    headline = (lj or {}).get("headline") or _rule_headline(name, event, vc, growth, verdict)
    rid = f"{re.sub(r'[^A-Za-z0-9.]', '', sym)}-{event.get('date') or _today()}"
    rec = {"id": rid, "symbol": sym, "name": name, "date": event.get("date"), "period": event.get("period"), "created": _now(),
           "headline": headline, "beat": vc.get("verdict"), "verdict": verdict, "thesis_reason": (lj or {}).get("thesis_reason") or tj.get("reason"),
           "guidance_text": (lj or {}).get("guidance"), "watch": (lj or {}).get("watch") or _rule_watch(event, cons_view, tj),
           "facts": facts, "sources": src.items, "errors": errors, "debate": deb, "mode": "llm" if lj else "rules"}
    md = render(rec)
    d = reports_dir()
    (d / f"{rid}.md").write_text(md, encoding="utf-8")
    (d / f"{rid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    summary = {k: rec[k] for k in ("id", "symbol", "name", "date", "period", "created", "headline", "beat", "verdict", "mode")}
    with LOCK:
        st = load()
        st["reports"] = [r for r in st["reports"] if r.get("id") != rid] + [summary]
        st["reported"][f"{sym}|{event.get('date')}"] = rid
        save(st)
    try:
        from . import vectors  # 研报结论也进语义记忆
        mem.db.execute("SELECT 1")
        vectors.ensure(mem.db)
        with vectors.LOCK:
            vectors._upsert(mem.db, "memory", f"event:{rid}", f"[{event.get('date')}] {name} {sym} 财报事件研报：{headline}；持仓逻辑{verdict or '无可对照'}。",
                            event.get("date") or "", "", {"report": rid}, vectors.Concepts(mem))
            mem.db.commit()
    except Exception:
        pass
    if push is not False:
        _announce(rec, push)
    return {**summary, "markdown": md, "path": str(d / f"{rid}.md")}


def _rule_headline(name, event, vc, growth, verdict) -> str:
    g = next((r for r in growth if r["指标"] in ("营收", "营业总收入") and r.get("同比%") is not None), None)
    parts = [f"{name} {event.get('period') or '财报'}"]
    if vc.get("verdict"):
        parts.append(vc["verdict"])
    if g:
        parts.append(f"营收同比 {g['同比%']:+.2f}%")
    if verdict:
        parts.append(f"持仓逻辑{verdict}")
    return "，".join(parts)


def _rule_watch(event, cons_view, tj) -> list[str]:
    w = ["财报电话会 / 业绩说明会里管理层对下一季度的指引"]
    if cons_view and cons_view.get("target"):
        w.append("分析师是否在财报后上调或下调目标价（一致预期会滞后几天更新）")
    if tj.get("verdict") in ("削弱", "被证伪"):
        w.append("你的持仓逻辑受到挑战：复核仓位与止损，必要时跑一次深度分析")
    return w


def _run_debate(sym: str, mem: Memory) -> dict | None:
    try:
        from . import cite, debate
        from .agent import Agent
        a = Agent(mem, channel="earnings")
        a.sources = cite.Sources()
        a.turn_cards = []
        rec = debate.run_for_agent(a, sym, f"{sym} 刚发布财报，结合财报重新评估")
        v = rec.get("verdict") or {}
        return {"id": rec["id"], "view": v.get("view"), "confidence": v.get("confidence"), "summary": v.get("summary")}
    except Exception as e:
        return {"error": str(e)[:120]}


def render(rec: dict) -> str:
    f = rec["facts"]
    ev, vc = f["event"], f["vs_consensus"]

    def c(n):
        return f"[{n}]" if n else ""

    L = [f"# {rec['name']} {rec['symbol']} · {rec.get('period') or '财报'} 事件研报", "",
         f"> 财报日 {rec.get('date')}" + (f"（{ev.get('time_local')}）" if ev.get("time_local") else "") + f" · 生成于 {rec['created']} 北京时间 · "
         + ("模型撰写" if rec["mode"] == "llm" else "规则生成（未配置大模型）") + " · 仅供参考，不构成投资建议", "",
         "## 一句话", "", rec["headline"], ""]
    L += ["## I · 实际 vs 一致预期", ""]
    if vc["rows"]:
        L += ["| 指标 | 实际 | 一致预期 | 差异 |", "|---|---|---|---|"]
        for r in vc["rows"]:
            diff = f"{r['差异%']:+}%" if r.get("差异%") is not None else f"完成度 {r['完成度%']}%" if r.get("完成度%") is not None else "—"
            L.append(f"| {r['指标']} | {r['实际']}{c(r.get('cite'))} | {r['一致预期']}{c(r.get('cite'))} | {diff} |")
        L += ["", f"结论：**{vc.get('verdict') or '无法判断'}**。口径：{vc['method']}。", ""]
    else:
        L += [vc["method"] + "。", ""]
    cv = f.get("consensus")
    if cv:
        t = cv.get("target") or {}
        rt = "、".join(f"{k} {v}" for k, v in (cv.get("ratings") or {}).items() if v)
        L += [f"当前市场预期（取数时）：目标价 {t.get('low', '—')}–{t.get('high', '—')}" + (f"，均值 {t['mean']}" if t.get("mean") else "")
              + (f"，相对现价 {cv.get('price_now')} 空间 {cv['upside_pct']}%" if cv.get("upside_pct") is not None else "") + c(cv.get("cite"))
              + (f"；评级 {rt}" if rt else "") + "。", ""]
        for w in cv.get("warnings") or []:
            L += [f"注意：{w}", ""]
    L += ["## II · 关键指标同比", ""]
    if f["yoy"]:
        L += ["| 指标 | 本期 | 同比 |", "|---|---|---|"]
        for r in f["yoy"]:
            L.append(f"| {r['指标']}（{r.get('期')}） | {r['本期']}{c(r.get('cite'))} | {('%+.2f%%' % r['同比%']) if _num(r.get('同比%')) is not None else '—'} |")
        L.append("")
    else:
        L += ["没取到本期利润表数据。", ""]
    L += ["## III · 管理层指引", ""]
    if rec.get("guidance_text"):
        L += [rec["guidance_text"], ""]
    gd = f["guidance"]
    if gd:
        L += [f"- {g['date']} {g['kind']}：{g['title']}{c(g.get('cite'))}" + ("（含指引/展望表述）" if g["guidance"] else "") for g in gd] + [""]
        if not rec.get("guidance_text"):
            L += ["规则模式没有读原文，上面列出财报前后提到业绩、指引或展望的公告与新闻，指引是否变化请点开原文核对。", ""]
    else:
        L += ["财报前后的公告和新闻里没找到指引或展望的表述。", ""]
    th = f["thesis"]
    L += ["## IV · 与你的持仓逻辑对照", "", f"结论：**{rec.get('verdict') or '无持仓逻辑可对照'}**。{rec.get('thesis_reason') or ''}", ""]
    for u in th["user"]:
        L.append(f"- 你的记录：{u}")
    for cl in th["calls"]:
        L.append(f"- 过去的判断：{str(cl.get('ts'))[:10]} {cl.get('view')}（理由：{cl.get('reason')}）" + (f" → 复盘 {cl['verdict']}" if cl.get("verdict") else "（未到期）"))
    if th.get("debate"):
        L.append(f"- 最近一次 Agora：{th['debate'].get('created')} {th['debate'].get('view')}，置信度 {th['debate'].get('confidence')}")
    for p in (f["rule_thesis"].get("premise") or []):
        L.append(f"- {p}")
    if not (th["user"] or th["calls"] or th.get("debate")):
        L.append("- USER.md 和判断账本里还没有这只股票的持仓逻辑。")
    L.append("")
    if rec.get("debate"):
        d = rec["debate"]
        L += ["## Agora · 财报后辩论", "", (f"{d.get('view')}，置信度 {d.get('confidence')}：{d.get('summary')} [[debate:{d['id']}]]" if d.get("id") else f"辩论没跑成：{d.get('error')}"), ""]
    L += ["## V · 接下来盯什么", ""] + [f"- {w}" for w in rec.get("watch") or []] + [""]
    if rec.get("errors"):
        L += ["## 数据缺口", ""] + [f"- {e}" for e in rec["errors"]] + [""]
    L += ["—— 来源 ——"] + [f"[{i}] " + " · ".join(x for x in (s["name"], f"截至 {s['as_of']}" if s["as_of"] else "", s["url"]) if x)
                          for i, s in enumerate(rec["sources"], 1)]
    return "\n".join(L) + "\n"


def _announce(rec: dict, push=None):
    from . import alerts, notify
    push = push or notify.push
    title = f"Stoa 财报研报 · {rec['name']} {rec.get('period') or ''}".strip()
    body = f"**{rec['headline']}**\n\n持仓逻辑：{rec.get('verdict') or '无可对照'}。{rec.get('thesis_reason') or ''}\n\n网页 → 提醒 → 事件研报 查看全文。\n\n仅供参考，不构成投资建议。"
    try:
        push(title, body)
    except Exception:
        pass
    alerts.add_events([{"time": _now(), "kind": "earnings_report", "symbol": rec["symbol"], "title": f"{rec['name']} 财报研报：{rec['headline']}",
                        "report": rec["id"], "verdict": rec.get("verdict")}])


def remind_text(it: dict, mem: Memory, fetch: Fetch, days: int) -> tuple[str, dict]:
    n = it["next"]
    parts = [f"{it['name']}（{it['symbol']}）将于 {n['date']} 发布{n.get('period') or '财报'}（{days} 天后）。"]
    if n.get("eps_est") is not None:
        parts.append(f"市场预期季度 EPS {n['eps_est']}（{it.get('source')}）。")
    try:
        cons = fetch.consensus(it["symbol"]) or {}
        t = cons.get("target") or {}
        if t:
            parts.append(f"分析师目标价 {t.get('low')}–{t.get('high')}" + (f"，均值 {t['mean']}" if t.get("mean") else "") + f"（{cons.get('source')}）。")
    except Exception:
        pass
    th = thesis(mem, it["symbol"])
    if th["calls"]:
        cl = th["calls"][-1]
        parts.append(f"你上次的判断：{str(cl['ts'])[:10]} {cl['view']}，理由「{(cl.get('reason') or '')[:50]}」" + (f"，复盘 {cl['verdict']}。" if cl.get("verdict") else "，尚未到期。"))
    if th["user"]:
        rec_line = re.sub(r"^\[[\d-]+\]\s*", "", th["user"][0])[:60]
        parts.append(f"你的记录：{rec_line}。")
    return " ".join(parts), th


def tick(log=print, push=None, fetch: Fetch | None = None, mem: Memory | None = None, llm=None, today: str | None = None) -> list[dict]:
    """常驻调度的一轮：刷新日历（节流）、财报前提醒、财报后研报。返回本轮产生的事件。"""
    c = cfg()
    if not c["enabled"]:
        return []
    mem = mem or Memory()
    fetch = fetch or Fetch()
    d = refresh(mem, fetch)
    today = today or _today()
    td = datetime.strptime(today, "%Y-%m-%d").date()
    out = []
    from . import alerts, notify
    push = push or notify.push
    for sym, it in d["items"].items():
        n = it.get("next")
        if n:
            days = (datetime.strptime(n["date"], "%Y-%m-%d").date() - td).days
            key = f"{sym}|{n['date']}|remind"
            if 0 <= days <= c["remind_days"] and key not in d["reminded"]:
                text, _ = remind_text(it, mem, fetch, days)
                try:
                    push(f"Stoa 财报前提醒 · {it['name']}", text + "\n\n仅供参考，不构成投资建议。")
                except Exception:
                    pass
                ev = {"time": _now(), "kind": "earnings_soon", "symbol": sym, "title": text[:200], "date": n["date"]}
                alerts.add_events([ev])
                out.append(ev)
                with LOCK:
                    st = load()
                    st["reminded"].append(key)
                    save(st)
                    d["reminded"].append(key)
        for e in it.get("events") or []:
            ed = datetime.strptime(e["date"], "%Y-%m-%d").date()
            key = f"{sym}|{e['date']}"
            if key in d["reported"] or not (0 <= (td - ed).days <= WINDOW_DAYS):
                continue
            if not (e.get("published") or (td - ed).days >= 1):
                continue
            try:
                if llm is None:
                    from .agent import load_cfg
                    from .providers import Router
                    r = Router(load_cfg().get("llm", {}))
                    llm_use = r if r.ready else None
                else:
                    llm_use = llm
                rep = make_report(sym, mem, e, fetch, llm_use, with_debate=c["debate"], name=it.get("name"), push=push)
                out.append({"kind": "earnings_report", "symbol": sym, "report": rep["id"]})
                d["reported"][key] = rep["id"]
                log("财报研报", rep["id"], rep["headline"][:60])
            except Exception as ex:
                log("财报研报失败", sym, ex)
    return out


def report_for_agent(sym: str, mem: Memory, llm=None, with_debate: bool = False, fetch: Fetch | None = None) -> dict:
    if not sym:
        return {"error": "要给股票代码"}
    rep = make_report(sym, mem, fetch=fetch, llm=llm, with_debate=with_debate, push=False)
    rec = load_report(rep["id"]) or {}
    f = rec.get("facts") or {}
    return {"report_id": rep["id"], "headline": rep["headline"], "vs_consensus": {k: (f.get("vs_consensus") or {}).get(k) for k in ("verdict", "surprise_pct", "method", "rows")},
            "yoy": f.get("yoy"), "guidance": f.get("guidance"), "thesis_verdict": rep["verdict"], "thesis_reason": rec.get("thesis_reason"),
            "consensus": f.get("consensus"), "sources": rec.get("sources"), "path": rep["path"],
            "note": "研报已写入文件并推送；回答时转述 实际 vs 预期、同比、指引、持仓逻辑结论，数字照抄。"}


def load_report(rid: str) -> dict | None:
    rid = re.sub(r"[^A-Za-z0-9.\-]", "", rid or "")
    f = reports_dir() / f"{rid}.json"
    if not f.exists():
        return None
    try:
        j = json.loads(f.read_text(encoding="utf-8"))
        md = reports_dir() / f"{rid}.md"
        j["markdown"] = md.read_text(encoding="utf-8") if md.exists() else render(j)
        return j
    except Exception:
        return None


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="python -m stock_agent earnings", description="财报日历与事件研报")
    p.add_argument("cmd", nargs="?", default="list", choices=["list", "refresh", "check", "report"])
    p.add_argument("symbol", nargs="?")
    p.add_argument("--debate", action="store_true")
    a = p.parse_args(argv)
    from . import settings
    settings.load_env_file()
    mem = Memory()
    if a.cmd in ("list", "refresh"):
        v = calendar_view(mem, refresh_now=a.cmd == "refresh")
        for r in v["tracked"]:
            print(f"{r['symbol']:<10} {r['name']:<8} {r['why']:<6} 下次 {r['next_date'] or '—'}" + (f"（{r['days_left']} 天后，{r['period']}）" if r["next_date"] else "")
                  + (f"  上次 {r['last_date']}" if r["last_date"] else "") + (f"  ！{r['error']}" if r.get("error") else ""))
        print(v["note"]) if not v["tracked"] else None
    elif a.cmd == "check":
        print(tick())
    elif a.cmd == "report":
        if not a.symbol:
            raise SystemExit("用法：python -m stock_agent earnings report 0700.HK")
        from .agent import load_cfg
        from .providers import Router
        r = Router(load_cfg().get("llm", {}))
        rep = make_report(a.symbol, mem, llm=r if r.ready else None, with_debate=a.debate, push=None)
        print(rep["markdown"])
        print("已写入", rep["path"])
