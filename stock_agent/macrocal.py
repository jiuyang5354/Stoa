"""重大宏观数据日程 + 早报用的「偏热 / 符合 / 偏冷」三情景。

日程来源（2026-10-31 取，2025–2027 年的 CPI/非农/议息为官方日期）：
- 美国 CPI、非农：BLS 官方发布表 https://www.bls.gov/schedule/news_release/cpi.htm 、empsit.htm（美东 08:30）
- FOMC 议息：美联储 https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm（第二天美东 14:00 公布决议）
- 中国官方 PMI：国家统计局每月最后一天 09:30（规则推算，节假日可能调整）
- 中国 CPI/PPI：国家统计局每月 9 日前后 09:30（规则推算，标「约」）
- 美国 ISM 制造业 PMI：每月第一个工作日美东 10:00（规则推算）
表外年份按规则推算；config.yaml 的 macro_events 可以补充或覆盖（[{date: 2026-11-10, kind: us_cpi, name: …, time: "21:30"}]）。

情景是方向性的经验映射（数据比预期热/冷时，利率、美元、风险偏好通常怎么走），不是预测；
另外用持仓自己的历史行情算「过去同类数据公布日的平均波动」，给影响幅度一个有出处的参照。
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

CN = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")

US_CPI = ["2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-13", "2025-06-11", "2025-07-15", "2025-08-12", "2025-09-11",
          "2025-10-24", "2025-12-18", "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14",
          "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13",
          "2027-05-12", "2027-06-10", "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10"]
US_NFP = ["2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04", "2025-05-02", "2025-06-06", "2025-07-03", "2025-08-01", "2025-09-05",
          "2025-11-20", "2025-12-16", "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05", "2026-07-02",
          "2026-08-07", "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02",
          "2027-05-07", "2027-06-04", "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03"]
# FOMC 决议日（会议第二天）
FOMC = ["2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
        "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
        "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"]
OFFICIAL_SRC = {"us_cpi": "https://www.bls.gov/schedule/news_release/cpi.htm", "us_nfp": "https://www.bls.gov/schedule/news_release/empsit.htm",
                "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"}

KINDS = {
    "us_cpi": {"name": "美国 CPI", "country": "美国", "labels": ("偏热", "符合", "偏冷")},
    "us_nfp": {"name": "美国非农就业", "country": "美国", "labels": ("偏热", "符合", "偏冷")},
    "fomc": {"name": "美联储议息（FOMC）", "country": "美国", "labels": ("偏热（偏鹰）", "符合", "偏冷（偏鸽）")},
    "us_ism": {"name": "美国 ISM 制造业 PMI", "country": "美国", "labels": ("偏热", "符合", "偏冷")},
    "cn_pmi": {"name": "中国官方制造业 PMI", "country": "中国", "labels": ("偏热", "符合", "偏冷")},
    "cn_cpi": {"name": "中国 CPI / PPI", "country": "中国", "labels": ("偏热", "符合", "偏冷")},
}

# 三情景对各市场的方向（+1 偏利好、-1 偏利空、0 有限）与一句话机制。顺序：偏热 / 符合 / 偏冷
_US_INFL = {
    "A股": ((-1, "美元走强、人民币承压，外资偏谨慎"), (0, "影响有限，回到国内主线"), (1, "美元走弱，外资流入环境改善")),
    "港股": ((-1, "美债收益率上行压估值，港股科技弹性大、跌得更多"), (0, "影响有限"), (1, "流动性预期改善，港股科技弹性最大")),
    "美股": ((-1, "降息预期降温、收益率上行，成长/纳指先承压"), (0, "不确定性落地，波动收窄"), (1, "降息预期升温，成长股受益；若冷到像衰退则转防御")),
}
SCENARIO_MAP = {
    "us_cpi": _US_INFL,
    "us_nfp": {
        "A股": ((-1, "美元走强，外资偏谨慎"), (0, "影响有限"), (1, "美元走弱；但就业过冷引发衰退担忧时全球风险偏好下降")),
        "港股": ((-1, "收益率上行，港股科技承压"), (0, "影响有限"), (1, "降息预期升温利好；过冷则担忧需求")),
        "美股": ((0, "经济强但降息推迟，指数分化：周期强、成长弱"), (0, "符合预期，关注薪资增速"), (-1, "大幅偏冷→衰退交易，小盘与周期承压，防御占优")),
    },
    "fomc": {
        "A股": ((-1, "美元偏强，压制外资风险偏好"), (0, "按预期落地，关注点阵图/发布会措辞"), (1, "美元走弱，利好外资回流")),
        "港股": ((-1, "港股对美元利率最敏感，偏鹰时承压"), (0, "影响有限"), (1, "偏鸽时港股弹性最大")),
        "美股": ((-1, "收益率与美元上行，高估值成长股回调"), (0, "落地后波动下降"), (1, "风险偏好回升，成长与小盘受益")),
    },
    "us_ism": {
        "A股": ((0, "外需预期改善，出口链受益"), (0, "影响有限"), (0, "外需担忧，出口链承压")),
        "港股": ((0, "影响有限"), (0, "影响有限"), (0, "影响有限")),
        "美股": ((1, "周期股受益；但若价格分项高则加息担忧"), (0, "影响有限"), (-1, "制造业收缩担忧，周期承压")),
    },
    "cn_pmi": {
        "A股": ((1, "景气回升，周期/顺周期板块受益"), (0, "符合预期，关注新订单与价格分项"), (-1, "景气走弱压制周期，但稳增长政策预期升温，高股息/防御占优")),
        "港股": ((1, "中资股盈利预期改善"), (0, "影响有限"), (-1, "中资股承压，等政策加码")),
        "美股": ((0, "影响有限"), (0, "影响有限"), (0, "影响有限")),
    },
    "cn_cpi": {
        "A股": ((0, "消费/上游涨价受益，但货币宽松空间收窄"), (0, "影响有限"), (0, "通缩担忧加深，宽松预期升温，利好成长与高股息")),
        "港股": ((0, "影响有限"), (0, "影响有限"), (0, "宽松预期利好中资股估值")),
        "美股": ((0, "影响有限"), (0, "影响有限"), (0, "影响有限")),
    },
}
FOCUS = {"us_cpi": "核心 CPI 环比（市场最看这个）与住房分项", "us_nfp": "新增就业、失业率与平均时薪", "fomc": "利率决定、点阵图与发布会对下一次的指引",
         "us_ism": "新订单与价格分项", "cn_pmi": "是否站上 50、新订单与出厂价格分项", "cn_cpi": "核心 CPI 与 PPI 同比方向"}


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _first_bday(y: int, m: int) -> date:
    d = date(y, m, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _bj(d: date, hh: int, mm: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, dtime(hh, mm), tz).astimezone(CN)


def _ev(kind: str, local_day: date, at_bj: datetime, src: str, approx: bool = False) -> dict:
    k = KINDS[kind]
    return {"kind": kind, "name": k["name"], "country": k["country"], "local_date": local_day.isoformat(),
            "date": at_bj.date().isoformat(), "time_bj": at_bj.strftime("%m-%d %H:%M"), "at": at_bj.isoformat(),
            "source": src, "approx": approx, "focus": FOCUS.get(kind, "")}


def events(start: date, end: date, extra: list | None = None) -> list[dict]:
    """start–end（按北京时间日期，含两端）之间的重大数据。"""
    out = []
    for s in US_CPI:
        out.append(_ev("us_cpi", _d(s), _bj(_d(s), 8, 30, ET), OFFICIAL_SRC["us_cpi"]))
    for y in range(start.year - 1, end.year + 2):  # 表外年份：CPI 约每月 12 日（推算）
        if not any(x[:4] == str(y) for x in US_CPI):
            for m in range(1, 13):
                d = date(y, m, 12)
                while d.weekday() >= 5:
                    d += timedelta(days=1)
                out.append(_ev("us_cpi", d, _bj(d, 8, 30, ET), "规则推算（约每月 12 日）", approx=True))
    for y in range(start.year - 1, end.year + 2):  # 表外：每月第一个周五（推算）
        for m in range(1, 13):
            d = date(y, m, 1)
            d += timedelta(days=(4 - d.weekday()) % 7)
            if not any(x[:4] == str(y) for x in US_NFP):  # 官方表覆盖的年份不推算（2025 年停摆期间非农延后，推算会错）
                out.append(_ev("us_nfp", d, _bj(d, 8, 30, ET), "规则推算（每月第一个周五）", approx=True))
            ism = _first_bday(y, m)
            out.append(_ev("us_ism", ism, _bj(ism, 10, 0, ET), "规则推算（每月第一个工作日）", approx=True))
            last = date(y, m, calendar.monthrange(y, m)[1])
            out.append(_ev("cn_pmi", last, _bj(last, 9, 30, CN), "规则推算（国家统计局，每月最后一天）", approx=True))
            c9 = date(y, m, 9)
            out.append(_ev("cn_cpi", c9, _bj(c9, 9, 30, CN), "规则推算（国家统计局，约每月 9 日）", approx=True))
    for s in US_NFP:
        out.append(_ev("us_nfp", _d(s), _bj(_d(s), 8, 30, ET), OFFICIAL_SRC["us_nfp"]))
    for s in FOMC:
        out.append(_ev("fomc", _d(s), _bj(_d(s), 14, 0, ET), OFFICIAL_SRC["fomc"]))
    for e in extra or []:  # 用户补充 / 覆盖（北京时间）
        try:
            kind = e.get("kind") if e.get("kind") in KINDS else "us_cpi"
            d = date.fromisoformat(str(e["date"])[:10])
            hh, mm = (int(x) for x in str(e.get("time") or "09:30").split(":")[:2])
            ev = _ev(kind, d, datetime.combine(d, dtime(hh, mm), CN), "用户配置")
            if e.get("name"):
                ev["name"] = str(e["name"])
            out = [x for x in out if not (x["kind"] == kind and abs((date.fromisoformat(x["date"]) - d).days) <= 3)]
            out.append(ev)
        except Exception:
            continue
    seen, res = set(), []
    for e in sorted(out, key=lambda x: x["at"]):
        key = (e["kind"], e["date"])
        if start.isoformat() <= e["date"] <= end.isoformat() and key not in seen:
            seen.add(key)
            res.append(e)
    return res


def lookahead(today: date) -> date:
    """早报看多远：到下一个工作日为止（周五看到下周一）。"""
    d = today + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def past_dates(kind: str, before: date, n: int = 12) -> list[str]:
    """过去 n 次同类数据的「受影响交易日」（北京时间日期；美国晚间公布的，A股/港股反应在第二天，取 local_date 当日美股、次日亚洲）。"""
    evs = [e for e in events(before - timedelta(days=500), before - timedelta(days=1)) if e["kind"] == kind]
    return [e["at"] for e in evs][-n:]


def reaction(closes, kind: str, market: str, before: date, n: int = 12) -> dict | None:
    """持仓在过去 n 次同类数据公布后第一个交易日的平均绝对波动，对比平时。closes: pandas Series（日期索引）。"""
    import pandas as pd
    if closes is None or len(closes) < 60:
        return None
    s = closes.dropna()
    r = s.pct_change().dropna() * 100
    hits = []
    for at in past_dates(kind, before, n):
        t = datetime.fromisoformat(at)
        # 北京时间 16:00 后公布的（美国数据）：美股当天（美东日期）、亚洲次日；之前公布的：当天
        if market == "美股":
            day = t.astimezone(ET).date()
        else:
            day = t.date() + (timedelta(days=1) if t.hour >= 16 else timedelta(0))
        idx = r.index[r.index >= pd.Timestamp(day)]
        if len(idx):
            hits.append(abs(float(r.loc[idx[0]])))
    if len(hits) < 4:
        return None
    normal = float(r.tail(250).abs().mean())
    avg = sum(hits) / len(hits)
    return {"n": len(hits), "avg_abs%": round(avg, 2), "normal_abs%": round(normal, 2), "ratio": round(avg / normal, 2) if normal else None}


def _market(sym: str) -> str:
    s = sym.upper()
    return "A股" if s.endswith((".SS", ".SZ", ".BJ")) else "港股" if s.endswith(".HK") or s in ("^HSI", "HSTECH.HK") else "美股"


def scenarios(ev: dict, holdings: list[dict], closes: dict | None = None, today: date | None = None) -> dict:
    """holdings: [{symbol, name}]。返回三情景：每个情景下各市场方向 + 机制，以及持仓各自的方向与历史波动参照。"""
    today = today or date.today()
    mp = SCENARIO_MAP.get(ev["kind"], {})
    labels = KINDS[ev["kind"]]["labels"]
    sign = {1: "偏利好", -1: "偏利空", 0: "影响有限"}
    out = []
    for i, lab in enumerate(labels):
        mk = {m: {"dir": sign[v[i][0]], "why": v[i][1]} for m, v in mp.items()}
        hs = []
        for h in holdings:
            m = _market(h["symbol"])
            d, why = (mp.get(m) or ((0, ""),) * 3)[i]
            hs.append({"symbol": h["symbol"], "name": h.get("name") or h["symbol"], "market": m, "dir": sign[d], "why": why})
        out.append({"label": lab, "markets": mk, "holdings": hs})
    react = []
    for h in holdings:
        r = reaction((closes or {}).get(h["symbol"]), ev["kind"], _market(h["symbol"]), today)
        if r:
            react.append({"symbol": h["symbol"], "name": h.get("name") or h["symbol"], **r})
    return {"event": ev, "scenarios": out, "reaction": react}


def render(items: list[dict]) -> list[str]:
    """早报里的「重大数据前瞻」小节（Markdown 行）。"""
    if not items:
        return []
    lines = ["## 重大数据前瞻", ""]
    for it in items:
        e = it["event"]
        lines.append(f"### {e['name']} · 北京时间 {e['time_bj']}{'（约）' if e.get('approx') else ''}")
        lines.append(f"看点：{e['focus']}。")
        lines.append("")
        lines.append("| 情景 | A股 | 港股 | 美股 | 持仓 |")
        lines.append("|---|---|---|---|---|")
        for sc in it["scenarios"]:
            m = sc["markets"]
            cell = lambda k: ("—" if k not in m else m[k]["dir"] if m[k]["why"] in ("", m[k]["dir"]) else f"{m[k]['dir']}：{m[k]['why']}")  # noqa: E731
            hold = "；".join(f"{h['name']} {h['dir']}" for h in sc["holdings"]) or "—"
            lines.append(f"| {sc['label']} | {cell('A股')} | {cell('港股')} | {cell('美股')} | {hold} |")
        if it.get("reaction"):
            lines.append("")
            lines.append("历史参照（过去同类数据公布后第一个交易日的平均波动 vs 平时）：" + "；".join(
                f"{r['name']} ±{r['avg_abs%']}% vs ±{r['normal_abs%']}%（{r['n']} 次）" for r in it["reaction"]))
        lines.append(f"日程来源：{e['source']}")
        lines.append("")
    lines.append("情景是方向性的经验映射，不是预测；结果揭晓前不要押方向。")
    lines.append("")
    return lines
