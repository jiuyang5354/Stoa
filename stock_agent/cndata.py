"""中国市场结构化数据（免密钥）：宏观、A股财务指标（可按行业筛选）、中证指数官方日线、A股不复权日线、基金净值。

数据源：东方财富数据中心（国家统计局/人民银行/外汇局数据的镜像）、中证指数公司、腾讯证券、天天基金。
查数题优先用这里的结构化数据，网页搜索摘要作为补充。
"""
from __future__ import annotations

import json
import re

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"

MACRO = {  # kind: (reportName, 字段说明)
    "ppi": ("RPT_ECONOMY_PPI", "BASE 当月指数(上年同月=100) · BASE_SAME 当月同比% · BASE_ACCUMULATE 累计(上年同期=100)"),
    "cpi": ("RPT_ECONOMY_CPI", "NATIONAL_SAME 全国同比% · NATIONAL_SEQUENTIAL 环比% · NATIONAL_ACCUMULATE 累计(上年同期=100)；CITY_/RURAL_ 城市/农村"),
    "gdp": ("RPT_ECONOMY_GDP", "DOMESTICL_PRODUCT_BASE 累计 GDP 亿元 · SUM_SAME 累计同比% · FIRST/SECOND/THIRD 三次产业"),
    "reserves": ("RPT_ECONOMY_GOLD_CURRENCY", "FOREX 外汇储备 亿美元 · GOLD_RESERVES 黄金储备 亿美元 · *_SAME 同比% · *_SEQUENTIAL 环比%"),
    "pmi": ("RPT_ECONOMY_PMI", "MAKE_INDEX 制造业 PMI · NMAKE_INDEX 非制造业 PMI"),
    "m2": ("RPT_ECONOMY_CURRENCY_SUPPLY", "BASIC_CURRENCY M2 亿元 · BASIC_CURRENCY_SAME M2 同比% · CURRENCY M1 · FREE_CASH M0"),
    "retail": ("RPT_ECONOMY_TOTAL_RETAIL", "RETAIL_TOTAL 社零总额 亿元 · RETAIL_TOTAL_SAME 同比% · RETAIL_TOTAL_ACCUMULATE 累计"),
    "industrial": ("RPT_ECONOMY_INDUS_GROW", "BASE_SAME 规上工业增加值同比% · BASE_ACCUMULATE 累计同比%"),
    "fai": ("RPT_ECONOMY_ASSET_INVEST", "BASE 固定资产投资累计 亿元 · BASE_ACCUMULATE 累计同比%"),
}


def _dc(report: str, filt: str = "", sort: str = "REPORT_DATE", size: int = 120, columns: str = "ALL") -> list[dict]:
    p = {"reportName": report, "columns": columns, "pageSize": size, "pageNumber": 1, "sortColumns": sort, "sortTypes": -1}
    if filt:
        p["filter"] = filt
    j = requests.get(DC, params=p, headers=UA, timeout=20).json()
    return ((j.get("result") or {}).get("data")) or []


def macro(kind: str, start: str = "", end: str = "") -> dict:
    k = kind.lower().strip()
    if k == "lpr":
        j = requests.get("https://datacenter.eastmoney.com/api/data/get", params={"type": "RPTA_WEB_RATE", "sty": "ALL", "st": "TRADE_DATE", "sr": -1, "p": 1, "ps": 200},
                         headers=UA, timeout=20).json()
        rows = ((j.get("result") or {}).get("data")) or []
        rows = [{"date": r["TRADE_DATE"][:10], "LPR1Y": r.get("LPR1Y"), "LPR5Y": r.get("LPR5Y")} for r in rows
                if (not start or r["TRADE_DATE"][:10] >= start) and (not end or r["TRADE_DATE"][:10] <= end)]
        return {"kind": "lpr", "source": "东方财富数据中心（人民银行 LPR）", "rows": rows[:120], "fields": "LPR1Y/LPR5Y %"}
    if k not in MACRO:
        return {"error": f"kind 可选：{', '.join(list(MACRO) + ['lpr'])}"}
    rep, fields = MACRO[k]
    f = ""
    if start:
        f += f"(REPORT_DATE>='{start}')"
    if end:
        f += f"(REPORT_DATE<='{end}')"
    rows = _dc(rep, f, size=240)
    for r in rows:
        r["REPORT_DATE"] = (r.get("REPORT_DATE") or "")[:10]
    return {"kind": k, "source": "东方财富数据中心（国家统计局/人民银行/外汇局数据）", "url": "https://data.eastmoney.com/cjsj/",
            "fields": fields, "note": "REPORT_DATE 2024-03-01 表示 2024 年 3 月（季度数据为该季末月）；按月平均、按季合计等自己用 calc 算", "rows": rows}


