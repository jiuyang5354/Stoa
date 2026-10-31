from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
TITLES = {"morning": "股市早报", "evening": "股市晚报", "adhoc": "股市快照"}


def _f(v, suffix=""):
    if v is None:
        return "—"
    if suffix == "%":
        return f"{v:+.2f}%"
    return f"{v:,.2f}"


def _row(name, symbol, d):
    if not d:
        return f"| {name} | {symbol} | 无数据 | | | | | |"
    return (
        f"| {name} | {symbol} | {_f(d['close'])} | {_f(d['chg_pct'], '%')} | "
        f"{_f(d['chg_5d'], '%')} | {_f(d['chg_20d'], '%')} | {d['rsi'] if d['rsi'] is not None else '—'} | "
        f"{'、'.join(d['signals']) or '—'} |"
    )


HEAD = "| 名称 | 代码 | 收盘 | 涨跌 | 5日 | 20日 | RSI | 信号 |\n|---|---|---:|---:|---:|---:|---:|---|"


def render(snapshot: dict, session: str, ai: str | None, sections: list[str] | None = None, late: str | None = None) -> str:
    """sections：早报的「今日判断」「重大数据前瞻」、晚报的「判断复盘」（已渲染好的 Markdown 行），放在行情表前面。
    late：补发说明（如「原定 08:30，补发于 10:12」），标题加「补发」。"""
    now = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [f"# {TITLES.get(session, '股市快照')}{'（补发）' if late else ''} · {now}（北京时间）", ""]
    if late:
        lines += [f"> 补发：{late}。行情是补发时的最新数据。", ""]
    if ai:
        lines += ["## AI 点评", "", ai, ""]
    if sections:
        lines += sections
    for market, rows in snapshot["indices"].items():
        lines += [f"## {market}", "", HEAD]
        lines += [_row(r["name"], r["symbol"], r["data"]) for r in rows]
        lines.append("")
    if snapshot["watchlist"]:
        lines += ["## 自选股", "", HEAD]
        lines += [_row(r["name"], r["symbol"], r["data"]) for r in snapshot["watchlist"]]
        lines.append("")
        news = [(r["name"], n) for r in snapshot["watchlist"] for n in r.get("news", [])]
        if news:
            lines += ["### 相关新闻", ""]
            lines += [f"- {name}：[{n['title']}]({n['url']})" if n.get("url") else f"- {name}：{n['title']}" for name, n in news]
            lines.append("")
    alerts = [
        f"{r['name']}：{'、'.join(r['data']['signals'])}"
        for rows in list(snapshot["indices"].values()) + [snapshot["watchlist"]]
        for r in rows
        if r.get("data") and r["data"].get("signals")
    ]
    if alerts:
        lines += ["## 信号提醒", ""] + [f"- {a}" for a in alerts] + [""]
    lines += ["---", "数据来源 Yahoo Finance，可能有延迟。信号与方向判断经回测不具统计显著性，仅供参考，不构成投资建议。"]
    return "\n".join(lines)
