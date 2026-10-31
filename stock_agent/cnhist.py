"""大中华区历史数据（v0.37，免密钥公开接口）：申万行业指数日线与横截面排名、沪深交易所市场统计、ETF 期权日线、国家统计局、上海金/金价。

全部取官方或公开行情站点的原始数，失败就返回 error + 下一步怎么查（hint），绝不编数。
cndata.run_tool 按 kind 分派到这里：sw_index / sw_rank / exchange_stats / etf_option / nbs / gold。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
# 证书链不全的公开统计站点（只读公开数据）：先正常校验，SSL 失败再不校验重试一次
SSL_LAX = ("swsresearch.com", "szse.cn", "stats.gov.cn", "sge.com.cn")
_cache: dict = {}
TTL = 3600


def _req(method: str, url: str, **kw):
    key = (method, url, json.dumps(kw.get("params") or kw.get("data") or {}, sort_keys=True, ensure_ascii=False))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    kw.setdefault("timeout", 25)
    kw["headers"] = {**UA, **(kw.get("headers") or {})}
    try:
        r = requests.request(method, url, **kw)
    except requests.exceptions.SSLError:
        if not any(h in url for h in SSL_LAX):
            raise
        import urllib3
        urllib3.disable_warnings()
        r = requests.request(method, url, verify=False, **kw)
    r.raise_for_status()
    _cache[key] = (time.time(), r)
    return r


def _d(s) -> str:
    s = str(s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ====================================================================== 申万行业指数
SW_HOST = "https://www.swsresearch.com/institute-sw/api/index_publish"
SW_L1_2021 = {"801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属", "801080": "电子", "801110": "家用电器",
              "801120": "食品饮料", "801130": "纺织服饰", "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
              "801180": "房地产", "801200": "商贸零售", "801210": "社会服务", "801230": "综合", "801710": "建筑材料", "801720": "建筑装饰",
              "801730": "电力设备", "801740": "国防军工", "801750": "计算机", "801760": "传媒", "801770": "通信", "801780": "银行",
              "801790": "非银金融", "801880": "汽车", "801890": "机械设备", "801950": "煤炭", "801960": "石油石化", "801970": "环保", "801980": "美容护理"}
SW_L1_2014 = {"801010": "农林牧渔", "801020": "采掘", "801030": "化工", "801040": "钢铁", "801050": "有色金属", "801080": "电子", "801110": "家用电器",
              "801120": "食品饮料", "801130": "纺织服装", "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
              "801180": "房地产", "801200": "商业贸易", "801210": "休闲服务", "801230": "综合", "801710": "建筑材料", "801720": "建筑装饰",
              "801730": "电气设备", "801740": "国防军工", "801750": "计算机", "801760": "传媒", "801770": "通信", "801780": "银行",
              "801790": "非银金融", "801880": "汽车", "801890": "机械设备"}


def sw_list(level: str = "二级行业") -> dict[str, str]:
    """申万现行指数清单（一级/二级/三级行业），code → 名称。"""
    out, page = {}, 1
    while page <= 10:
        j = _req("GET", f"{SW_HOST}/current/", params={"page": page, "page_size": 50, "indextype": level}).json()
        d = j.get("data") or {}
        for r in d.get("results") or []:
            out[str(r.get("swindexcode"))] = r.get("swindexname")
        if not d.get("next"):
            break
        page += 1
    return out


def _sw_code(x: str) -> tuple[str | None, str | None]:
    x = (x or "").strip()
    if re.fullmatch(r"8\d{5}(\.SI)?", x, re.I):
        c = x[:6]
        return c, SW_L1_2021.get(c) or SW_L1_2014.get(c)
    name = x.replace("Ⅱ", "").replace("II", "").replace("申万", "").replace("指数", "").strip()
    for tbl in (SW_L1_2021, SW_L1_2014):
        for c, n in tbl.items():
            if n == name:
                return c, n
    for level in ("二级行业", "三级行业"):
        try:
            for c, n in sw_list(level).items():
                if (n or "").replace("Ⅱ", "").replace("Ⅲ", "") == name:
                    return c, n
        except Exception:
            break
    return None, None


def _sw_rows(code: str) -> list[dict]:
    j = _req("GET", f"{SW_HOST}/trend/", params={"swindexcode": code, "period": "DAY"}).json()
    rows = []
    for r in j.get("data") or []:
        c = _num(r.get("closeindex"))
        if c is None:
            continue
        rows.append({"date": _d(r.get("bargaindate")), "open": _num(r.get("openindex")), "high": _num(r.get("maxindex")), "low": _num(r.get("minindex")),
                     "close": c, "change_pct": _num(r.get("markup")), "volume_100m_shares": _num(r.get("bargainamount")), "amount_100m": _num(r.get("bargainsum"))})
    rows.sort(key=lambda r: r["date"])
    return rows


def sw_index(code: str, start: str = "", end: str = "", full: bool = False) -> dict:
    """单个申万指数日线（一级/二级/三级，含 2014 版已停用的旧一级如 801020 采掘）。"""
    c, name = _sw_code(code)
    if not c:
        return {"error": f"认不出申万指数「{code}」：给 6 位代码（801xxx）或行业名（如 食品饮料、白酒Ⅱ）",
                "hint": "一级行业可用 sw_rank 一次看全部；二级名称以申万官网为准"}
    rows = _sw_rows(c)
    if not rows:
        return {"index": c, "error": "申万官网没返回这个指数的日线", "hint": "检查代码；或 web_search「申万 指数代码 行业名」"}
    span = [r for r in rows if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
    pre = next((r for r in reversed(rows) if start and r["date"] < start), None)
    out = {"index": c, "name": name, "source": "申万宏源研究 · 申万行业指数（官网日线）", "url": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex",
           "count": len(span), "as_of": rows[-1]["date"],
           "note": "change_pct 为官网公布的当日涨跌幅（相对前一交易日收盘）；区间涨跌用 range 里的 change_pct_from_prev_close（以 start 前一交易日收盘为基准）"}
    if span:
        out["range"] = {"first": span[0], "last": span[-1], "prev_close_before_start": pre,
                        "change_pct": round((span[-1]["close"] / span[0]["close"] - 1) * 100, 4),
                        "change_pct_from_prev_close": round((span[-1]["close"] / pre["close"] - 1) * 100, 4) if pre else None,
                        "highest_close": max(span, key=lambda r: r["close"]), "lowest_close": min(span, key=lambda r: r["close"])}
    out.update(series_payload(span, f"sw_{c}_{start}_{end}", full))
    return out


def sw_rank(start: str, end: str = "", version: str = "2021", level: str = "一级", codes=None, top: int = 0) -> dict:
    """申万行业横截面：区间（或单日 start=end）各指数涨跌幅排名。单日涨跌 = 当日收盘 / 前一交易日收盘 − 1。"""
    end = end or start
    if codes:
        tbl = {c: (SW_L1_2021.get(c) or SW_L1_2014.get(c) or c) for c in codes}
    elif level.startswith("一"):
        tbl = SW_L1_2014 if str(version).startswith("2014") else SW_L1_2021
    else:
        try:
            tbl = sw_list("二级行业" if level.startswith("二") else "三级行业")
        except Exception as e:
            return {"error": f"申万指数清单取不到：{str(e)[:120]}"}

    def one(item):
        c, n = item
        try:
            rows = _sw_rows(c)
        except Exception as e:
            return {"code": c, "name": n, "error": str(e)[:80]}
        span = [r for r in rows if start <= r["date"] <= end]
        pre = next((r for r in reversed(rows) if r["date"] < start), None)
        if not span or not pre:
            return {"code": c, "name": n, "error": "区间内无数据"}
        return {"code": c, "name": n, "start_base": pre["date"], "base_close": pre["close"], "end_date": span[-1]["date"], "close": span[-1]["close"],
                "change_pct": round((span[-1]["close"] / pre["close"] - 1) * 100, 4)}

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, tbl.items()))
    ok = sorted([r for r in res if "change_pct" in r], key=lambda r: r["change_pct"])
    bad = [f'{r["code"]} {r["name"]}：{r["error"]}' for r in res if "error" in r]
    out = {"source": "申万宏源研究 · 申万行业指数（官网日线）", "url": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex",
           "version": version if level.startswith("一") else "现行", "level": level, "start": start, "end": end,
           "basis": "涨跌幅 = 区间最后一个交易日收盘 / start 之前最后一个交易日收盘 − 1（单日即当日涨跌幅）",
           "count": len(ok), "rows_ascending": ok[:top] if top else ok}
    if bad:
        out["failed"] = bad[:12]
    if not ok:
        out["error"] = "所有指数在该区间都没取到数据"
    return out


# ====================================================================== 交易所市场统计
SSE_REF = {"Referer": "https://www.sse.com.cn/"}
SSE_PRODUCTS = {"01": "主板A股", "02": "主板B股", "03": "科创板", "11": "股票回购", "17": "股票合计"}


def exchange_stats(exchange: str, date: str) -> dict:
    """沪深交易所某一交易日的市场概况：上市数量、成交额、总市值/流通市值（亿元）、平均市盈率、换手率。
    年末/月末数据给那一天（或之前最后一个交易日）即可；年度合计成交额等交易所只出年报/统计年鉴，用 web_search。"""
    ex = (exchange or "").lower()
    d = _d(date)
    if not d:
        return {"error": "给 date（YYYY-MM-DD）"}
    tried = []
    for k in range(0, 8):  # 非交易日往前找
        dd = (datetime.fromisoformat(d) - timedelta(days=k)).strftime("%Y-%m-%d")
        try:
            if ex in ("sz", "szse", "深", "深交所", "深市"):
                j = _req("GET", "https://www.szse.cn/api/report/ShowReport/data",
                         params={"SHOWTYPE": "JSON", "CATALOGID": "1803_sczm", "TABKEY": "tab1", "txtQueryDate": dd}).json()
                data = (j[0].get("data") if j else None) or []
                if data:
                    rows = [{"category": re.sub(r"&nbsp;|\s", "", r.get("lbmc") or ""), "count": _num(r.get("zqsl")), "turnover_100m": _num(r.get("cjje")),
                             "total_mv_100m": _num(r.get("sjzz")), "float_mv_100m": _num(r.get("ltsz"))} for r in data]
                    return {"exchange": "深圳证券交易所", "date": dd, "requested": d, "source": "深交所 · 市场总貌 · 证券类别统计",
                            "url": "https://www.szse.cn/market/overview/index.html", "rows": rows,
                            "note": "金额单位亿元；count 为该类证券数量（只）；turnover 为当日成交额"}
            elif ex in ("sh", "sse", "沪", "上交所", "沪市"):
                j = _req("GET", "https://query.sse.com.cn/commonQuery.do", headers=SSE_REF,
                         params={"sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C", "PRODUCT_CODE": "01,02,03,11,17", "type": "inParams", "SEARCH_DATE": dd}).json()
                data = j.get("result") or []
                if data and any(_num(r.get("LIST_NUM")) for r in data):
                    rows = [{"category": SSE_PRODUCTS.get(r.get("PRODUCT_CODE"), r.get("PRODUCT_CODE")), "count": _num(r.get("LIST_NUM")),
                             "total_mv_100m": _num(r.get("TOTAL_VALUE")), "float_mv_100m": _num(r.get("NEGO_VALUE")), "turnover_100m": _num(r.get("TRADE_AMT")),
                             "volume_100m_shares": _num(r.get("TRADE_VOL")), "avg_pe": _num(r.get("AVG_PE_RATE")), "turnover_rate_pct": _num(r.get("TOTAL_TO_RATE"))}
                            for r in data]
                    return {"exchange": "上海证券交易所", "date": dd, "requested": d, "source": "上交所 · 股票数据 · 成交概况（每日）",
                            "url": "https://www.sse.com.cn/market/stockdata/overview/day/", "rows": rows, "note": "金额单位亿元"}
            else:
                return {"error": "exchange 用 sh（上交所）或 sz（深交所）",
                        "hint": "北交所市场统计没有稳定公开接口：web_search「北京证券交易所 市场统计 年末 总市值」并 fetch_url bse.cn 原页"}
        except Exception as e:
            tried.append(f"{dd}：{str(e)[:80]}")
            if len(tried) >= 3:
                break
    return {"exchange": exchange, "requested": d, "error": "交易所接口没返回这天及之前一周的数据" + (f"（{tried[0]}）" if tried else ""),
            "hint": "年度统计（全年成交额、退市家数、上市公司营收增速等）在交易所年度市场统计/年报实证分析里：web_search 加「深交所 上市公司 年报 实证分析」或「市场统计年鉴」后 fetch_url 原文"}


# ====================================================================== ETF 期权日线
SINA_REF = {"Referer": "https://stock.finance.sina.com.cn/"}
OPT_UNDERLYING = {"50etf": "510050", "300etf": "510300", "500etf": "510500", "科创50": "588000", "科创板50": "588080", "深300etf": "159919",
                  "510050": "510050", "510300": "510300", "510500": "510500", "588000": "588000", "588080": "588080", "159919": "159919"}


def _opt_daily(code: str) -> list[dict]:
    r = _req("GET", "https://stock.finance.sina.com.cn/futures/api/jsonp_v2.php//StockOptionDaylineService.getSymbolInfo",
             params={"symbol": f"CON_OP_{code}"}, headers=SINA_REF)
    m = re.search(r"\((\[.*\])\)", r.text, re.S)
    arr = json.loads(m.group(1)) if m else []
    return [{"date": x["d"], "open": _num(x.get("o")), "high": _num(x.get("h")), "low": _num(x.get("l")), "close": _num(x.get("c")), "volume": _num(x.get("v"))}
            for x in arr]


def _opt_find(underlying: str, month: str, cp: str, strike: float) -> tuple[str | None, str | None, list]:
    """在新浪「当前挂牌」合约里按 标的+到期月+认购/认沽+行权价 找合约编码。过期合约不在挂牌表里。"""
    u = OPT_UNDERLYING.get(underlying.lower().replace(" ", ""), underlying)
    side = "DOWN" if cp.lower() in ("p", "put", "沽", "认沽") else "UP"
    r = _req("GET", f"https://hq.sinajs.cn/list=OP_{side}_{u}{month[-4:]}", headers=SINA_REF)
    codes = re.findall(r"CON_OP_(\d+)", r.text)
    if not codes:
        return None, None, []
    q = _req("GET", "https://hq.sinajs.cn/list=" + ",".join(f"CON_OP_{c}" for c in codes), headers=SINA_REF).text
    seen = []
    for c, body in re.findall(r"hq_str_CON_OP_(\d+)=\"([^\"]*)\"", q):
        f = body.split(",")
        if len(f) > 37:
            seen.append((c, f[37], _num(f[7])))
            if strike and abs((_num(f[7]) or 0) - (strike if strike < 100 else strike / 1000)) < 1e-6:
                return c, f[37], seen
    return None, None, seen


def etf_option(code: str = "", underlying: str = "", month: str = "", cp: str = "", strike: float | None = None,
               start: str = "", end: str = "", full: bool = False) -> dict:
    """上交所/深交所 ETF 期权合约日线（新浪财经，过期合约给合约编码也能取）。"""
    name = None
    if not code:
        if not (underlying and month and cp and strike):
            return {"error": "给合约编码 code（如 10008000），或 underlying(300ETF/50ETF/500ETF/科创50) + month(YYMM，如 2505) + cp(C/P) + strike(如 3.3)"}
        try:
            code, name, seen = _opt_find(underlying, str(month), cp, float(strike))
        except Exception as e:
            return {"error": f"新浪期权挂牌表取不到：{str(e)[:100]}"}
        if not code:
            return {"error": f"当前挂牌合约里没有 {underlying} {month} {cp} {strike}（已到期的合约不在挂牌表里）",
                    "hint": "已到期合约需要合约编码：web_search「" + f"{underlying}{'沽' if cp.lower().startswith('p') else '购'}{int(str(month)[-2:])}月{int(float(strike) * 1000) if float(strike) < 100 else int(float(strike))}" + " 合约编码 1000」再用 code 查；或 fetch_url 上交所/期权数据页",
                    "listed_now": [f"{c} {n}" for c, n, _ in seen[:12]]}
    try:
        rows = _opt_daily(str(code))
    except Exception as e:
        return {"code": code, "error": f"新浪期权日线取不到：{str(e)[:100]}"}
    if not rows:
        return {"code": code, "error": "这个合约编码没有日线数据", "hint": "确认 8 位合约编码（上交所 1000xxxx）"}
    span = [r for r in rows if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
    for i, r in enumerate(span):
        j = rows.index(r)
        r["change_pct"] = round((r["close"] / rows[j - 1]["close"] - 1) * 100, 4) if j and rows[j - 1]["close"] else None
    out = {"code": str(code), "name": name, "source": "新浪财经 · 股票期权日线", "url": f"https://stock.finance.sina.com.cn/option/quotes.html",
           "listed_first_day": rows[0]["date"], "last_day": rows[-1]["date"], "count": len(span),
           "note": "close 为当日收盘价（元/份）；连续上涨等统计请用 run_python 读 csv 或逐日比较，不要心算"}
    out.update(series_payload(span, f"opt_{code}_{start}_{end}", full or len(span) <= 160))
    return {k: v for k, v in out.items() if v is not None}


# ====================================================================== 国家统计局
NBS_DB = {"monthly": "hgyd", "quarterly": "hgjd", "annual": "hgnd", "province_annual": "fsnd", "province_monthly": "fsyd"}


def nbs(zb: str = "", db: str = "annual", period: str = "", query: str = "", region: str = "") -> dict:
    """国家统计局数据库（data.stats.gov.cn easyquery）：zb 指标代码（如 A0201），period 如 2010-2024 / LAST10 / 2024；query 搜指标。"""
    dbcode = NBS_DB.get(db, db)
    base = "https://data.stats.gov.cn/easyquery.htm"
    hint = ("国家统计局接口常拒绝境外 IP 或改版：改用 web_search「国家统计局 统计年鉴 + 指标名 + 年份」，fetch_url www.stats.gov.cn/sj/ndsj/ 年鉴表或统计公报原文；"
            "民政类（医疗救助等）查民政部统计公报/中国民政统计年鉴")
    try:
        if query and not zb:
            r = _req("GET", "https://data.stats.gov.cn/search.htm", params={"s": query, "m": "searchdata", "db": "", "p": 0})
            j = r.json()
            items = [{"zb": re.sub(r"<[^>]+>", "", it.get("zb") or ""), "db": it.get("db"), "period": it.get("sj"), "value": it.get("data"),
                      "region": it.get("reg"), "report": re.sub(r"<[^>]+>", "", it.get("report") or "")} for it in (j.get("result") or [])[:20]]
            return {"query": query, "source": "国家统计局 · 数据查询", "url": f"https://data.stats.gov.cn/search.htm?s={query}", "results": items,
                    "note": "搜索结果给的是单个数值；要整段序列，用结果里的指标再按 zb 查"} if items else {"query": query, "error": "国家统计局搜索无结果", "hint": hint}
        if not zb:
            return {"error": "给 zb（指标代码）或 query（指标名关键词）", "hint": hint}
        wds = [{"wdcode": "reg", "valuecode": region}] if region else []
        dfw = [{"wdcode": "zb", "valuecode": zb}] + ([{"wdcode": "sj", "valuecode": period}] if period else [])
        j = _req("GET", base, params={"m": "QueryData", "dbcode": dbcode, "rowcode": "zb", "colcode": "sj", "wds": json.dumps(wds), "dfwds": json.dumps(dfw, ensure_ascii=False),
                                      "k1": str(int(time.time() * 1000))}).json()
    except Exception as e:
        return {"zb": zb, "query": query, "error": f"国家统计局接口不可用：{str(e)[:100]}", "hint": hint}
    rd = j.get("returndata") or {}
    names = {}
    for node in rd.get("wdnodes") or []:
        for n in node.get("nodes") or []:
            names[n.get("code")] = (n.get("cname"), n.get("unit"))
    rows = []
    for dn in rd.get("datanodes") or []:
        wd = {w["wdcode"]: w["valuecode"] for w in dn.get("wds") or []}
        dat = dn.get("data") or {}
        if not dat.get("hasdata"):
            continue
        z = wd.get("zb")
        rows.append({"indicator": (names.get(z) or (z,))[0], "unit": (names.get(z) or (None, None))[1], "period": wd.get("sj"), "value": dat.get("data")})
    if not rows:
        return {"zb": zb, "error": "国家统计局没有返回数据（指标代码或时间可能不对）", "hint": hint}
    rows.sort(key=lambda r: (r["indicator"] or "", r["period"] or ""))
    return {"zb": zb, "db": dbcode, "source": "国家统计局 · 国家数据（data.stats.gov.cn）", "url": "https://data.stats.gov.cn/easyquery.htm?cn=" + {"hgnd": "C01", "hgyd": "A01", "hgjd": "B01"}.get(dbcode, "C01"),
            "count": len(rows), "rows": rows[:240]}


# ====================================================================== 金价
def gold(kind: str = "sge_benchmark", start: str = "", end: str = "", instid: str = "Au99.99", full: bool = False) -> dict:
    """上海黄金交易所：sge_benchmark 上海金基准价（早盘/午盘，元/克）；spot 现货合约日线（Au99.99 / Au(T+D) / Ag(T+D) 等）。"""
    k = (kind or "sge_benchmark").lower()
    try:
        if k in ("sge_benchmark", "benchmark", "上海金", "shau"):
            j = _req("POST", "https://www.sge.com.cn/graph/DayilyJzj").json()
            am = {datetime.fromtimestamp(t / 1000 + 8 * 3600, timezone.utc).strftime("%Y-%m-%d"): v for t, v in j.get("zp") or []}
            pm = {datetime.fromtimestamp(t / 1000 + 8 * 3600, timezone.utc).strftime("%Y-%m-%d"): v for t, v in j.get("wp") or []}
            rows = [{"date": d, "am": am.get(d), "pm": pm.get(d)} for d in sorted(set(am) | set(pm))]
            src, url, note = "上海黄金交易所 · 上海金基准价", "https://www.sge.com.cn/sjzx/jzj", "元/克；am 早盘基准价、pm 午盘基准价"
        else:
            j = _req("POST", "https://www.sge.com.cn/graph/Dailyhq", data={"instid": instid}).json()
            rows = [{"date": r[0], "open": r[1], "close": r[2], "low": r[3], "high": r[4]} for r in j.get("time") or []]
            src, url, note = f"上海黄金交易所 · {instid} 日行情", "https://www.sge.com.cn/sjzx/mrhq", "元/克（白银元/千克）；字段 开/收/低/高"
    except Exception as e:
        return {"error": f"上海黄金交易所接口不可用：{str(e)[:100]}", "hint": "可用 price_history 取 GC=F（COMEX 美元/盎司）并注明口径不同"}
    span = [r for r in rows if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
    if not span:
        return {"error": "该区间没有数据", "available": [rows[0]["date"], rows[-1]["date"]] if rows else None}
    out = {"kind": k, "source": src, "url": url, "note": note + "。品牌金店（周大福、周六福等）的零售金价不在交易所数据里：web_search 品牌官网/金价播报原页，并说明来源",
           "count": len(span), "as_of": rows[-1]["date"]}
    out.update(series_payload(span, f"gold_{k}_{start}_{end}", full or len(span) <= 160))
    return out


# ====================================================================== 完整序列输出
def series_payload(rows: list[dict], stem: str, full: bool) -> dict:
    """序列 ≤160 行或 full=True：逐日列出（列式，省 token）；同时写一份 CSV 到 workspace 供 run_python 读。"""
    if not rows:
        return {}
    out = {}
    path = write_csv(rows, stem)
    if path:
        out["csv"] = path
    if full or len(rows) <= 160:
        cols = list(rows[0].keys())
        if len(rows) > 400:  # 太长只列日期和收盘，完整 OHLC 在 CSV
            cols = [c for c in ("date", "close", "am", "pm", "value") if c in cols]
        out["columns"] = cols
        out["rows"] = [[r.get(c) for c in cols] for r in rows]
        out["_full_series"] = True
    else:
        out["series_note"] = f"共 {len(rows)} 行未逐日列出；需要完整逐日序列给 full=true（或用 run_python 读 csv）"
    return out


def write_csv(rows: list[dict], stem: str) -> str | None:
    try:
        from .sandbox import _workspace
        ws = _workspace()
        if not ws:
            return None
        d = ws / "data"
        d.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", stem).strip("_")[:80] + ".csv"
        cols = list(rows[0].keys())
        with open(d / name, "w", encoding="utf-8", newline="") as f:
            import csv
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c) for c in cols])
        return "/workspace/data/" + name
    except Exception:
        return None


def run(kind: str, a: dict):
    full = bool(a.get("full"))
    if kind == "sw_index":
        return sw_index(a.get("code") or a.get("industry") or "", a.get("start") or "", a.get("end") or "", full)
    if kind == "sw_rank":
        codes = a.get("codes")
        if isinstance(codes, str):
            codes = [c for c in re.split(r"[,\s]+", codes) if c]
        return sw_rank(a.get("start") or a.get("date") or "", a.get("end") or "", str(a.get("version") or "2021"), a.get("level") or "一级", codes, int(a.get("top") or 0))
    if kind == "exchange_stats":
        return exchange_stats(a.get("exchange") or a.get("code") or "", a.get("date") or a.get("end") or a.get("start") or "")
    if kind == "etf_option":
        return etf_option(a.get("code") or "", a.get("underlying") or "", str(a.get("month") or ""), a.get("cp") or "", a.get("strike"),
                          a.get("start") or "", a.get("end") or "", full)
    if kind == "nbs":
        return nbs(a.get("zb") or a.get("code") or "", a.get("db") or "annual", a.get("period") or "", a.get("query") or "", a.get("region") or "")
    if kind == "gold":
        return gold(a.get("sub") or a.get("code") or "sge_benchmark", a.get("start") or "", a.get("end") or "", a.get("instid") or "Au99.99", full)
    return None


KINDS = ("sw_index", "sw_rank", "exchange_stats", "etf_option", "nbs", "gold")
