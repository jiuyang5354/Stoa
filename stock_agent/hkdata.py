"""v0.38 补数据缺口（盲评 v1 Q1/Q2/Q8/Q12）：缺数据时先换公开来源补齐，并写明口径。

- turnover(symbol)       成交量 / 成交额：腾讯行情 qt.gtimg.cn（A股 f35「价/量(手)/额(元)」，港股 f37 港元，美股 f37 美元）。
- southbound(...)        南向资金（港股通）：东方财富数据中心 RPT_MUTUAL_DEAL_HISTORY（006 南向合计、002 港股通(沪)、004 港股通(深)）的每日净买入，
                         个股用 RPT_MUTUAL_TOP10DEAL（港股通十大成交股的买入/卖出/净买入，只在进入前十的日子有数）。南向每日净买入至今仍披露（停的是北向）。
- hk_interests(symbol)   港交所披露易「权益披露」（di.hkex.com.hk）：大股东/董事的增减持通知（表格序号、名称、原因代码、买卖股数、均价、持股、占比、日期）。
- tencent_daily(...)     腾讯日线 fqkline：港股指数（恒生科技 HSTECH、恒生指数 HSI、国企指数 HSCEI）、港股、A股；Yahoo 没有或 404 时 price_history 的备用源。
全部免密钥；每个结果带 source / url（具体数据页）/ as_of。
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timedelta

from .marketdata import DC, EM_REF, UA, _f, _fetch, now_cn, parse, src

TX_Q = "https://qt.gtimg.cn/q="
TX_K = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
DI_LIST = "https://di.hkex.com.hk/di/NSSrchCorpList.aspx"
DI_FORMS = "https://di.hkex.com.hk/di/NSAllFormList.aspx"

# 指数别名 → 腾讯代码 / 显示名 / 公开页面
INDEX_ALIAS = {
    "HSTECH": ("hkHSTECH", "恒生科技指数"), "^HSTECH": ("hkHSTECH", "恒生科技指数"), "HSTECH.HK": ("hkHSTECH", "恒生科技指数"), "恒生科技": ("hkHSTECH", "恒生科技指数"),
    "恒生科技指数": ("hkHSTECH", "恒生科技指数"), "^HSI": ("hkHSI", "恒生指数"), "HSI": ("hkHSI", "恒生指数"), "恒生指数": ("hkHSI", "恒生指数"),
    "^HSCE": ("hkHSCEI", "恒生中国企业指数"), "HSCEI": ("hkHSCEI", "恒生中国企业指数"), "^HSCEI": ("hkHSCEI", "恒生中国企业指数"), "国企指数": ("hkHSCEI", "恒生中国企业指数"),
}


def tx_code(symbol: str) -> str | None:
    s = (symbol or "").strip()
    if s.upper() in INDEX_ALIAS or s in INDEX_ALIAS:
        return (INDEX_ALIAS.get(s.upper()) or INDEX_ALIAS[s])[0]
    m = re.fullmatch(r"(\d{6})\.(SS|SZ)", s.upper())
    if m and s.upper() in ("000001.SS", "000300.SS", "000016.SS", "000905.SS", "000688.SS"):
        return "sh" + m.group(1)
    if s.upper() in ("399001.SZ", "399006.SZ"):
        return "sz" + s[:6]
    p = parse(s)
    return p.get("tencent") if p["market"] in ("A", "HK") else None


def page_url(code: str) -> str:
    return f"https://gu.qq.com/{code}" if code else ""


# ---------------------------------------------------------------- 成交额
def turnover(symbol: str) -> dict | None:
    """腾讯实时行情里的成交量与成交额（最近交易日）。A股成交额单位元（换算亿元），港股港元，美股美元。"""
    p = parse(symbol)
    if p["market"] not in ("A", "HK", "US"):  # 指数的成交额字段口径不同，不在这里给
        return None
    code = p.get("tencent")
    if not code:
        return None
    t = _fetch(TX_Q + code, headers=UA, ttl=60, kind="text")
    return parse_turnover(t, p["market"], code)


def parse_turnover(text: str, market: str, code: str = "") -> dict | None:
    f = text.split("~")
    if len(f) < 38 or not _f(f[3]):
        return None
    ts = f[30]
    date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if re.fullmatch(r"\d{14}", ts) else ts[:10].replace("/", "-")
    if market == "A":
        parts = f[35].split("/")
        amt = _f(parts[2]) if len(parts) > 2 else (_f(f[37]) * 1e4 if _f(f[37]) is not None else None)
        vol = _f(f[36] or f[6])
        out = {"volume_lots": vol, "volume_shares": vol * 100 if vol is not None else None, "amount": amt, "currency": "CNY"}
    else:
        out = {"volume_shares": _f(f[36] or f[6]), "amount": _f(f[37]), "currency": "HKD" if market == "HK" else "USD"}
    if out.get("amount") is None:
        return None
    out["amount_yi"] = round(out["amount"] / 1e8, 2)
    out.update({"date": date, "source": "腾讯证券实时行情", "url": page_url(code),
                "basis": "成交额＝当日累计成交金额（A股人民币元、港股港元、美股美元），amount_yi 为亿"})
    return out


# ---------------------------------------------------------------- 南向资金
SOUTH_NOTE = ("南向资金＝港股通（沪）+ 港股通（深）的买入减卖出成交额，按交易日披露（停止披露每日净买入的是北向，南向仍披露）；"
              "个股净买入只在该股进入当日港股通十大成交活跃股时公布，其余日子没有逐日个股数据，汇总只覆盖有数的日子。")


def southbound(symbol: str | None = None, days: int = 5) -> dict:
    days = max(1, min(int(days or 5), 30))
    out = {"source": "东方财富数据中心（港股通）", "url": "https://data.eastmoney.com/hsgt/index.html", "rule_note": SOUTH_NOTE, "sources": [], "warnings": []}
    try:
        rows = {}
        for mt, key in (("006", "total"), ("002", "sh"), ("004", "sz")):
            j = _fetch(DC, params={"reportName": "RPT_MUTUAL_DEAL_HISTORY", "columns": "ALL", "filter": f'(MUTUAL_TYPE="{mt}")',
                                   "pageSize": days, "sortColumns": "TRADE_DATE", "sortTypes": "-1"}, headers=EM_REF, ttl=600)
            for x in (j.get("result") or {}).get("data") or []:
                d = str(x.get("TRADE_DATE"))[:10]
                r = rows.setdefault(d, {"date": d})
                net = _f(x.get("NET_DEAL_AMT"))
                r[f"{key}_net_buy_yi"] = round(net / 100, 2) if net is not None else None  # 单位：百万港元 → 亿港元
                if key == "total":
                    r["buy_yi"] = round(_f(x.get("BUY_AMT")) / 100, 2) if _f(x.get("BUY_AMT")) is not None else None
                    r["sell_yi"] = round(_f(x.get("SELL_AMT")) / 100, 2) if _f(x.get("SELL_AMT")) is not None else None
        market = sorted(rows.values(), key=lambda r: r["date"], reverse=True)
        out["market"] = market
        tot = [r.get("total_net_buy_yi") for r in market if r.get("total_net_buy_yi") is not None]
        if tot:
            out["market_sum_net_buy_yi"] = round(sum(tot), 2)
            out["market_days"] = len(tot)
        out["as_of"] = market[0]["date"] if market else ""
        out["currency"] = "HKD（亿港元）"
        out["sources"].append(src("东方财富 · 南向资金每日成交", "https://data.eastmoney.com/hsgt/index.html", out["as_of"], ok=bool(market)))
    except Exception as e:
        out["sources"].append(src("东方财富 · 南向资金每日成交", "https://data.eastmoney.com/hsgt/index.html", ok=False, error=str(e)[:100]))
    if symbol:
        p = parse(symbol)
        out["symbol"] = p["yahoo"]
        if p["market"] != "HK":
            out["stock_error"] = "个股南向资金只适用于港股（港股通标的）"
        else:
            url = f"https://data.eastmoney.com/hsgt/hsgtDetail/scgk.html?code={p['code']}"
            try:
                since = (now_cn() - timedelta(days=max(14, days * 3))).strftime("%Y-%m-%d")
                j = _fetch(DC, params={"reportName": "RPT_MUTUAL_TOP10DEAL", "columns": "ALL",
                                       "filter": f'(SECURITY_CODE="{p["code"]}")(MUTUAL_TYPE in ("002","004"))(TRADE_DATE>=\'{since}\')',
                                       "pageSize": 60, "sortColumns": "TRADE_DATE", "sortTypes": "-1"}, headers=EM_REF, ttl=600)
                by = {}
                for x in (j.get("result") or {}).get("data") or []:
                    d = str(x.get("TRADE_DATE"))[:10]
                    r = by.setdefault(d, {"date": d, "net_buy_yi": 0.0, "buy_yi": 0.0, "sell_yi": 0.0, "channels": []})
                    for k, col in (("net_buy_yi", "NET_BUY_AMT"), ("buy_yi", "BUY_AMT"), ("sell_yi", "SELL_AMT")):
                        r[k] = round(r[k] + (_f(x.get(col)) or 0) / 1e8, 4)
                    r["channels"].append({"002": "港股通(沪)", "004": "港股通(深)"}.get(str(x.get("MUTUAL_TYPE")), str(x.get("MUTUAL_TYPE"))))
                    r["close"] = _f(x.get("CLOSE_PRICE"))
                stock = sorted(by.values(), key=lambda r: r["date"], reverse=True)[:days]
                for r in stock:
                    for k in ("net_buy_yi", "buy_yi", "sell_yi"):
                        r[k] = round(r[k], 2)
                out["stock"] = stock
                if stock:
                    out["stock_sum_net_buy_yi"] = round(sum(r["net_buy_yi"] for r in stock), 2)
                    out["stock_days_covered"] = len(stock)
                    out["stock_verdict"] = "净买入" if out["stock_sum_net_buy_yi"] > 0 else "净卖出" if out["stock_sum_net_buy_yi"] < 0 else "持平"
                out["sources"].append(src("东方财富 · 港股通十大成交股", url, stock[0]["date"] if stock else "", ok=bool(stock),
                                          error=None if stock else "近期没有进入港股通十大成交股，个股逐日净买入未披露"))
                out["stock_url"] = url
                out["url"] = url  # 出处链接指向该股的港股通页面
            except Exception as e:
                out["sources"].append(src("东方财富 · 港股通十大成交股", url, ok=False, error=str(e)[:100]))
    if not any(s["ok"] for s in out["sources"]):
        out["error"] = "南向资金数据取不到"
    return out


# ---------------------------------------------------------------- 港交所权益披露
CODE_URL = "https://di.hkex.com.hk/di/NSStdCode_C.htm?ft={ft}#{code}"  # 原因代码说明页（披露易官方）


def _di_date(d: datetime) -> str:
    return d.strftime("%d/%m/%Y")


def parse_di_rows(page: str) -> list[dict]:
    """披露易「所有披露权益通知」表格 → 行。"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        link = re.search(r'href="(NSForm\d?\.aspx\?fn=[^"]+)"', tr)
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        cells = [c for c in cells if c != ""]
        if not cells or not re.fullmatch(r"[A-Z]{2}\d{8}[A-Z]\d{5}", cells[0]):
            continue
        r = {"form_no": cells[0], "holder": cells[1] if len(cells) > 1 else ""}
        if link:
            r["url"] = "https://di.hkex.com.hk/di/" + _html.unescape(link.group(1))
        rest = cells[2:]
        cm = next((re.match(r"(\d{4})\s*\((L|S|P)\)", c) for c in rest if re.match(r"\d{4}\s*\((?:L|S|P)\)", c)), None)
        code = cm.group(1) if cm else ""
        if cm:
            rest = [c for c in rest if not re.match(r"\d{4}\s*\((?:L|S|P)\)", c)]
            r["reason_code"] = code
            r["reason_code_url"] = CODE_URL.format(ft=cells[0][:2], code=code)
        nums = [c for c in rest if re.search(r"\d[\d,]*\((?:L|S|P)\)", c)]
        price = next((c for c in rest if re.match(r"(?:HKD|RMB|USD)\s*[\d.]+", c)), "")
        pct = next((c for c in rest if re.fullmatch(r"\d+(?:\.\d+)?\((?:L|S|P)\)", c)), "")
        date = next((c for c in rest if re.fullmatch(r"\d{2}/\d{2}/\d{4}", c)), "")
        shares = [c for c in nums if c != pct]
        if len(shares) >= 2:
            r["shares_traded"], r["shares_held"] = shares[0], shares[1]
        elif shares:
            r["shares_held"] = shares[0]
        if price:
            r["avg_price"] = price
        if pct:
            r["pct_of_issued"] = pct
        if date:
            r["event_date"] = f"{date[6:]}-{date[3:5]}-{date[:2]}"
        rows.append(r)
    return rows


def hk_interests(symbol: str, days: int = 365) -> dict:
    p = parse(symbol)
    if p["market"] != "HK":
        return {"error": "港交所权益披露只适用于港股（A股大股东减持用 get_corporate_events / get_announcements）"}
    end = now_cn()
    start = end - timedelta(days=max(30, min(int(days or 365), 1500)))
    sc = str(int(p["code"]))
    q = {"sa1": "cl", "scsd": _di_date(start), "sced": _di_date(end), "sc": sc, "src": "MAIN", "lang": "ZH", "g_lang": "zh-HK"}
    first = _fetch(DI_LIST, params=q, headers=UA, ttl=3600, kind="text")
    m = re.search(r'href="(NSAllFormList\.aspx\?[^"]+)"', first)
    if not m:
        return {"symbol": p["yahoo"], "error": "披露易没有找到该股的权益披露记录", "url": f"{DI_LIST}?sa1=cl&scsd={q['scsd']}&sced={q['sced']}&sc={sc}&src=MAIN&lang=ZH"}
    url = "https://di.hkex.com.hk/di/" + _html.unescape(m.group(1))
    page = _fetch(url, headers=UA, ttl=3600, kind="text")
    rows = parse_di_rows(page)
    # 同一持有人按时间看持股变化：减少=减持
    by = {}
    for r in sorted(rows, key=lambda r: r.get("event_date", "")):
        h = by.get(r["holder"])
        held = _f(re.sub(r"[^\d]", "", r.get("shares_held", "")) or None)
        if h is not None and held is not None and r.get("shares_traded"):
            r["change_vs_prev_notice"] = "减少" if held < h else "增加" if held > h else "不变"
        if held is not None:
            by[r["holder"]] = held
    rows.sort(key=lambda r: r.get("event_date", ""), reverse=True)
    return {"symbol": p["yahoo"], "source": "港交所披露易 · 权益披露", "url": url, "as_of": rows[0].get("event_date", "") if rows else end.strftime("%Y-%m-%d"),
            "window": f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}", "notices": rows[:30], "count": len(rows),
            "note": "（L）好仓（S）淡仓（P）可供借出；shares_traded 为该次买卖涉及股数，avg_price 为每股平均价；原因代码含义见 reason_code_url（披露易说明页），"
                    "change_vs_prev_notice 按同一持有人前后两次通知的持股数比较（减少＝减持）；表格序号可在披露易查原通知。A股端（如 688981）大股东减持另看上交所公告。"}


# ---------------------------------------------------------------- 腾讯日线（price_history 备用源）
def tencent_daily(symbol: str, d0: str, d1: str) -> tuple[list[dict], dict]:
    code = tx_code(symbol)
    if not code:
        raise LookupError(f"腾讯日线不支持 {symbol}")
    rows, start = [], datetime.fromisoformat(d0)
    stop = datetime.fromisoformat(d1)
    while start <= stop:  # 每次最多 640 根
        seg_end = min(stop, start + timedelta(days=880))
        j = _fetch(TX_K, params={"param": f"{code},day,{start:%Y-%m-%d},{seg_end:%Y-%m-%d},640,"}, headers=UA, ttl=3600)
        d = ((j.get("data") or {}).get(code) or {})
        for k in d.get("day") or d.get("qfqday") or []:
            if len(k) >= 5 and _f(k[2]) is not None:
                rows.append({"date": k[0], "open": _f(k[1]), "close": _f(k[2]), "high": _f(k[3]), "low": _f(k[4]), "volume": _f(k[5]) if len(k) > 5 else None})
        start = seg_end + timedelta(days=1)
    rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
    if not rows:
        raise LookupError(f"腾讯日线没有 {symbol} 的数据")
    name = next((v[1] for k, v in INDEX_ALIAS.items() if v[0] == code), "")
    return rows, {"symbol": symbol, "name": name, "tencent_code": code, "splits": []}


# ---------------------------------------------------------------- 工具
TOOL_NAMES = {"get_southbound", "hk_interests"}


def run_tool(name: str, a: dict, agent=None):
    if name == "get_southbound":
        return southbound(a.get("symbol"), int(a.get("days") or 5))
    if name == "hk_interests":
        return hk_interests(a["symbol"], int(a.get("days") or 365))
    return {"error": f"未知工具 {name}"}


TOOL_SPECS = [
    {"name": "get_southbound", "description": "南向资金（港股通）：每日净买入/买入/卖出（亿港元，南向合计及沪/深两通道）与近 N 日合计；给港股 symbol 时附该股港股通净买入（只在进入当日十大成交活跃股的日子有数，汇总注明覆盖天数）。"
     "问「南向资金净买还是净卖」「港股通买了多少」时用；南向每日净买入仍在披露（停止披露的是北向）。",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "港股代码，如 0700.HK，可省略只看总额"}, "days": {"type": "integer", "description": "1–30，默认 5"}}}},
    {"name": "hk_interests", "description": "港股大股东/董事增减持：港交所披露易「权益披露」通知（持有人、原因代码、买卖股数、每股均价、持股及占已发行股份%、事件日期），默认近 365 天。"
     "港股「大股东减持/增持」「谁在卖」先用它；A股一侧另查 get_corporate_events / get_announcements。",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string", "description": "港股代码，如 0981.HK"}, "days": {"type": "integer"}}, "required": ["symbol"]}},
]