def indicators(code: str = "", industry: str = "", report_date: str = "", sort: str = "") -> dict:
    """A股公司已披露的主要财务指标（合并报表）：营收、归母净利、加权 ROE、EPS、毛利率、同比；可按申万二级行业名筛全行业。"""
    cols = "SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,NOTICE_DATE,PUBLISHNAME,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT,WEIGHTAVG_ROE,BASIC_EPS,BPS,XSMLL,YSTZ,SJLTZ,MGJYXJJE"
    f = ""
    if code:
        digits = re.sub(r"\D", "", code)[:6]
        f += f'(SECURITY_CODE="{digits}")'
    if industry:
        f += f'(PUBLISHNAME="{industry}")'
    if report_date:
        f += f"(REPORTDATE='{report_date}')"
    if not f:
        return {"error": "给 code 或 industry（申万二级行业名，如 白酒Ⅱ、电池、银行Ⅱ），可加 report_date（如 2024-12-31）"}
    try:
        rows = _dc("RPT_LICO_FN_CPD", f, sort=sort or ("WEIGHTAVG_ROE" if industry else "REPORTDATE"), size=200 if industry else 12, columns=cols.replace(",DEDUCT_PARENT_NETPROFIT", ""))
    except Exception as e:
        return {"error": str(e)[:160]}
    for r in rows:
        for k in ("REPORTDATE", "NOTICE_DATE"):
            if r.get(k):
                r[k] = r[k][:10]
    return {"source": "东方财富数据中心 · 业绩报表（公司定期报告，合并口径）", "url": "https://data.eastmoney.com/bbsj/",
            "fields": "TOTAL_OPERATE_INCOME 营业总收入(元) · PARENT_NETPROFIT 归母净利润(元) · WEIGHTAVG_ROE 加权ROE% · BASIC_EPS 元 · BPS 每股净资产 · XSMLL 销售毛利率% · YSTZ 营收同比% · SJLTZ 净利同比% · MGJYXJJE 每股经营现金流 · PUBLISHNAME 申万行业",
            "note": "行业名用申万二级（带Ⅱ的要写全，如 白酒Ⅱ）；同一公司 A/B 股会各出现一行，题目问 A 股时去掉 B 股代码（2/9 开头）",
            "count": len(rows), "rows": rows}


