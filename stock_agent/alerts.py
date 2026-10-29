"""价格预警 + 公告提醒。

规则存在数据目录 alerts.json；后台线程每 ALERT_INTERVAL 秒（默认 300）检查一次：
- above / below：现价上穿 / 跌破某个价位
- up / down：当日涨幅 / 跌幅超过某个百分比
- announcement：这只股票有新公告就提醒
另外 ANN_WATCH=1（默认开）时，自选股的新公告也会自动提醒。
触发后推送到已开的渠道，并记进事件列表，网页右上角弹出提示。
"""
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .memory import home

CN = timezone(timedelta(hours=8))
KINDS = {"above": "涨到", "below": "跌到", "up": "单日涨超", "down": "单日跌超", "announcement": "有新公告"}
LOCK = threading.Lock()
MAX_EVENTS = 200


def _now():
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M")


def _path():
    return home() / "data" / "alerts.json"


def load() -> dict:
    p = _path()
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d.setdefault("rules", []); d.setdefault("events", []); d.setdefault("seen", {})
            return d
        except Exception:
            pass
    return {"rules": [], "events": [], "seen": {}}


def save(d: dict):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d["events"] = d["events"][-MAX_EVENTS:]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def add_events(events: list[dict]):
    """其它模块（财报事件等）往提醒事件流里写：网页轮询弹出、桌面窗口通知。"""
    if not events:
        return
    with LOCK:
        d = load()
        d["events"] += events
        save(d)


def describe(r: dict) -> str:
    n = r.get("name") or r["symbol"]
    if r["kind"] == "announcement":
        return f"{n} 有新公告"
    unit = "%" if r["kind"] in ("up", "down") else ""
    return f"{n} {KINDS[r['kind']]} {r['value']:g}{unit}"


def add(symbol: str, kind: str, value: float | None = None, name: str = "", repeat: bool = False, note: str = "") -> dict:
    symbol = symbol.strip().upper()
    if kind not in KINDS:
        return {"error": f"kind 只能是 {'/'.join(KINDS)}"}
    if kind != "announcement":
        try:
            value = float(value)
        except (TypeError, ValueError):
            return {"error": "价格或百分比要填数字"}
        if value <= 0:
            return {"error": "数值要大于 0（跌幅也填正数，如 5 表示跌超 5%）"}
    r = {"id": uuid.uuid4().hex[:8], "symbol": symbol, "name": name or symbol, "kind": kind, "value": value,
         "repeat": bool(repeat), "note": note[:120], "created": _now(), "fired": None, "active": True}
    with LOCK:
        d = load()
        if kind == "announcement":
            r["repeat"] = True
        if any(x["symbol"] == symbol and x["kind"] == kind and x.get("value") == value and x["active"] for x in d["rules"]):
            return {"msg": "同样的提醒已经有了。", "rule": r}
        d["rules"].append(r)
        save(d)
    if kind == "announcement":
        r["repeat"] = True
    tail = "（每条新公告都会提醒，删掉才停）" if kind == "announcement" else "（每天最多提醒一次）" if repeat else "（触发一次后自动关闭）"
    return {"msg": f"已设提醒：{describe(r)}{tail}", "rule": r}


def remove(rid: str) -> str:
    with LOCK:
        d = load()
        n = len(d["rules"])
        d["rules"] = [r for r in d["rules"] if r["id"] != rid]
        save(d)
    return "已删除。" if len(d["rules"]) < n else f"没有 id 为 {rid} 的提醒。"


def listing() -> dict:
    d = load()
    return {"rules": [dict(r, text=describe(r)) for r in d["rules"]], "events": d["events"][-50:][::-1],
            "interval": int(os.getenv("ALERT_INTERVAL", "300")), "ann_watch": os.getenv("ANN_WATCH", "1") != "0"}


