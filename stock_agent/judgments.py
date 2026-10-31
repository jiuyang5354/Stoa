"""早报判断与晚报复盘。

早报：每个关注标的写一条「判断 + 失效时间 + 翻转条件」，记进判断账本（calls 表，source='morning'）。
  - 判断：规则给出（均线排列 + 成交分布的平衡/失衡），置信度封顶 65——回测里择时判断不显著，不装作很有把握；
  - 翻转条件：看涨看「收盘跌破」最近的支撑（20 日线 / 价值区下沿 / POC 里离现价最近且在下方的），看跌反之，震荡两头都写；
  - 失效时间：N 个交易日后的收盘（默认 5，REPORT_CALL_DAYS 可调），到期由账本照常结算；
  - 同一标的还有没过期、没失效的早报判断且方向没变，就沿用不重记；方向变了，旧的标「翻转」、记新的。
晚报：对照当天仍在跟踪的早报判断逐条复盘——站住 / 承压（方向不利但没破翻转价）/ 失效（收盘触发翻转条件）/ 待定（今天没有新收盘）。
v0.35：每条判断附「历史触达率底数」（baserate.py：同等幅度过去约 250 个交易日在 N 天内出现的比例），写进账本；
晚报给出已结算判断的命中率 vs 平均底数。底数不是预测概率；取不到日线就不写，不影响判断本身。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
CONF_CAP = 65
BAND = 1.0  # 震荡判断：收盘相对判断价 ±1% 以内算站住


def horizon_days() -> int:
    try:
        return max(1, min(20, int(os.getenv("REPORT_CALL_DAYS", "5"))))
    except ValueError:
        return 5


def add_bdays(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


CLOSE_TIME = {"A股": "15:00 北京时间", "港股": "16:00 香港时间", "美股": "16:00 美东时间"}


def market(sym: str) -> str:
    s = sym.upper()
    if s.endswith((".SS", ".SZ", ".BJ")):
        return "A股"
    return "港股" if s.endswith(".HK") or s in ("^HSI", "HSTECH.HK") else "美股"


def judge(symbol: str, name: str, d: dict, vp: dict | None = None, today: date | None = None, days: int | None = None) -> dict | None:
    """d = indicators.analyze() 的结果；vp = volprofile.profile() 结果（可无）。返回判断 dict，数据不够返回 None。"""
    if not d or d.get("close") is None or d.get("ma20") is None:
        return None
    today = today or datetime.now(CN_TZ).date()
    days = days or horizon_days()
    c, ma20, ma60 = d["close"], d["ma20"], d.get("ma60")
    gap = (c / ma20 - 1) * 100
    why = []
    if ma60 and c > ma20 > ma60:
        view, why = "看涨", [f"收盘 {c:g} 在 20 日线 {ma20:g} 与 60 日线 {ma60:g} 之上，多头排列"]
    elif ma60 and c < ma20 < ma60:
        view, why = "看跌", [f"收盘 {c:g} 在 20 日线 {ma20:g} 与 60 日线 {ma60:g} 之下，空头排列"]
    else:
        view, why = "震荡", [f"均线没有排列（收盘 {c:g}，20 日线 {ma20:g}" + (f"，60 日线 {ma60:g}）" if ma60 else "）")]
    conf = 50 + min(10, abs(gap) * 1.5)
    if vp and not vp.get("error"):
        st, rg = vp.get("state"), vp.get("regime")
        why.append(f"成交分布：{st}（{rg}），价值区 {vp['val']:g}–{vp['vah']:g}，POC {vp['poc']:g}")
        if rg == "趋势" and ((st == "向上失衡" and view == "看涨") or (st == "向下失衡" and view == "看跌")):
            conf += 5
        elif rg == "震荡" and view != "震荡" and st == "平衡":
            conf -= 5
            if abs(gap) < 2:
                view = "震荡"
                why.append("价格还在价值区里、离 20 日线不到 2%，按震荡看")
    rsi = d.get("rsi")
    if rsi is not None and ((view == "看涨" and rsi >= 75) or (view == "看跌" and rsi <= 25)):
        conf -= 5
        why.append(f"RSI {rsi:g} 已在极端区，追的性价比低")
    conf = int(max(50, min(CONF_CAP, round(conf))))
    # 翻转价位
    sup = [x for x in (ma20, (vp or {}).get("val"), (vp or {}).get("poc")) if x and x < c]
    res = [x for x in (ma20, (vp or {}).get("vah"), (vp or {}).get("poc")) if x and x > c]
    lo20 = d.get("low_20d")
    hi20 = d.get("high_20d")
    flip_up = flip_down = None
    if view == "看涨":
        flip_down = max(sup) if sup else round(c * 0.97, 3)
        flip = f"收盘跌破 {flip_down:g} → 看涨作废（转震荡/看跌）"
    elif view == "看跌":
        flip_up = min(res) if res else round(c * 1.03, 3)
        flip = f"收盘站上 {flip_up:g} → 看跌作废（转震荡/看涨）"
    else:
        flip_up = vp["vah"] if (vp or {}).get("vah") and vp["vah"] > c else (hi20 if hi20 and hi20 > c * 1.005 else round(c * 1.03, 3))
        flip_down = vp["val"] if (vp or {}).get("val") and vp["val"] < c else (lo20 if lo20 and lo20 < c * 0.995 else round(c * 0.97, 3))
        flip = f"收盘站上 {flip_up:g} 转看涨、跌破 {flip_down:g} 转看跌"
    exp_day = add_bdays(today, days)
    mk = market(symbol)
    return {"symbol": symbol, "name": name or symbol, "market": mk, "view": view, "confidence": conf, "price": c,
            "as_of": d.get("date"), "flip": flip, "flip_up": round(flip_up, 3) if flip_up else None,
            "flip_down": round(flip_down, 3) if flip_down else None,
            "expires": f"{exp_day.isoformat()} {CLOSE_TIME[mk]}收盘", "due": exp_day.isoformat(), "days": days, "reason": "；".join(why)}


def attach_base(js: list[dict], bars: dict | None) -> list[dict]:
    """bars = {代码: 约 300 根日线}。算出底数就写进判断（base_rate / flip_rate / base_n），算不出就跳过。"""
    if not bars:
        return js
    from . import baserate
    for j in js:
        r = baserate.for_judgment(j, bars.get(j["symbol"]))
        if r:
            j.update({k: r.get(k) for k in ("base_rate", "flip_rate")}, base_n=r["n"])
    return js


def record(mem, js: list[dict], today: date | None = None) -> list[dict]:
    """写进账本：同一标的有仍有效的同方向早报判断就沿用；方向变了旧的标「翻转」再记新的。返回带 id / carried 的列表。"""
    today = today or datetime.now(CN_TZ).date()
    td = today.isoformat()
    out = []
    for j in js:
        act = mem.calls_where("symbol=? AND source='morning' AND status='open' AND due>=? AND (review IS NULL OR review NOT IN ('失效','翻转'))",
                              (j["symbol"], td))
        same = [a for a in act if a["view"] == j["view"]]
        if same:
            a = same[-1]
            out.append({**j, "id": a["id"], "carried": a["report_date"] != td, "since": a["report_date"],
                        "price": a["price"], "flip": a["flip"] or j["flip"], "flip_up": a["flip_up"], "flip_down": a["flip_down"],
                        "expires": a["expires"] or j["expires"], "confidence": a["confidence"] or j["confidence"],
                        # 沿用的判断沿用当初记下的底数（它和当初的价位、翻转价对应）
                        "base_rate": a.get("base_rate"), "flip_rate": a.get("flip_rate"), "base_n": a.get("base_n")})
            continue
        for a in act:  # 方向变了：旧判断由新判断取代
            mem.set_review(a["id"], "翻转", f"{td} 早报改为{j['view']}", j["price"])
        cid = mem.record_call(j["symbol"], j["name"], j["view"], j["price"], j["days"], f"早报｜{j['reason']}｜翻转：{j['flip']}",
                              j["confidence"], due=j["due"], source="morning", flip=j["flip"], flip_up=j["flip_up"],
                              flip_down=j["flip_down"], expires=j["expires"], report_date=td, price_date=j.get("as_of"),
                              base_rate=j.get("base_rate"), flip_rate=j.get("flip_rate"), base_n=j.get("base_n"))
        out.append({**j, "id": cid, "carried": False, "since": td})
    return out


def morning(mem, snapshot: dict, vps: dict | None = None, today: date | None = None, write: bool = True, bars: dict | None = None) -> list[dict]:
    """从早报快照生成判断：自选股；没有自选就用每个市场第一个指数。bars：各标的日线（算历史底数用，可无）。"""
    rows = list(snapshot.get("watchlist") or [])
    if not rows:
        rows = [r[0] for r in (snapshot.get("indices") or {}).values() if r]
    js = []
    for r in rows:
        j = judge(r["symbol"], r.get("name"), r.get("data") or {}, (vps or {}).get(r["symbol"]), today)
        if j:
            js.append(j)
    attach_base(js, bars)
    return record(mem, js, today) if write else js


def review(mem, snapshot: dict, today: date | None = None) -> list[dict]:
    """晚报复盘：今天仍在跟踪（或今天到期 / 今天被替换）的早报判断，对照最新收盘。"""
    today = today or datetime.now(CN_TZ).date()
    td = today.isoformat()
    data = {r["symbol"]: r.get("data") or {} for rows in (snapshot.get("indices") or {}).values() for r in rows}
    data.update({r["symbol"]: r.get("data") or {} for r in snapshot.get("watchlist") or []})
    calls = mem.calls_where("source='morning' AND report_date<=? AND (due>=? OR status='closed' AND end_date>=?) "
                            "AND (review IS NULL OR review NOT IN ('失效','翻转') OR substr(review_at,1,10)=?)", (td, td, td, td))
    out = []
    for c in calls:
        d = data.get(c["symbol"]) or {}
        close, day = d.get("close"), d.get("date")
        r = {"id": c["id"], "symbol": c["symbol"], "name": c["name"], "view": c["view"], "since": c["report_date"],
             "price": c["price"], "flip": c["flip"], "expires": c["expires"], "close": close}
        if c.get("review") == "翻转":
            r.update(result="翻转", note=c["review_note"] or "早报已改判")
            out.append(r)
            continue
        if close is None or not day or (c.get("price_date") and day <= c["price_date"]):
            r.update(result="待定", note="今天还没有新的收盘数据")
            out.append(r)
            continue
        chg = (close / c["price"] - 1) * 100 if c["price"] else 0.0
        r["chg"] = round(chg, 2)
        up, dn = c.get("flip_up"), c.get("flip_down")
        if (dn and close < dn) or (up and close > up):
            res, note = "失效", f"收盘 {close:g} {'跌破' if dn and close < dn else '站上'} 翻转价 {dn if dn and close < dn else up:g}"
        elif c["view"] == "震荡":
            res, note = ("站住", f"收盘在判断价 ±{BAND:g}% 内") if abs(chg) <= BAND else ("承压", f"偏离判断价 {chg:+.2f}%，未破翻转价")
        else:
            good = chg >= 0 if c["view"] == "看涨" else chg <= 0
            res, note = ("站住", f"较判断价 {chg:+.2f}%") if good else ("承压", f"较判断价 {chg:+.2f}%，方向不利但未破翻转价")
        r.update(result=res, note=note)
        mem.set_review(c["id"], res, note, close)
        out.append(r)
    return out


def render_morning(js: list[dict]) -> list[str]:
    if not js:
        return []
    from .baserate import fmt
    has_base = any(j.get("base_rate") is not None or j.get("flip_rate") is not None for j in js)
    if has_base:
        lines = ["## 今日判断", "", "| 标的 | 判断 | 置信度 | 历史底数 | 失效时间 | 翻转条件 | 依据 |", "|---|---|---:|---|---|---|---|"]
    else:
        lines = ["## 今日判断", "", "| 标的 | 判断 | 置信度 | 失效时间 | 翻转条件 | 依据 |", "|---|---|---:|---|---|---|"]
    for j in js:
        tag = f"（沿用 {j['since']}）" if j.get("carried") else ""
        base = f" {fmt(j)} |" if has_base else ""
        lines.append(f"| {j['name']} | {j['view']}{tag} | {j['confidence']}% |{base} {j['expires']} | {j['flip']} | {j['reason']} |")
    lines += ["", "判断已记进账本，晚报逐条复盘、到期自动结算。择时判断经回测不具统计显著性，翻转条件比方向更重要。"]
    if has_base:
        lines.append("历史底数：过去约 250 个交易日里，按 ATR 归一的同等幅度在判断期限内出现的比例（判断成立按到期收盘、失效价按盘中触及）——"
                     "是没有判断力时的参照，不是预测概率。")
    lines.append("")
    return lines


def base_stats(mem) -> dict | None:
    """已结算的早报判断：命中率 vs 平均历史底数（只算记了底数的）。"""
    try:
        from . import baserate
        return baserate.compare(mem.calls_where("status='closed' AND source='morning' AND base_rate IS NOT NULL"))
    except Exception:
        return None


def render_review(rs: list[dict], stats: dict | None = None) -> list[str]:
    from .baserate import render_compare
    tail = [render_compare(stats), ""] if stats else []
    if not rs:
        return ["## 判断复盘", "", "今天没有在跟踪的早报判断。", ""] + tail
    cnt = {k: sum(r["result"] == k for r in rs) for k in ("站住", "承压", "失效", "翻转", "待定")}
    head = " / ".join(f"{k} {v}" for k, v in cnt.items() if v)
    lines = ["## 判断复盘", "", f"对照早报：{head}。", "", "| 标的 | 早报判断 | 判断价 | 收盘 | 变动 | 结果 | 说明 |", "|---|---|---:|---:|---:|---|---|"]
    for r in rs:
        ch = f"{r['chg']:+.2f}%" if r.get("chg") is not None else "—"
        lines.append(f"| {r['name']} | {r['view']}（{r['since']}） | {r['price']:g} | {r['close'] if r.get('close') is not None else '—'} | {ch} | {r['result']} | {r['note']} |")
    bad = [r for r in rs if r["result"] == "失效"]
    if bad:
        lines += ["", "失效的判断不再跟踪：" + "、".join(f"{r['name']}（{r['flip']}）" for r in bad) + "。明早重新给判断。"]
    lines.append("")
    return lines + tail
