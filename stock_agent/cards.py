"""回答下方的小数据卡（v0.16）：资金流柱、一致预期区间、分时走势、期权、龙虎榜、年报段落/对比。

agent 每轮把新数据工具的结果压成精简的卡片数据，存 数据目录/data/cards/<id>.json，回答末尾加 [[cards:<id>]]，
网页靠这个标记取数画图（历史对话里同样能展开）。卡片只放画图需要的数字，来源与时间照抄工具结果。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
CARD_TOOLS = {"get_fund_flow", "get_northbound", "get_dragon_tiger", "get_consensus", "get_intraday", "get_options", "read_filing", "compare_filings", "get_quotes",
              "price_levels", "financial_bridge", "option_payoff", "trade_replay"}


def cards_dir() -> Path:
    from .memory import home
    d = home() / "data" / "cards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _base(r: dict, kind: str) -> dict:
    return {"type": kind, "symbol": r.get("symbol"), "source": r.get("source"), "url": r.get("url"), "as_of": r.get("as_of"),
            "warnings": [w for w in (r.get("warnings") or []) if w][:3], "cite": r.get("_cite")}


def build(tool: str, args: dict, r) -> list[dict]:
    if not isinstance(r, dict) or r.get("error"):
        return []
    if tool == "get_fund_flow":
        c = _base(r, "flow")
        c.update(unit=r.get("unit"), sum5=r.get("main_net_5d"), super5=r.get("super_net_5d"),
                 rows=[{"date": x["date"], "main": x.get("main_net"), "super": x.get("super_net"), "intraday": x.get("intraday", False)} for x in r.get("rows") or []])
        return [c]
    if tool == "get_consensus":
        t = r.get("target") or {}
        c = _base(r, "consensus")
        c.update(low=t.get("low"), high=t.get("high"), mean=t.get("mean") or t.get("median"), price=r.get("price_now"), upside=r.get("upside_pct"),
                 ratings=r.get("ratings") or {}, orgs=r.get("orgs"),
                 eps=[{"label": str(e.get("year") or e.get("period")), "kind": e.get("kind", "预测"), "v": e.get("eps", e.get("avg"))} for e in (r.get("eps") or [])][:4],
                 alt=r.get("target_yahoo") or r.get("target_nasdaq"))
        return [c]
    if tool == "get_intraday":
        days = r.get("days") or []
        if not days:
            return []
        d = days[-1]
        c = _base(r, "intraday")
        c.update(date=d.get("date"), points=d.get("points") or [], prev_close=d.get("prev_close"), open=d.get("open"), high=d.get("high"), low=d.get("low"),
                 last=d.get("last"), vwap=d.get("vwap"), chg=d.get("chg_pct"), interval=r.get("interval_min"), tz=r.get("tz"),
                 more=[{"date": x["date"], "last": x.get("last"), "chg": x.get("chg_pct")} for x in days[:-1]])
        return [c]
    if tool == "get_options":
        c = _base(r, "options")
        c.update(spot=r.get("spot"), expiries=[{k: e.get(k) for k in ("expiry", "days", "atm_iv_pct", "pcr_oi", "pcr_volume", "max_pain", "near_30d")} for e in r.get("expiries") or []])
        return [c]
    if tool == "get_northbound":
        c = _base(r, "north")
        c.update(market=(r.get("market") or [])[:5], holdings=(r.get("holdings") or [])[:4], rule=r.get("rule_note"))
        return [c]
    if tool == "get_dragon_tiger":
        c = _base(r, "lhb")
        c.update(records=(r.get("records") or [])[:3], buy=(r.get("buy_seats") or [])[:5], sell=(r.get("sell_seats") or [])[:5],
                 recent=r.get("listed_recently"), note=r.get("note"), seats_date=r.get("seats_date"))
        return [c]
    if tool == "read_filing":
        c = _base(r, "filing")
        c.update(title=r.get("title"), year=r.get("year"), section=r.get("section_name"), pages=r.get("section_pages"), query=r.get("query"),
                 passages=[{"page": p["page"], "url": p.get("url"), "text": p["text"][:180], "cite": p.get("_cite")} for p in r.get("passages") or []])
        return [c]
    if tool == "compare_filings":
        c = _base(r, "compare")
        pick = lambda xs: [{"page": x.get("page") or x.get("page_b"), "url": x.get("url"), "text": (x.get("text") or x.get("after") or "")[:150], "cite": x.get("_cite")} for x in (xs or [])[:3]]
        c.update(year_a=r.get("year_a"), year_b=r.get("year_b"), section=r.get("section_name"), counts=r.get("counts"), ratio=r.get("unchanged_ratio"),
                 added=pick(r.get("added")), removed=pick(r.get("removed")))
        return [c]
    if tool == "price_levels":
        c = _base(r, "levels")
        c.update(last=r.get("last"), supports=r.get("supports"), resistances=r.get("resistances"), points=r.get("points"))
        return [c]
    if tool == "financial_bridge":
        c = _base(r, "bridge")
        c.update(name=r.get("name"), period=r.get("period"), steps=r.get("steps"), ratios=r.get("ratios"), currency=r.get("currency"))
        return [c] if r.get("steps") else []
    if tool == "option_payoff":
        c = _base(r, "payoff")
        c.update(spot=r.get("spot"), breakevens=r.get("breakevens"), max_profit=r.get("max_profit"), max_loss=r.get("max_loss"),
                 pnl_at_spot=r.get("pnl_at_spot"), points=r.get("points"), legs=r.get("legs"))
        return [c]
    if tool == "trade_replay":
        c = _base(r, "replay")
        c.update({k: r.get(k) for k in ("entry", "stop", "target", "n", "horizon", "p_target_first", "p_stop_first", "p_neither",
                                         "exp_return_pct", "exp_R", "fan", "sample")})
        return [c]
    if tool == "get_quotes":  # 只在交叉校验报警时出卡
        out = []
        for sym, v in r.items():
            if isinstance(v, dict) and v.get("warnings"):
                cc = v.get("crosscheck") or {}
                out.append({"type": "quotecheck", "symbol": sym, "values": cc.get("values"), "spread": cc.get("spread_pct"), "threshold": cc.get("threshold_pct"),
                            "warnings": v["warnings"][:3], "source": v.get("source"), "as_of": v.get("date"), "cite": v.get("_cite")})
        return out
    return []


def save(cards: list[dict]) -> str | None:
    if not cards:
        return None
    cid = f"{datetime.now(CN_TZ):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    (cards_dir() / f"{cid}.json").write_text(json.dumps({"id": cid, "cards": cards}, ensure_ascii=False, default=str), encoding="utf-8")
    return cid


def load(cid: str) -> dict | None:
    cid = re.sub(r"[^A-Za-z0-9_.-]", "", cid or "")
    f = cards_dir() / f"{cid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
