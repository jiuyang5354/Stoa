"""Point-in-time 公告与财报：只给决策日之前已经发布的东西，用于回测（单轮 / 排序模式的「有信息」消融）。

能拿到带发布时间的历史数据：
- A股 / 港股公告：东方财富公告中心 np-anotice（可翻页到多年前；display_time / notice_date 为发布时间）。
- 美股：SEC EDGAR submissions（filingDate）+ XBRL companyconcept（每个数值带 filed 日期）。
- A股财报：东方财富 RPT_F10_FINANCE_MAINFINADATA，带 NOTICE_DATE（披露日，不是报告期）。
- 港股财报：东方财富 RPT_HKF10_FN_MAININDICATOR 没有披露日 → 按上市规则期限保守估计可见日
  （年报 = 报告期 + 90 天、中报/季报 = 报告期 + 62 天；同期若查到「业绩」公告且更早，用公告日）。
拿不到的：个股新闻。东方财富搜索与个股资讯列表只给最近约 200 条 / 约 10 页，新浪个股资讯约 25 页，都只覆盖最近 1–2 个月，
做不了一年前的回测，所以这里不提供新闻（不伪造）。

防前视：发布日严格早于决策日（发布当天收盘后才出的公告，决策日当天收盘价已定，算不可见；美股盘后 8-K 同理），
财报一律按披露日而不是报告期。盲测时公告标题里的公司名替换成「该公司」。
"""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd

UA = {"User-Agent": "Mozilla/5.0"}
SEC_FORMS = {"10-K", "10-Q", "8-K", "20-F", "6-K", "10-K/A", "10-Q/A"}
_ROUTINE = re.compile(r"董事会决议公告$|监事会决议公告$|法律意见书|独立董事|章程|议事规则|工作制度|会议资料|翌日披露报表|月报表|"
                      r"股份发行人的证券变动月报表|自愿公告-?$|提名委员会|薪酬委员会|审核委员会|职权范围")


