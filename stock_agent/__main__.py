import argparse
import sys
import json
from datetime import datetime
from pathlib import Path

import yaml

from . import data, indicators, llm, notify, report

ROOT = Path(__file__).resolve().parent.parent


def default_config() -> str:
    from .memory import config_path
    return str(config_path())


def build_snapshot(cfg: dict, with_news: bool) -> dict:
    alerts = cfg.get("alerts", {})
    idx = cfg.get("indices", {})
    wl = cfg.get("watchlist") or []
    symbols = [i["symbol"] for rows in idx.values() for i in rows] + [w["symbol"] for w in wl]
    closes = data.fetch_closes(list(dict.fromkeys(symbols)))

    def item(e):
        s = closes.get(e["symbol"])
        return {"symbol": e["symbol"], "name": e.get("name", e["symbol"]),
                "data": indicators.analyze(s, alerts) if s is not None else {}}

    snap = {"indices": {m: [item(e) for e in rows] for m, rows in idx.items()},
            "watchlist": [item(e) for e in wl], "_closes": closes}
    if with_news:
        for w in snap["watchlist"]:
            w["news"] = data.fetch_news(w["symbol"])
    return snap


def session_sections(session: str, snap: dict, closes: dict, mem, cfg: dict) -> list[str]:
    """早报：今日判断 + 重大数据前瞻；晚报：判断复盘。结果也放进 snap（给 AI 点评与战报用）。"""
    from datetime import date
    from . import judgments, macrocal
    today = datetime.now(report.CN_TZ).date()
    if session == "evening":
        rs = judgments.review(mem, snap, today)
        snap["review"] = [{k: r.get(k) for k in ("name", "view", "since", "price", "close", "chg", "result", "note")} for r in rs]
        return judgments.render_review(rs)
    vps = {}
    rows = list(snap.get("watchlist") or []) or [r[0] for r in snap["indices"].values() if r]
    from concurrent.futures import ThreadPoolExecutor
    from . import volprofile

    def one(sym):
        try:
            bars, _ = volprofile.fetch_bars(sym, "daily", 60)
            return sym, volprofile.profile(bars)
        except Exception:
            return sym, None
    with ThreadPoolExecutor(6) as ex:
        vps = dict(ex.map(one, [r["symbol"] for r in rows]))
    js = judgments.morning(mem, snap, vps, today)
    snap["judgments"] = [{k: j.get(k) for k in ("name", "view", "confidence", "expires", "flip", "carried")} for j in js]
    out = judgments.render_morning(js)
    try:
        from .earnings import tracked
        hold = [{"symbol": s, "name": v["name"]} for s, v in tracked(mem).items()]
    except Exception:
        hold = []
    hold = hold or [{"symbol": r["symbol"], "name": r["name"]} for r in rows]
    evs = macrocal.events(today, macrocal.lookahead(today), cfg.get("macro_events"))
    nowiso = datetime.now(report.CN_TZ).isoformat()
    big = [e for e in evs if e["at"] >= nowiso]  # 已经公布的不再写情景
    if big:
        missing = [h["symbol"] for h in hold if h["symbol"] not in closes]
        if missing:
            try:
                closes = {**closes, **data.fetch_closes(missing)}
            except Exception:
                pass
        items = [macrocal.scenarios(e, hold, closes, today) for e in big]
        snap["macro"] = [{"name": it["event"]["name"], "time_bj": it["event"]["time_bj"]} for it in items]
        out += macrocal.render(items)
    return out