def csindex(code: str, start: str, end: str) -> dict:
    """中证指数公司官方日线（沪深300 000300、中证500 000905、中证1000 000852、上证50 000016 及中证行业指数）。"""
    c = re.sub(r"\D", "", code)[:6] or code
    j = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf", params={"indexCode": c, "startDate": start.replace("-", ""), "endDate": end.replace("-", "")},
                     headers=UA, timeout=20).json()
    rows = [{"date": f'{r["tradeDate"][:4]}-{r["tradeDate"][4:6]}-{r["tradeDate"][6:]}', "open": r.get("open"), "high": r.get("high"), "low": r.get("low"), "close": r.get("close"),
             "change_pct": r.get("changePct"), "amount_100m": r.get("tradingValue"), "pe": r.get("peg")} for r in j.get("data") or []]
    name = (j.get("data") or [{}])[0].get("indexNameCn") if j.get("data") else None
    out = {"index": c, "name": name, "source": "中证指数有限公司", "url": f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={c}", "count": len(rows)}
    out["rows"] = rows if len(rows) <= 260 else rows[-260:]
    if rows:
        out["range"] = {"first": rows[0], "last": rows[-1], "change_pct": round((rows[-1]["close"] / rows[0]["close"] - 1) * 100, 4),
                        "highest_close": max(rows, key=lambda r: r["close"]), "lowest_close": min(rows, key=lambda r: r["close"])}
    return out


def kline_raw(code: str, start: str, end: str, adjust: str = "", full: bool = False) -> dict:
    """A股/港股日线（腾讯证券）。adjust 空=不复权（当时实际价），qfq 前复权，hfq 后复权。"""
    s = code.strip().lower()
    m = re.fullmatch(r"(?:(sh|sz|bj|hk))?(\d{4,6})(?:\.(ss|sh|sz|hk|bj))?", s)
    if not m:
        return {"error": "code 用 600519 / sz000858 / 00700.HK 这类"}
    num, pre, suf = m.group(2), m.group(1), m.group(3)
    mk = pre or {"ss": "sh", "sh": "sh", "sz": "sz", "hk": "hk", "bj": "bj"}.get(suf or "") or ("hk" if len(num) <= 5 else "sh" if num[0] in "569" else "bj" if num[0] in "48" else "sz")
    sym = mk + (num.zfill(5) if mk == "hk" else num)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    j = requests.get(url, params={"param": f"{sym},day,{start},{end},2000,{adjust}"}, headers=UA, timeout=20).json()
    d = ((j.get("data") or {}).get(sym)) or {}
    raw = d.get(adjust + "day") or d.get("day") or []
    rows = [{"date": r[0], "open": float(r[1]), "close": float(r[2]), "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])} for r in raw if len(r) >= 6]
    out = {"symbol": sym, "adjust": adjust or "none(不复权)", "source": "腾讯证券日线", "count": len(rows)}
    out["rows"] = rows if len(rows) <= 200 and not full else None
    if full and rows:
        from .cnhist import series_payload
        out.update(series_payload(rows, f"kline_{sym}_{start}_{end}_{adjust}", True))
    if rows:
        hi = max(rows, key=lambda r: r["high"]); lo = min(rows, key=lambda r: r["low"])
        out["range"] = {"first": rows[0], "last": rows[-1], "change_pct": round((rows[-1]["close"] / rows[0]["close"] - 1) * 100, 4),
                        "highest_high": hi, "lowest_low": lo, "highest_close": max(rows, key=lambda r: r["close"])}
        if len(rows) > 200 and not full:
            out["note"] = f"共 {len(rows)} 天，未逐日列出；要完整逐日序列给 full=true，具体某天缩小 start/end"
    return {k: v for k, v in out.items() if v is not None}


def fund_nav(code: str, start: str, end: str) -> dict:
    lst = []
    for page in range(1, 16):  # 接口每页最多 20 条
        j = requests.get("https://api.fund.eastmoney.com/f10/lsjz", params={"fundCode": code, "pageIndex": page, "pageSize": 20, "startDate": start, "endDate": end},
                         headers={**UA, "Referer": "https://fundf10.eastmoney.com/"}, timeout=20).json()
        got = ((j.get("Data") or {}).get("LSJZList")) or []
        lst += got
        if len(got) < 20:
            break
    rows = [{"date": r["FSRQ"], "nav": float(r["DWJZ"]) if r.get("DWJZ") else None, "acc_nav": float(r["LJJZ"]) if r.get("LJJZ") else None,
             "change_pct": r.get("JZZZL")} for r in reversed(lst)]
    return {"fund": code, "source": "天天基金 · 历史净值", "url": f"https://fundf10.eastmoney.com/jjjz_{code}.html", "count": len(rows), "rows": rows[-260:]}


def run_tool(name: str, a: dict):
    k = (a.get("kind") or "").lower()
    from . import cnhist
    if k in cnhist.KINDS:
        return cnhist.run(k, a)
    if k in ("indicators", "financials"):
        return indicators(a.get("code") or "", a.get("industry") or "", a.get("report_date") or "", a.get("sort") or "")
    if k == "csindex":
        return csindex(a.get("code") or "000300", a.get("start") or "", a.get("end") or "")
    if k == "kline":
        return kline_raw(a.get("code") or "", a.get("start") or "", a.get("end") or "", a.get("adjust") or "", bool(a.get("full")))
    if k == "fund_nav":
        return fund_nav(a.get("code") or "", a.get("start") or "", a.get("end") or "")
    return macro(k or a.get("indicator") or "", a.get("start") or "", a.get("end") or "")


TOOL_NAMES = {"cn_data"}
S = {"type": "string"}
TOOL_SPECS = [
    {"name": "cn_data", "description": "中国市场结构化数据（比网页搜索可靠，查数题先用它）。kind："
     "ppi/cpi/gdp/reserves(外汇与黄金储备)/pmi/m2/retail(社零)/industrial(工业增加值)/fai(固投)/lpr —— 国家统计局/人民银行/外汇局月度数据，start/end 为 YYYY-MM-DD；"
     "indicators —— A股定期报告主要指标（营收、归母净利、加权ROE、EPS、毛利率、同比），给 code 查单家，或 industry=申万二级行业名（如 白酒Ⅱ）+ report_date=2024-12-31 一次拿全行业做筛选排名；"
     "csindex —— 中证指数官方日线（000300 等）；kline —— A股/港股日线，adjust 空=不复权实际成交价（问历史最高价、某日开盘价用它），qfq/hfq 复权；fund_nav —— 基金历史净值；"
     "sw_index —— 申万行业指数日线（code=801xxx 或行业名，含 2014 版旧一级）；sw_rank —— 申万行业横截面涨跌排名（start/end，单日 start=end；version 2021/2014，level 一级/二级）；"
     "exchange_stats —— 沪深交易所某日市场概况（exchange=sh/sz，date：上市数、成交额、总市值）；etf_option —— ETF 期权合约日线（code 合约编码，或 underlying+month(YYMM)+cp(C/P)+strike）；"
     "nbs —— 国家统计局（query 搜指标，或 zb+db(annual/monthly/quarterly)+period）；gold —— 上海金基准价（sub=sge_benchmark）或 SGE 现货日线（sub=spot, instid=Au99.99）。"
     "full=true 返回完整逐日序列并写 /workspace/data/*.csv 供 run_python 计算。失败会给 error+hint，照 hint 换路径，不要编数。",
     "parameters": {"type": "object", "properties": {"kind": S, "code": S, "industry": S, "report_date": S, "start": S, "end": S, "adjust": S, "sort": S,
                                                     "date": S, "exchange": S, "version": S, "level": S, "underlying": S, "month": S, "cp": S, "strike": {"type": "number"},
                                                     "zb": S, "db": S, "period": S, "query": S, "sub": S, "instid": S, "full": {"type": "boolean"}}, "required": ["kind"]}},
]
