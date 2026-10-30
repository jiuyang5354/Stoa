"""公司事件、筹码与情绪数据：A股解禁/业绩预告/机构调研/增减持/回购/质押/两融/股东户数/涨停情绪/板块资金，
美股 SEC Form 4 内部人交易与 FINRA 空头成交占比。

每个函数返回带 source / url / as_of / sources / warnings 的 dict，和 marketdata 的口径一致，
「确实没有」返回空列表并写 note，「接口坏了」写 warnings 或 error，不混在一起。
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from .marketdata import DC, EM_REF, _err, _f, _fetch, now_cn, parse, src

from .research import SEC_UA
EM_DC_PAGE = "https://data.eastmoney.com/"


def _dc(report: str, flt: str, sort: str, n: int = 10, order: str = "-1", ttl: int = 1800) -> list[dict]:
    j = _fetch(DC, params={"reportName": report, "columns": "ALL", "filter": flt, "pageNumber": 1, "pageSize": n,
                           "sortColumns": sort, "sortTypes": order, "source": "WEB", "client": "WEB"}, headers=EM_REF, ttl=ttl)
    return (j.get("result") or {}).get("data") or []


def _d(x) -> str:
    return str(x or "")[:10]


def _a_only(symbol: str, what: str):
    p = parse(symbol)
    if p["market"] != "A":
        return p, {"symbol": p["yahoo"], "error": f"{what}只适用于 A股"}
    return p, None


# ---------------------------------------------------------------- A股公司事件
def _lockups(code):
    today = now_cn().strftime("%Y-%m-%d")
    end = (now_cn() + timedelta(days=180)).strftime("%Y-%m-%d")
    up = _dc("RPT_LIFT_STAGE", f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{today}\')(FREE_DATE<=\'{end}\')', "FREE_DATE", 10, "1")
    past = _dc("RPT_LIFT_STAGE", f'(SECURITY_CODE="{code}")(FREE_DATE<\'{today}\')', "FREE_DATE", 3)
    row = lambda r: {"date": _d(r.get("FREE_DATE")), "type": r.get("FREE_SHARES_TYPE"), "shares_wan": _f(r.get("CURRENT_FREE_SHARES") or r.get("ABLE_FREE_SHARES") or r.get("FREE_SHARES"), 1), "ratio_pct": _f((_f(r.get("FREE_RATIO")) or 0) * 100, 3) if r.get("FREE_RATIO") is not None else None,
                     "mktcap_yi": _f((_f(r.get("LIFT_MARKET_CAP")) or 0) / 1e4, 2) if r.get("LIFT_MARKET_CAP") is not None else None}
    return {"upcoming_180d": [row(r) for r in up], "recent": [row(r) for r in past]}


def _forecasts(code):
    out = []
    for r in _dc("RPT_PUBLIC_OP_NEWPREDICT", f'(SECURITY_CODE="{code}")', "NOTICE_DATE", 8):
        out.append({"notice_date": _d(r.get("NOTICE_DATE")), "period": _d(r.get("REPORT_DATE")), "indicator": r.get("PREDICT_FINANCE"),
                    "type": r.get("PREDICT_TYPE"), "chg_pct_low": _f(r.get("ADD_AMP_LOWER"), 1), "chg_pct_high": _f(r.get("ADD_AMP_UPPER"), 1),
                    "content": (r.get("PREDICT_CONTENT") or "")[:160]})
    return out


def _surveys(code):
    return [{"notice_date": _d(r.get("NOTICE_DATE")), "date": _d(r.get("RECEIVE_START_DATE")), "orgs": _f(r.get("SUM")), "way": r.get("RECEIVE_WAY_EXPLAIN")}
            for r in _dc("RPT_ORG_SURVEYNEW", f'(SECURITY_CODE="{code}")(IS_SOURCE="1")(NUMBERNEW="1")', "NOTICE_DATE", 6)]


def _holder_trades(code):
    return [{"notice_date": _d(r.get("NOTICE_DATE")), "holder": r.get("HOLDER_NAME"), "direction": r.get("DIRECTION"),
             "change_wan": _f(r.get("CHANGE_NUM_SYMBOL") if r.get("CHANGE_NUM_SYMBOL") is not None else r.get("CHANGE_NUM"), 2),
             "pct_of_float": _f(r.get("CHANGE_FREE_RATIO"), 3), "after_pct_total": _f(r.get("HOLD_RATIO"), 2),
             "period": f"{_d(r.get('START_DATE'))}~{_d(r.get('END_DATE'))}"}
            for r in _dc("RPT_SHARE_HOLDER_INCREASE", f'(SECURITY_CODE="{code}")', "NOTICE_DATE", 8)]


BUYBACK_PROGRESS = {"001": "董事会预案", "002": "股东大会通过", "003": "股东大会否决", "004": "实施中", "005": "停止实施", "006": "完成实施"}


def _buybacks(code):
    return [{"latest_notice": _d(r.get("UPDATEDATE") or r.get("UPD")), "progress": BUYBACK_PROGRESS.get(str(r.get("REPURPROGRESS")), r.get("REPURPROGRESS")),
             "plan": f"{_d(r.get('REPURSTARTDATE'))}~{_d(r.get('REPURENDDATE'))}", "price_cap": _f(r.get("REPURPRICECAP"), 2),
             "amount_yi": [_f((_f(r.get("REPURAMOUNTLOWER")) or 0) / 1e8, 2), _f((_f(r.get("REPURAMOUNTLIMIT")) or 0) / 1e8, 2)],
             "done_amount_yi": _f((_f(r.get("REPURAMOUNT")) or 0) / 1e8, 2), "objective": (r.get("REPUROBJECTIVE") or "")[:80]}
            for r in _dc("RPTA_WEB_GETHGLIST_NEW", f'(DIM_SCODE="{code}")', "UPD", 3)]


def _pledge(code):
    rs = _dc("RPT_CSDC_LIST", f'(SECURITY_CODE="{code}")', "TRADE_DATE", 1)
    if not rs:
        return None
    r = rs[0]
    return {"date": _d(r.get("TRADE_DATE")), "pledge_ratio_pct": _f(r.get("PLEDGE_RATIO"), 2), "deals": _f(r.get("PLEDGE_DEAL_NUM")),
            "pledged_wan_shares": _f(r.get("REPURCHASE_BALANCE"), 1)}


A_EVENT_KINDS = {"lockup": ("限售解禁", _lockups), "forecast": ("业绩预告", _forecasts), "survey": ("机构调研", _surveys),
                 "holder": ("股东增减持", _holder_trades), "buyback": ("回购", _buybacks), "pledge": ("股权质押", _pledge)}


def a_events(symbol: str, kinds: list[str] | None = None) -> dict:
    p, bad = _a_only(symbol, "A股公司事件")
    if bad:
        return bad
    ks = [k for k in (kinds or A_EVENT_KINDS) if k in A_EVENT_KINDS] or list(A_EVENT_KINDS)
    out = {"symbol": p["yahoo"], "source": "东方财富数据中心", "url": f"https://data.eastmoney.com/stockdata/{p['code']}.html",
           "as_of": now_cn().strftime("%Y-%m-%d"), "warnings": [], "sources": []}
    with ThreadPoolExecutor(3) as ex:  # 东财数据中心限流：并发别超过 3
        futs = {k: ex.submit(A_EVENT_KINDS[k][1], p["code"]) for k in ks}
    for k, fu in futs.items():
        name = A_EVENT_KINDS[k][0]
        try:
            out[k] = fu.result()
            out["sources"].append(src(f"东方财富 · {name}", out["url"], out["as_of"]))
        except Exception as e:
            out[k] = None
            out["warnings"].append(f"{name}取不到：{_err(e)}")
            out["sources"].append(src(f"东方财富 · {name}", out["url"], ok=False, error=_err(e)))
    flags = []
    lk = (out.get("lockup") or {}).get("upcoming_180d") or []
    big = [x for x in lk if (x.get("ratio_pct") or 0) >= 1]
    if big:
        flags.append(f"{big[0]['date']} 解禁占总股本 {big[0]['ratio_pct']}%")
    sells = [x for x in out.get("holder") or [] if x.get("direction") == "减持" and x["notice_date"] >= (now_cn() - timedelta(days=90)).strftime("%Y-%m-%d")]
    if sells:
        flags.append(f"近 90 天有 {len(sells)} 条股东减持公告")
    pl = out.get("pledge") or {}
    if (pl.get("pledge_ratio_pct") or 0) >= 20:
        flags.append(f"质押比例 {pl['pledge_ratio_pct']}%（≥20%）")
    out["flags"] = flags
    return out


# ---------------------------------------------------------------- A股筹码：两融 + 股东户数
def a_chips(symbol: str, days: int = 10) -> dict:
    p, bad = _a_only(symbol, "两融与股东户数")
    if bad:
        return bad
    out = {"symbol": p["yahoo"], "source": "东方财富数据中心", "url": f"https://data.eastmoney.com/rzrq/detail/{p['code']}.html", "warnings": [], "sources": []}
    try:
        m = [{"date": _d(r.get("DATE")), "rz_balance_yi": _f((_f(r.get("RZYE")) or 0) / 1e8, 2), "rz_buy_yi": _f((_f(r.get("RZMRE")) or 0) / 1e8, 2),
              "rq_balance_yi": _f((_f(r.get("RQYE")) or 0) / 1e8, 3)} for r in _dc("RPTA_WEB_RZRQ_GGMX", f'(SCODE="{p["code"]}")', "DATE", max(2, min(days, 30)))]
        out["margin"] = m
        if len(m) >= 2 and m[-1]["rz_balance_yi"]:
            out["margin_change_pct"] = _f((m[0]["rz_balance_yi"] / m[-1]["rz_balance_yi"] - 1) * 100, 2)
        out["as_of"] = m[0]["date"] if m else None
        out["sources"].append(src("东方财富 · 融资融券", out["url"], out.get("as_of") or ""))
    except Exception as e:
        out["margin"] = None
        out["warnings"].append(f"融资融券取不到：{_err(e)}（非两融标的也会没有）")
    try:
        h = [{"date": _d(r.get("END_DATE")), "holders": _f(r.get("HOLDER_NUM")), "chg_pct": _f(r.get("HOLDER_NUM_RATIO"), 2),
              "avg_float_shares": _f(r.get("AVG_FREE_SHARES"))} for r in _dc("RPT_HOLDERNUMLATEST", f'(SECURITY_CODE="{p["code"]}")', "END_DATE", 4)]
        out["holders"] = h
        out["sources"].append(src("东方财富 · 股东户数", f"https://data.eastmoney.com/gdhs/detail/{p['code']}.html", h[0]["date"] if h else ""))
    except Exception as e:
        out["holders"] = None
        out["warnings"].append(f"股东户数取不到：{_err(e)}")
    out["note"] = "股东户数减少=筹码趋于集中，只是参考；融资余额是杠杆资金存量，不等于主力动向"
    return out


# ---------------------------------------------------------------- A股情绪：涨停/炸板/跌停 + 板块资金
ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _zt(endpoint, date, sort="fbt:asc"):
    j = _fetch(f"https://push2ex.eastmoney.com/{endpoint}", params={"ut": ZT_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 10000, "sort": sort, "date": date},
               headers={**EM_REF, "Referer": "https://quote.eastmoney.com/"}, ttl=300)
    return (j.get("data") or {}).get("pool") or []


BOARD_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3"}


def _boards(kind="industry", n=5):
    j = _fetch("https://push2.eastmoney.com/api/qt/clist/get",
               params={"pn": 1, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f62", "fs": BOARD_FS[kind], "fields": "f12,f14,f3,f62,f184,f204"},
               headers=EM_REF, ttl=300)
    rows = [{"name": x.get("f14"), "chg_pct": _f(x.get("f3"), 2), "main_net_yi": _f((_f(x.get("f62")) or 0) / 1e8, 2), "leader": x.get("f204")}
            for x in ((j.get("data") or {}).get("diff") or []) if isinstance(x, dict)]
    rows = [r for r in rows if r["main_net_yi"] is not None]
    rows.sort(key=lambda r: r["main_net_yi"], reverse=True)
    return rows[:n], rows[-n:][::-1]


def a_mood(date: str | None = None) -> dict:
    """A股当日（或最近交易日）打板情绪 + 板块主力资金前后五。"""
    d = (date or now_cn().strftime("%Y%m%d")).replace("-", "")
    out = {"source": "东方财富 · 涨停板行情中心", "url": "https://quote.eastmoney.com/ztb/", "warnings": [], "sources": []}
    zt = zb = dt = None
    for back in range(0, 8):  # 非交易日往前找
        day = (datetime.strptime(d, "%Y%m%d") - timedelta(days=back)).strftime("%Y%m%d")
        try:
            zt = _zt("getTopicZTPool", day)
        except Exception as e:
            out["warnings"].append(f"涨停池取不到：{_err(e)}")
            break
        if zt:
            d = day
            break
    if zt:
        try:
            zb, dt = _zt("getTopicZBPool", d), _zt("getTopicDTPool", d, "fund:asc")
        except Exception as e:
            out["warnings"].append(f"炸板/跌停池取不到：{_err(e)}")
        ladder = {}
        for x in zt:
            ladder[x.get("lbc", 1)] = ladder.get(x.get("lbc", 1), 0) + 1
        top = sorted(zt, key=lambda x: (-(x.get("lbc") or 1), x.get("fbt") or 0))[:5]
        ind = {}
        for x in zt:
            ind[x.get("hybk") or "其他"] = ind.get(x.get("hybk") or "其他", 0) + 1
        nz, nb = len(zt), len(zb or [])
        out.update({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "limit_up": nz, "broken": nb, "limit_down": len(dt or []) if dt is not None else None,
                    "break_rate_pct": _f(nb / (nz + nb) * 100, 1) if nz + nb else None, "max_height": max(ladder) if ladder else 0,
                    "ladder": {f"{k}板": v for k, v in sorted(ladder.items())},
                    "leaders": [{"name": x.get("n"), "code": x.get("c"), "boards": x.get("lbc"), "industry": x.get("hybk")} for x in top],
                    "hot_industries": sorted(ind.items(), key=lambda kv: -kv[1])[:5]})
        out["as_of"] = out["date"]
        out["sources"].append(src("东方财富 · 涨停/炸板/跌停池", out["url"], out["date"]))
    for kind, label in (("industry", "行业"), ("concept", "概念")):
        try:
            inflow, outflow = _boards(kind)
            out[f"{kind}_inflow"], out[f"{kind}_outflow"] = inflow, outflow
            out["sources"].append(src(f"东方财富 · {label}板块资金流", "https://data.eastmoney.com/bkzj/hy.html", now_cn().strftime("%Y-%m-%d")))
        except Exception as e:
            out["warnings"].append(f"{label}板块资金流取不到：{_err(e)}")
    if "as_of" not in out:
        out["as_of"] = now_cn().strftime("%Y-%m-%d")
    return out


# ---------------------------------------------------------------- 美股：Form 4 内部人交易
F4_CODES = {"P": "公开市场买入", "S": "公开市场卖出", "A": "公司授予", "M": "期权行权", "F": "缴税扣股", "G": "赠与", "D": "卖回公司", "X": "行权", "C": "转换"}
_TICKERS = {"at": 0, "map": {}}


def _cik(ticker: str) -> str | None:
    if time.time() - _TICKERS["at"] > 86400 or not _TICKERS["map"]:
        j = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=20).json()
        _TICKERS["map"] = {str(v["ticker"]).upper(): str(v["cik_str"]).zfill(10) for v in j.values()}
        _TICKERS["at"] = time.time()
    return _TICKERS["map"].get(ticker.upper().replace(".", "-"))


def _xv(el, path):
    n = el.find(path) if el is not None else None
    if n is None:
        return None
    v = n.find("value")
    s = ((v.text if v is not None else n.text) or "").strip()
    return s or None


def _form4(url: str) -> list[dict]:
    root = ET.fromstring(requests.get(url, headers=SEC_UA, timeout=20).content)
    owner = _xv(root, "reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    role = ", ".join(filter(None, [("董事" if _xv(rel, "isDirector") in ("1", "true") else None), _xv(rel, "officerTitle"),
                                   ("10%股东" if _xv(rel, "isTenPercentOwner") in ("1", "true") else None)])) if rel is not None else ""
    plan = _xv(root, "aff10b5One") in ("1", "true")
    out = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _xv(t, "transactionCoding/transactionCode")
        sh, px = _f(_xv(t, "transactionAmounts/transactionShares")), _f(_xv(t, "transactionAmounts/transactionPricePerShare"))
        out.append({"date": _xv(t, "transactionDate"), "owner": owner, "role": role, "code": code, "type": F4_CODES.get(code, code),
                    "ad": _xv(t, "transactionAmounts/transactionAcquiredDisposedCode"), "shares": sh, "price": px,
                    "value_usd": round(sh * px) if sh and px else None, "owned_after": _f(_xv(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                    "plan_10b5_1": plan})
    return out


def us_insiders(symbol: str, days: int = 90, max_filings: int = 15) -> dict:
    p = parse(symbol)
    if p["market"] != "US":
        return {"symbol": p["yahoo"], "error": "内部人交易（SEC Form 4）只适用于美股"}
    t = p["yahoo"]
    try:
        cik = _cik(t)
    except Exception as e:
        return {"symbol": t, "error": f"SEC 代码表取不到：{_err(e)}"}
    if not cik:
        return {"symbol": t, "error": "SEC 代码表里没有这只股票"}
    page = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=40"
    try:
        f = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_UA, timeout=20).json()["filings"]["recent"]
    except Exception as e:
        return {"symbol": t, "error": f"SEC 申报列表取不到：{_err(e)}", "sources": [src("SEC EDGAR · Form 4", page, ok=False, error=_err(e))]}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    idx = [i for i, x in enumerate(f["form"]) if x == "4" and f["filingDate"][i] >= since]
    urls = [(f["filingDate"][i], f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['accessionNumber'][i].replace('-', '')}/{f['primaryDocument'][i].split('/')[-1]}")
            for i in idx[:max_filings]]
    trades, warns = [], []
    with ThreadPoolExecutor(4) as ex:  # SEC 限 10 次/秒
        for (filed, u), fu in zip(urls, [ex.submit(_form4, u) for _, u in urls]):
            try:
                trades += [{**x, "filed": filed, "url": u} for x in fu.result()]
            except Exception as e:
                warns.append(f"{filed} 一份 Form 4 解析失败：{_err(e)}")
    buys = [x for x in trades if x["code"] == "P"]
    sells = [x for x in trades if x["code"] == "S"]
    summ = {"form4_in_window": len(idx), "parsed": len(urls), "open_market_buys": len(buys), "open_market_sells": len(sells),
            "buy_value_usd": sum(x["value_usd"] or 0 for x in buys), "sell_value_usd": sum(x["value_usd"] or 0 for x in sells),
            "sells_under_10b5_1": sum(1 for x in sells if x["plan_10b5_1"])}
    flags = []
    disc = [x for x in sells if not x["plan_10b5_1"]]
    if disc:
        flags.append(f"近 {days} 天内部人非计划性卖出 {len(disc)} 笔，约 {sum(x['value_usd'] or 0 for x in disc) / 1e6:.1f} 百万美元")
    if buys:
        flags.append(f"近 {days} 天内部人公开市场买入 {len(buys)} 笔")
    key = [x for x in trades if x["code"] in ("P", "S")] or trades
    key.sort(key=lambda x: (x.get("date") or ""), reverse=True)
    return {"symbol": t, "source": "SEC EDGAR · Form 4", "url": page, "as_of": urls[0][0] if urls else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "days": days, "summary": summ, "flags": flags, "trades": key[:20], "warnings": warns,
            "note": "P/S 才是公开市场真买卖；A/M/F 是授予、行权、扣税，不代表看法。10b5-1 是事先登记的计划卖出，信号更弱",
            "sources": [src("SEC EDGAR · Form 4", page, urls[0][0] if urls else "")]}


# ---------------------------------------------------------------- 美股：FINRA 空头成交占比
def us_short_volume(symbol: str, days: int = 5) -> dict:
    p = parse(symbol)
    if p["market"] != "US":
        return {"symbol": p["yahoo"], "error": "FINRA 空头成交数据只适用于美股"}
    t, rows, d, tries = p["yahoo"].upper(), [], datetime.now(timezone.utc), 0
    while len(rows) < days and tries < days * 2 + 4:
        tries += 1
        if d.weekday() < 5:
            day = d.strftime("%Y%m%d")
            try:
                txt = _fetch(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day}.txt", headers=SEC_UA, ttl=6 * 3600, kind="text")
                for line in txt.splitlines():
                    if line.startswith(f"{day}|{t}|"):
                        x = line.split("|")
                        sv, tv = float(x[2]), float(x[4])
                        rows.append({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "short": round(sv), "total": round(tv), "ratio_pct": round(sv / tv * 100, 1) if tv else None})
                        break
            except Exception:
                pass
        d -= timedelta(days=1)
    url = "https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data"
    if not rows:
        return {"symbol": t, "error": "近几个交易日的 FINRA 空头成交文件取不到", "sources": [src("FINRA · Reg SHO 日空头成交", url, ok=False)]}
    avg = round(sum(r["ratio_pct"] or 0 for r in rows) / len(rows), 1)
    return {"symbol": t, "source": "FINRA · Reg SHO 日空头成交", "url": url, "as_of": rows[0]["date"], "rows": rows, "avg_ratio_pct": avg,
            "note": "空头成交占比 ≠ 空头持仓（含做市商对冲，40–50% 很常见），看日度变化趋势，不要用绝对值下结论",
            "warnings": [], "sources": [src("FINRA · Reg SHO 日空头成交", url, rows[0]["date"])]}


# ---------------------------------------------------------------- A股交易规则：涨跌停幅度 + T+1
def a_limit_pct(code: str, name: str = "") -> int:
    """主板 10%，创业板 300/301 与科创板 688/689 20%，北交所 30%，ST/*ST 主板 5%（创业板/科创板 ST 仍 20%）。"""
    c = str(code)[-6:]
    if c.startswith(("300", "301", "688", "689")):
        return 20
    if c.startswith(("4", "8", "92")):
        return 30
    if "ST" in (name or "").upper():
        return 5
    return 10


def a_rules(symbol: str, close: float | None, name: str = "") -> dict | None:
    p = parse(symbol)
    if p["market"] != "A" or not close:
        return None
    pct = a_limit_pct(p["code"], name)
    return {"limit_pct": pct, "next_limit_up": round(close * (1 + pct / 100) + 1e-9, 2), "next_limit_down": round(close * (1 - pct / 100) + 1e-9, 2),
            "t_plus": 1, "lot": 200 if p["code"].startswith(("688", "689")) else 100,
            "note": "T+1：当天买入最早下一个交易日才能卖；涨跌停价按收盘价估算，ST 股和新股上市前 5 日规则不同"}