def sub_command() -> bool:
    """chat / review / memory 子命令；其余参数走报告模式。"""
    if len(sys.argv) < 2 or sys.argv[1] not in ("chat", "review", "memory", "serve", "mcp", "providers", "desktop", "backtest", "eval", "earnings", "news-snapshot", "research"):
        return False
    cmd = sys.argv[1]
    if cmd == "chat":
        from .agent import repl
        repl()
    elif cmd == "review":
        from .agent import review_due
        from .memory import Memory
        notes = review_due(Memory())
        print("\n".join(notes) or "没有到期的判断。")
    elif cmd == "serve":
        from .serve import main as serve
        serve()
    elif cmd == "desktop":
        from .desktop import main as desktop
        desktop()
    elif cmd == "mcp":
        from .mcp import stdio
        stdio()
    elif cmd == "providers":
        from .data import PROVIDERS, provider_order
        from .providers import PRESETS, Router
        print("大模型：", Router())
        print("可选预设：", " ".join(PRESETS))
        print("行情源顺序：", " → ".join(provider_order()), "（可选：yahoo " + " ".join(PROVIDERS) + "）")
    elif cmd == "backtest":
        from .backtest import main as bt
        bt(sys.argv[2:])
    elif cmd == "eval":
        from .evals import main as ev
        ev(sys.argv[2:])
    elif cmd == "news-snapshot":
        from .newsdaily import main as nm
        nm(sys.argv[2:])
    elif cmd == "research":
        from .inquiry import main as im
        im(sys.argv[2:])
    elif cmd == "earnings":
        from .earnings import main as em
        em(sys.argv[2:])
    elif cmd == "memory":
        from .memory import Memory
        args = sys.argv[2:]
        if args and args[0] in ("reindex", "index", "stats", "search"):
            from . import settings, vectors
            settings.load_env_file()
            m = Memory()
            if args[0] in ("reindex", "index"):
                import time as _t
                t0 = _t.time()
                s = vectors.sync(m, full=args[0] == "reindex")
                print(f"语义索引{'重建' if args[0] == 'reindex' else '增量更新'}完成（{_t.time() - t0:.1f}s）：", s)
                print(vectors.stats(m))
            elif args[0] == "stats":
                print(json.dumps(vectors.stats(m), ensure_ascii=False, indent=1))
            else:
                for h in vectors.search(m, " ".join(args[1:]), limit=10):
                    print(f"[{h['label']} · {h['via']} · {h['sim']}] {h['ts'][:16]}  {h['text'][:100]}")
        else:
            print(Memory().snapshot())
    return True


def main():
    if sub_command():
        return
    p = argparse.ArgumentParser(description="Stoa · 股市 Agent")
    p.add_argument("--session", choices=["morning", "evening", "adhoc"], default="adhoc")
    p.add_argument("--config", default=None)
    p.add_argument("--no-llm", action="store_true", help="不调用大模型点评")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    p.add_argument("--json", action="store_true", help="输出 JSON 快照")
    p.add_argument("--push", action="store_true", help="推送到已配置的渠道")
    p.add_argument("--poster", action="store_true", help="另出一张战报长图（装了 Playwright 出 PNG，否则只写 HTML）")
    p.add_argument("--late", help="补发说明（定时器补跑时传入，报告标「补发」）")
    p.add_argument("--push-key", help="推送幂等键（任务+计划时间）；同一个键重试或补跑不会重复推送")
    p.add_argument("--no-judgments", action="store_true", help="早报不写判断、晚报不复盘")
    a = p.parse_args()

    cfg = yaml.safe_load(Path(a.config or default_config()).read_text(encoding="utf-8"))
    snap = build_snapshot(cfg, with_news=not a.no_news)
    closes = snap.pop("_closes", {})
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return
    from .memory import Memory
    mem = Memory()
    sections = []
    if not a.no_judgments and a.session in ("morning", "evening"):
        try:
            sections = session_sections(a.session, snap, closes, mem, cfg)
        except Exception as e:  # 判断/复盘出错不影响行情报告
            sections = [f"> 判断模块出错：{str(e)[:160]}", ""]
    ai = None if a.no_llm else llm.comment(snap, cfg.get("llm", {}), mem.snapshot())
    md = report.render(snap, a.session, ai, sections, late=a.late)
    print(md)
    if not a.stdout:
        from .memory import home
        out = home() / "reports" / f"{datetime.now(report.CN_TZ):%Y-%m-%d}-{a.session}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text(md, encoding="utf-8")
        (home() / "reports" / "latest.md").write_text(md, encoding="utf-8")
        # 战报长图用：存一份结构化快照，网页 /poster 直接读，不再重抓行情
        (home() / "reports" / "latest.json").write_text(json.dumps(
            {"session": a.session, "time": datetime.now(report.CN_TZ).isoformat(), "snap": snap, "ai": ai},
            ensure_ascii=False, default=str), encoding="utf-8")
        if a.poster:
            from . import poster
            d = poster.data(snap, a.session, ai, mem.call_stats())
            img = out.with_suffix(".png")
            page = poster.html(d, save=False)
            print(f"\n战报长图：{img if poster.png(page, img) else img.with_suffix('.html')}")
    if a.push:
        title = report.TITLES.get(a.session, "股市快照") + ("（补发）" if a.late else "")
        sent = notify.push(title, md, key=a.push_key)
        print(f"\n已推送：{'、'.join(sent) or '未配置任何推送渠道'}")


if __name__ == "__main__":
    main()
