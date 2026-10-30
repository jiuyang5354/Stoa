"""Nuntius · 每日新闻快照（前瞻采集，给将来的无前视回测用）。

历史个股新闻只能回溯 1–2 个月（东方财富搜索 / 新浪都有页数上限），所以一年前的新闻在回测里没法公平使用。
唯一可靠的办法是从现在起每天把自选股和指数的相关新闻带「抓取时间」落盘，一年后就有一份 point-in-time 的新闻库。

  python -m stock_agent news-snapshot                 # 抓一次：config.yaml 的自选股 + 指数
  python -m stock_agent news-snapshot --symbols 600519.SS,NVDA --limit 20
  python -m stock_agent news-snapshot --stats         # 看库里有多少
  serve 常驻时：NEWS_SNAPSHOT_TIMES=17:30（默认；北京时间，每天，含周末；设 off 关闭）

存储（数据目录/data/news/）：
  <代码>.jsonl      每行一条新闻：{id, symbol, fetched_at(带时区 ISO), published(源给的发布时间，原样), published_tz,
                    title, summary, source, url, via, query}；按 id（url 或 标题+发布时间 的 sha1）去重，只追加不改写。
  snapshots.jsonl   每次快照一行：{at, symbols, new, seen, errors, seconds}。

回测用 asof(symbol, day)：只返回 fetched_at 严格早于决策日 00:00（北京时间）的条目——以「我们什么时候真的看到它」为准，
而不是源给的发布时间（源可能事后改时间或补录）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
_LOCK = threading.Lock()


def news_dir() -> Path:
    from .memory import home
    d = home() / "data" / "news"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fname(symbol: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in symbol)
    return news_dir() / f"{safe}.jsonl"


def item_id(it: dict) -> str:
    key = (it.get("url") or "").strip() or f"{it.get('title', '').strip()}|{it.get('time') or it.get('published') or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load(symbol: str) -> list[dict]:
    p = _fname(symbol)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def targets(cfg: dict | None = None) -> list[dict]:
    """要抓的对象：自选股 + 指数。[{symbol, name, kind}]"""
    if cfg is None:
        from .agent import load_cfg
        cfg = load_cfg()
    out = [{"symbol": w["symbol"], "name": w.get("name") or w["symbol"], "kind": "stock"} for w in cfg.get("watchlist") or []]
    for rows in (cfg.get("indices") or {}).values():
        out += [{"symbol": i["symbol"], "name": i.get("name") or i["symbol"], "kind": "index"} for i in rows]
    seen, uniq = set(), []
    for t in out:
        if t["symbol"] not in seen:
            seen.add(t["symbol"])
            uniq.append(t)
    return uniq


def _fetch(t: dict, limit: int, fetcher=None) -> tuple[list[dict], str | None]:
    """一个对象的新闻。个股走 research.news；A/港股指数用名字做关键词搜，美股指数走 Yahoo RSS。"""
    from . import research
    f = fetcher or research.news
    try:
        if t["kind"] == "index" and not t["symbol"].startswith("^"):
            r = f(t["symbol"], limit, t["name"])
        else:
            r = f(t["symbol"], limit)
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:100]}"
    err = "；".join(r.get("errors") or []) or None
    return list(r.get("news") or []), err


def snapshot(symbols: list[str] | None = None, limit: int = 15, cfg: dict | None = None, fetcher=None, now: datetime | None = None) -> dict:
    """抓一次并落盘。返回 {at, symbols, new, seen, errors, per_symbol, seconds}。"""
    t0 = time.time()
    now = now or datetime.now(CN_TZ)
    ts = targets(cfg) if symbols is None else [{"symbol": s, "name": s, "kind": "index" if s.startswith("^") else "stock"} for s in symbols]
    if symbols is not None and cfg is not None:  # 给了 cfg 时尽量用里面的名字
        names = {t["symbol"]: t for t in targets(cfg)}
        ts = [names.get(t["symbol"], t) for t in ts]
    at = now.isoformat(timespec="seconds")
    new_total, seen_total, errors, per = 0, 0, {}, {}
    for t in ts:
        items, err = _fetch(t, limit, fetcher)
        if err:
            errors[t["symbol"]] = err
        with _LOCK:
            have = {x.get("id") for x in load(t["symbol"])}
            rows = []
            for it in items:
                iid = item_id(it)
                if iid in have:
                    seen_total += 1
                    continue
                have.add(iid)
                rows.append({"id": iid, "symbol": t["symbol"], "fetched_at": at, "published": it.get("time") or it.get("published") or "",
                             "published_tz": "Asia/Shanghai" if it.get("source") != "Yahoo Finance" else "UTC",
                             "title": it.get("title", ""), "summary": it.get("summary", ""), "source": it.get("source", ""),
                             "url": it.get("url", ""), "via": "eastmoney" if "eastmoney" in (it.get("url") or "") else (it.get("source") or ""),
                             "query": t["name"] if t["kind"] == "index" else t["symbol"]})
            if rows:
                with _fname(t["symbol"]).open("a", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        new_total += len(rows)
        per[t["symbol"]] = {"new": len(rows), "got": len(items)}
    rec = {"at": at, "symbols": [t["symbol"] for t in ts], "new": new_total, "seen": seen_total, "errors": errors,
           "per_symbol": per, "seconds": round(time.time() - t0, 1)}
    with _LOCK, (news_dir() / "snapshots.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: v for k, v in rec.items() if k != "per_symbol"}, ensure_ascii=False) + "\n")
    return rec


def asof(symbol: str, day, days: int = 30, limit: int = 50) -> list[dict]:
    """point-in-time：决策日 day 之前（fetched_at < day 00:00 北京时间）抓到的、最近 days 天内的新闻，新到旧。"""
    import pandas as pd
    d0 = pd.Timestamp(day)
    cut = (d0.tz_localize(CN_TZ) if d0.tzinfo is None else d0.tz_convert(CN_TZ)).normalize()
    lo = cut - pd.Timedelta(days=days)
    out = []
    for it in load(symbol):
        try:
            f = pd.Timestamp(it["fetched_at"])
        except Exception:
            continue
        f = f.tz_localize(CN_TZ) if f.tzinfo is None else f.tz_convert(CN_TZ)
        if lo <= f < cut:
            out.append(it)
    out.sort(key=lambda x: (x["fetched_at"], x.get("published") or ""), reverse=True)
    return out[:limit]


def stats() -> dict:
    d = news_dir()
    per = {}
    for p in sorted(d.glob("*.jsonl")):
        if p.name == "snapshots.jsonl":
            continue
        rows = load(p.stem)
        if rows:
            per[rows[0]["symbol"]] = {"items": len(rows), "first": min(r["fetched_at"] for r in rows)[:10], "last": max(r["fetched_at"] for r in rows)[:10]}
    snaps = (d / "snapshots.jsonl").read_text(encoding="utf-8").splitlines() if (d / "snapshots.jsonl").exists() else []
    return {"dir": str(d), "symbols": len(per), "items": sum(v["items"] for v in per.values()), "snapshots": len(snaps),
            "last_snapshot": json.loads(snaps[-1]).get("at") if snaps else None, "per_symbol": per}


def snapshot_times() -> list[str]:
    v = os.getenv("NEWS_SNAPSHOT_TIMES", "17:30").strip()
    return [] if v.lower() in ("off", "0", "false", "") else [x.strip() for x in v.split(",") if ":" in x]


def loop(log=print, sleep=20):
    """serve 常驻：每天到点抓一次（北京时间，含周末——新闻周末也有）。启动时若今天还没抓过且已过点，补抓一次。"""
    done = set()
    try:
        last = stats().get("last_snapshot")
    except Exception:
        last = None
    while True:
        now, times = datetime.now(CN_TZ), snapshot_times()
        for hhmm in times:
            key = (now.date(), hhmm)
            due = now.strftime("%H:%M") >= hhmm and key not in done and not (last and last[:10] == now.strftime("%Y-%m-%d") and last[11:16] >= hhmm)
            if due:
                done.add(key)
                try:
                    r = snapshot()
                    last = r["at"]
                    log(f"新闻快照：{len(r['symbols'])} 个对象，新增 {r['new']} 条，重复 {r['seen']}，出错 {len(r['errors'])}")
                except Exception as e:
                    log("新闻快照出错", e)
        time.sleep(sleep)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m stock_agent news-snapshot", description="Nuntius · 每日新闻快照（前瞻采集）")
    p.add_argument("--symbols", help="逗号分隔；默认 config.yaml 的自选股 + 指数")
    p.add_argument("--limit", type=int, default=15, help="每个对象最多取几条")
    p.add_argument("--stats", action="store_true", help="只看库里的统计")
    a = p.parse_args(argv)
    from . import settings
    settings.load_env_file()
    if a.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=1))
        return
    r = snapshot([s.strip() for s in a.symbols.split(",")] if a.symbols else None, limit=a.limit)
    print(f"新闻快照 {r['at']}：{len(r['symbols'])} 个对象，新增 {r['new']} 条，已有 {r['seen']} 条，耗时 {r['seconds']} s")
    for s, v in r["per_symbol"].items():
        print(f"  {s:12} 取到 {v['got']:>3}  新增 {v['new']:>3}" + (f"  出错：{r['errors'][s]}" if s in r["errors"] else ""))
    print(f"存在 {news_dir()}")