# ---------------- 检查 ----------------
def _price_hits(rules, quotes):
    hits = []
    today = datetime.now(CN).strftime("%Y-%m-%d")
    for r in rules:
        q = quotes.get(r["symbol"])
        if not r["active"] or r["kind"] == "announcement" or not q:
            continue
        if r["repeat"] and (r.get("fired") or "")[:10] == today:
            continue
        price, pct, v = q["price"], q["pct"], r["value"]
        ok = {"above": price >= v, "below": price <= v, "up": pct is not None and pct >= v,
              "down": pct is not None and pct <= -v}[r["kind"]]
        if ok:
            hits.append((r, f"{r.get('name') or r['symbol']} 现价 {price:g}，今日 {pct:+.2f}%（{KINDS[r['kind']]} {v:g}{'%' if r['kind'] in ('up', 'down') else ''}）"))
    return hits


def _quotes(symbols):
    from . import data
    out = {}
    if not symbols:
        return out
    closes = data.fetch_closes(list(symbols))
    for s, c in closes.items():
        c = c.dropna()
        if len(c) >= 1:
            prev = float(c.iloc[-2]) if len(c) >= 2 else None
            price = float(c.iloc[-1])
            out[s] = {"price": round(price, 3), "pct": round((price / prev - 1) * 100, 2) if prev else None}
    return out


def _watch_symbols():
    try:
        from .agent import load_cfg
        return [(w["symbol"], w.get("name", w["symbol"])) for w in load_cfg().get("watchlist") or []]
    except Exception:
        return []


def check(push=None, fetch_quotes=None, fetch_ann=None) -> list[dict]:
    """跑一轮检查，返回新触发的事件。push/fetch_* 可替换，方便测试。"""
    from . import notify, research
    push = push or notify.push
    fetch_quotes = fetch_quotes or _quotes
    fetch_ann = fetch_ann or (lambda s: research.announcements(s, limit=5).get("announcements") or [])
    with LOCK:
        d = load()
    rules = [r for r in d["rules"] if r["active"]]
    new = []
    # 价格
    syms = {r["symbol"] for r in rules if r["kind"] != "announcement"}
    try:
        qs = fetch_quotes(syms)
    except Exception:
        qs = {}
    for r, text in _price_hits(rules, qs):
        new.append({"time": _now(), "kind": "price", "symbol": r["symbol"], "title": text, "rule": r["id"], "note": r.get("note", "")})
        r["fired"] = _now()
        if not r["repeat"]:
            r["active"] = False
    # 公告：显式规则 + 自选股
    ann_syms = {r["symbol"]: r.get("name") for r in rules if r["kind"] == "announcement"}
    if os.getenv("ANN_WATCH", "1") != "0":
        for s, n in _watch_symbols():
            ann_syms.setdefault(s.upper(), n)
    for s, n in ann_syms.items():
        try:
            items = fetch_ann(s)
        except Exception:
            continue
        seen = set(d["seen"].get(s, []))
        urls = [a["url"] for a in items]
        if s not in d["seen"]:  # 第一次只记下已有公告，不提醒旧的
            d["seen"][s] = urls[:30]
            continue
        for a in items:
            if a["url"] not in seen:
                new.append({"time": _now(), "kind": "announcement", "symbol": s, "title": f"{n or s} 公告：{a['title']}",
                            "url": a["url"], "published": a.get("time", "")})
        d["seen"][s] = (urls + [u for u in d["seen"][s] if u not in urls])[:30]
    with LOCK:
        cur = load()
        byid = {r["id"]: r for r in rules}
        cur["rules"] = [byid.get(r["id"], r) for r in cur["rules"]]
        cur["seen"] = d["seen"]
        cur["events"] += new
        save(cur)
    if new:
        body = "\n\n".join(f"**{e['title']}**" + (f"\n{e['url']}" if e.get("url") else "") + (f"\n备注：{e['note']}" if e.get("note") else "") for e in new)
        try:
            push(f"Stoa 提醒 · {len(new)} 条", body + "\n\n仅供参考，不构成投资建议。")
        except Exception:
            pass
    return new


def loop(log=print):
    import time
    time.sleep(15)
    while True:
        try:
            ev = check()
            if ev:
                log("提醒", len(ev), "条：", "；".join(e["title"] for e in ev)[:200])
        except Exception as e:
            log("提醒检查出错", e)
        try:  # 财报事件：日历刷新（节流）、财报前提醒、财报后自动研报
            from . import earnings
            earnings.tick(log)
        except Exception as e:
            log("财报事件检查出错", e)
        time.sleep(max(60, int(os.getenv("ALERT_INTERVAL", "300"))))
