import argparse
import json
from datetime import datetime
from pathlib import Path

import yaml

from . import data, indicators, llm, notify, report

ROOT = Path(__file__).resolve().parent.parent


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
            "watchlist": [item(e) for e in wl]}
    if with_news:
        for w in snap["watchlist"]:
            w["news"] = data.fetch_news(w["symbol"])
    return snap


def main():
    p = argparse.ArgumentParser(description="股市 Agent")
    p.add_argument("--session", choices=["morning", "evening", "adhoc"], default="adhoc")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--no-llm", action="store_true", help="不调用大模型点评")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    p.add_argument("--json", action="store_true", help="输出 JSON 快照")
    p.add_argument("--push", action="store_true", help="推送到已配置的渠道")
    a = p.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    snap = build_snapshot(cfg, with_news=not a.no_news)
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return
    ai = None if a.no_llm else llm.comment(snap, cfg.get("llm", {}))
    md = report.render(snap, a.session, ai)
    print(md)
    if not a.stdout:
        out = ROOT / "reports" / f"{datetime.now(report.CN_TZ):%Y-%m-%d}-{a.session}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text(md, encoding="utf-8")
        (ROOT / "reports" / "latest.md").write_text(md, encoding="utf-8")
    if a.push:
        sent = notify.push(report.TITLES.get(a.session, "股市快照"), md)
        print(f"\n已推送：{'、'.join(sent) or '未配置任何推送渠道'}")


if __name__ == "__main__":
    main()