def cache_dir() -> Path:
    from .backtest import bt_dir
    d = bt_dir() / "pit_events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _req(url, params=None, headers=None, tries=3):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=25)
            r.raise_for_status()
            return r
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _market(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith((".SS", ".SZ")):
        return "A"
    if s.endswith(".HK"):
        return "HK"
    return "US"


def _code(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith(".HK"):
        return s[:-3].zfill(5)
    return s.split(".")[0]


# ---------------------------------------------------------------- 公告
def _em_announcements(symbol: str, since: pd.Timestamp) -> list[dict]:
    mk, code = _market(symbol), _code(symbol)
    out, page = [], 1
    while page <= 60:
        r = _req("https://np-anotice-stock.eastmoney.com/api/security/ann",
                 params={"sr": -1, "page_size": 100, "page_index": page, "ann_type": "A" if mk == "A" else "H",
                         "client_source": "web", "stock_list": code, "f_node": 0, "s_node": 0})
        L = (r.json().get("data") or {}).get("list") or []
        if not L:
            break
        for a in L:
            disp = (a.get("display_time") or "")[:19]
            nd = (a.get("notice_date") or "")[:10]
            # 发布时间取二者中较晚的那天（display_time 常是前一晚，notice_date 是次日）
            pub = max(x for x in (disp[:10], nd) if x) if (disp or nd) else None
            if not pub:
                continue
            out.append({"pub": pub, "time": disp or nd, "title": re.sub(r"\s+", " ", a.get("title") or "").strip(),
                        "type": "、".join(c.get("column_name", "") for c in a.get("columns") or [])})
        if min(x["pub"] for x in out) < f"{since:%Y-%m-%d}":
            break
        page += 1
    return out


def _sec_cik(ticker: str) -> str | None:
    from .research import SEC_UA
    f = cache_dir() / "_sec_tickers.json"
    if f.exists():
        m = json.loads(f.read_text())
    else:
        m = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in _req("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA).json().values()}
        f.write_text(json.dumps(m))
    return m.get(ticker.upper())


def _sec_announcements(symbol: str, since: pd.Timestamp) -> list[dict]:
    from .research import SEC_UA
    cik = _sec_cik(_code(symbol))
    if not cik:
        return []
    j = _req(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_UA).json()
    recs = [j["filings"]["recent"]]
    if min(recs[0]["filingDate"] or ["9"]) > f"{since:%Y-%m-%d}":  # 发文多的公司（如 JPM 的 424B2）recent 只覆盖几个月，补翻旧分页
        for fl in j["filings"].get("files") or []:
            if fl.get("filingTo", "9") >= f"{since:%Y-%m-%d}":
                recs.append(_req("https://data.sec.gov/submissions/" + fl["name"], headers=SEC_UA).json())
    out = []
    for rec in recs:
        out += _sec_rows(rec, since)
    return out


def _sec_rows(rec: dict, since) -> list[dict]:
    out = []
    for i, form in enumerate(rec["form"]):
        if form not in SEC_FORMS or rec["filingDate"][i] < f"{since:%Y-%m-%d}":
            continue
        desc = (rec.get("primaryDocDescription") or [""] * len(rec["form"]))[i] or ""
        items = (rec.get("items") or [""] * len(rec["form"]))[i] or ""
        title = form + (f" items {items}" if items else "") + (f" {desc}" if desc and desc.upper() != form else "")
        out.append({"pub": rec["filingDate"][i], "time": rec["filingDate"][i], "title": title.strip(), "type": form})
    return out


# 8-K item 含义（让模型看懂标题）
ITEMS_8K = {"1.01": "重大协议", "2.02": "业绩发布", "2.05": "重组/裁员", "2.06": "资产减值", "5.02": "高管变动",
            "7.01": "Reg FD 披露", "8.01": "其他事项", "9.01": "财务报表与附件", "5.07": "股东大会表决", "1.02": "终止重大协议",
            "2.01": "收购/处置资产", "2.03": "新增债务", "3.02": "增发", "8.01 ": "其他事项"}


def _explain_8k(title: str) -> str:
    m = re.search(r"items ([\d.,]+)", title)
    if not m:
        return title
    zh = [ITEMS_8K.get(x.strip()) for x in m.group(1).split(",")]
    zh = [z for z in zh if z and z != "财务报表与附件"]
    return title.split(" items")[0] + ("：" + "、".join(zh) if zh else "")


# ---------------------------------------------------------------- 财报（按披露日）
def _f(x, nd=1):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else round(x, nd)


def _a_financials(symbol: str) -> list[dict]:
    from .research import secucode
    cols = "REPORT_DATE,REPORT_DATE_NAME,NOTICE_DATE,TOTALOPERATEREVETZ,PARENTNETPROFITTZ,KCFJCXSYJLRTZ,ROEJQ,XSMLL,EPSJB"
    r = _req("https://datacenter.eastmoney.com/securities/api/data/v1/get",
             params={"reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": cols, "filter": f'(SECUCODE="{secucode(_code(symbol))}")',
                     "pageSize": 16, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
    out = []
    for x in (r.json().get("result") or {}).get("data") or []:
        if not x.get("NOTICE_DATE"):
            continue
        out.append({"pub": x["NOTICE_DATE"][:10], "period": x.get("REPORT_DATE_NAME") or x["REPORT_DATE"][:10], "pub_basis": "披露日",
                    "营收同比%": _f(x.get("TOTALOPERATEREVETZ")), "归母净利同比%": _f(x.get("PARENTNETPROFITTZ")),
                    "扣非净利同比%": _f(x.get("KCFJCXSYJLRTZ")), "ROE%": _f(x.get("ROEJQ")), "毛利率%": _f(x.get("XSMLL"))})
    return out


def _hk_financials(symbol: str, anns: list[dict]) -> list[dict]:
    r = _req("https://datacenter.eastmoney.com/securities/api/data/v1/get",
             params={"reportName": "RPT_HKF10_FN_MAININDICATOR", "columns": "ALL", "filter": f'(SECUCODE="{_code(symbol)}.HK")',
                     "pageSize": 16, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
    out = []
    # 只认真正的业绩公告（「…止年度全年业绩」「中期业绩公布」「第三季度业绩」），排除董事会会议通知（「审议…业绩」「董事会会议召开日期」）
    res_anns = [a for a in anns if re.search(r"业绩(公[布告])?\s*$|业绩公[布告]|止.{0,20}(年度|六个月|三个月|九个月|季度).{0,6}业绩|results", a["title"], re.I)
                and not re.search(r"董事会|审议|会议|召开|日期|通知|股息|派息|meeting", a["title"], re.I)]
    for x in (r.json().get("result") or {}).get("data") or []:
        rd = pd.Timestamp(x["REPORT_DATE"][:10])
        annual = rd.month == 12
        pub, basis = rd + timedelta(days=90 if annual else 62), "按上市规则期限保守估计"
        # 报告期结束后、期限之前的第一条业绩公告
        cands = sorted(a["pub"] for a in res_anns if f"{rd:%Y-%m-%d}" < a["pub"] <= f"{pub:%Y-%m-%d}")
        if cands:
            pub, basis = pd.Timestamp(cands[0]), "业绩公告日"
        out.append({"pub": f"{pub:%Y-%m-%d}", "period": x.get("REPORT_TYPE") or f"{rd:%Y-%m-%d}", "pub_basis": basis,
                    "营收同比%": _f(x.get("OPERATE_INCOME_YOY")), "归母净利同比%": _f(x.get("HOLDER_PROFIT_YOY")),
                    "ROE%": _f(x.get("ROE_AVG")), "毛利率%": _f(x.get("GROSS_PROFIT_RATIO"))})
    return out


def _us_financials(symbol: str) -> list[dict]:
    """SEC XBRL：每份 10-Q/10-K 的当期值与同一份文件里的上年同期值 → 同比；可见日 = filed。"""
    from .research import SEC_UA
    cik = _sec_cik(_code(symbol))
    if not cik:
        return []

    def concept(tags):
        """多个候选标签合并（公司换过标签，如 AAPL 从 Revenues 换到 RevenueFromContract…），同一期以较新的标签为准。"""
        best = []
        for t in tags:
            try:
                r = _req(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{t}.json", headers=SEC_UA, tries=1)
                u = r.json().get("units", {})
                vals = u.get("USD") or []
            except Exception:
                continue
            if vals and max(v.get("filed", "") for v in vals) > max((v.get("filed", "") for v in best), default=""):
                best = vals
        return best

    def yoy(vals):
        by = {}
        for v in vals:
            if v.get("form") not in ("10-Q", "10-K") or not v.get("start"):
                continue
            by.setdefault(v["accn"], []).append(v)
        out = {}
        for accn, L in by.items():
            for v in L:
                v["_d"] = (pd.Timestamp(v["end"]) - pd.Timestamp(v["start"])).days
            q = [v for v in L if 80 <= v["_d"] <= 100] or [v for v in L if 350 <= v["_d"] <= 380]
            if not q:
                continue
            cur = max(q, key=lambda v: v["end"])
            prev = [v for v in q if abs((pd.Timestamp(cur["end"]) - pd.Timestamp(v["end"])).days - 365) <= 10]
            if prev and prev[0]["val"]:
                out[accn] = (cur["filed"], cur["end"], cur.get("fp"), cur["val"] / prev[0]["val"] * 100 - 100 if prev[0]["val"] > 0 else None)
        return out

    rev = yoy(concept(["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenuesNetOfInterestExpense"]))
    ni = yoy(concept(["NetIncomeLoss"]))
    out = []
    for accn in sorted(set(rev) | set(ni)):
        a, b = rev.get(accn), ni.get(accn)
        base = a or b
        out.append({"pub": base[0], "period": f"{base[1]}（{base[2] or ''}）", "pub_basis": "SEC filed 日",
                    "营收同比%": _f(a[3]) if a and a[3] is not None else None, "净利同比%": _f(b[3]) if b and b[3] is not None else None})
    return sorted(out, key=lambda x: x["pub"], reverse=True)


# ---------------------------------------------------------------- 汇总 + 缓存
def load(symbol: str, since: str = "2025-06-01", refresh: bool = False) -> dict:
    """抓一次存盘（data/backtests/pit_events/<symbol>.json），之后回测离线读。"""
    f = cache_dir() / f"{symbol.replace('^', '_')}.json"
    if f.exists() and not refresh:
        return json.loads(f.read_text(encoding="utf-8"))
    since_ts = pd.Timestamp(since)
    mk, errs = _market(symbol), {}
    anns, fins = [], []
    try:
        anns = _sec_announcements(symbol, since_ts) if mk == "US" else _em_announcements(symbol, since_ts)
    except Exception as e:
        errs["announcements"] = f"{type(e).__name__}: {str(e)[:120]}"
    try:
        fins = _us_financials(symbol) if mk == "US" else _a_financials(symbol) if mk == "A" else _hk_financials(symbol, anns)
    except Exception as e:
        errs["financials"] = f"{type(e).__name__}: {str(e)[:120]}"
    d = {"symbol": symbol, "market": mk, "since": since, "fetched": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
         "announcements": anns, "financials": fins, "errors": errs}
    if anns or fins:
        f.write_text(json.dumps(d, ensure_ascii=False, indent=0), encoding="utf-8")
    return d


def redact(text: str, names: list[str]) -> str:
    for n in sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True):
        text = re.sub(re.escape(n), "该公司", text, flags=re.I)
    return text


def asof(data: dict, date, window_days: int = 30, max_items: int = 8, names: list[str] | None = None) -> dict:
    """决策日 date 能看到的：发布日 < date 的近 window_days 天公告（去掉例行公告，最多 max_items 条）+ 最近一期已披露财报。"""
    d = f"{pd.Timestamp(date):%Y-%m-%d}"
    lo = f"{pd.Timestamp(date) - timedelta(days=window_days):%Y-%m-%d}"
    anns = [a for a in data.get("announcements") or [] if lo <= a["pub"] < d and not _ROUTINE.search(a["title"])]
    anns = sorted(anns, key=lambda a: a["pub"], reverse=True)
    seen, items = set(), []
    for a in anns:
        t = _explain_8k(a["title"]) if data.get("market") == "US" else a["title"]
        t = re.sub(r"^[^:：]{2,12}[:：]", "", t)  # 「贵州茅台:」前缀
        if names:
            t = redact(t, names)
        if t in seen:
            continue
        seen.add(t)
        items.append({"距决策日天数": (pd.Timestamp(d) - pd.Timestamp(a["pub"])).days, "标题": t[:60]})
        if len(items) >= max_items:
            break
    fins = [x for x in data.get("financials") or [] if x["pub"] < d]
    fin = None
    if fins:
        x = max(fins, key=lambda x: x["pub"])
        fin = {k: v for k, v in x.items() if k not in ("pub", "pub_basis") and v is not None}
        fin["披露距今天数"] = (pd.Timestamp(d) - pd.Timestamp(x["pub"])).days
    return {"近期公告": items, "最近已披露财报": fin}


def leak_check(data: dict, date) -> bool:
    """测试用：asof 里的每一条都必须早于决策日。"""
    d = f"{pd.Timestamp(date):%Y-%m-%d}"
    v = asof(data, date, window_days=3650, max_items=10 ** 6)
    return all(i["距决策日天数"] > 0 for i in v["近期公告"]) and (v["最近已披露财报"] is None or v["最近已披露财报"]["披露距今天数"] > 0)
