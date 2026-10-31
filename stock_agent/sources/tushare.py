"""Tushare Pro（可选）：只有用户在设置 → 行情源里填了自己的 TUSHARE_TOKEN 才启用；不内置、不付费。

遵循 data.DATA_PROVIDERS 的约定：daily() 返回与其它源同格式的收盘序列（拿够 MIN_ROWS 由 data.history 决定停不停），
financials() 返回统一口径字段（finunify.FIELDS），由 research.financials 并列展示并与免费源交叉校验。
积分/权限不够、网络不通都只返回 None 或 error，不影响免费源。
"""
from __future__ import annotations

import os

import requests

URL_ENV = "TUSHARE_URL"


class TushareError(RuntimeError):
    pass


def enabled() -> bool:
    return bool((os.getenv("TUSHARE_TOKEN") or "").strip())


def query(api: str, params: dict, fields: str = "") -> list[dict]:
    token = (os.getenv("TUSHARE_TOKEN") or "").strip()
    if not token:
        raise TushareError("未配置 TUSHARE_TOKEN")
    r = requests.post(os.getenv(URL_ENV, "http://api.tushare.pro"), timeout=20,
                      json={"api_name": api, "token": token, "params": params, "fields": fields})
    j = r.json()
    if j.get("code") not in (0, None):
        raise TushareError(f"Tushare {api}：{j.get('msg') or j.get('code')}")
    d = j.get("data") or {}
    cols = d.get("fields") or []
    return [dict(zip(cols, row)) for row in d.get("items") or []]


def ts_code(symbol: str) -> str | None:
    s = symbol.strip().upper()
    if s.endswith(".SS") or s.endswith(".SH"):
        return s[:6] + ".SH"
    if s.endswith(".SZ"):
        return s[:6] + ".SZ"
    if s.endswith(".BJ"):
        return s[:6] + ".BJ"
    if s.isdigit() and len(s) == 6:
        return s + (".SH" if s[0] in "569" else ".BJ" if s[0] in "48" else ".SZ")
    return None


def financials(symbol: str, periods: int = 4) -> dict:
    """A股：income（营业总收入、归母净利）+ fina_indicator（毛利率、净利率、ROE、EPS、BPS、资产负债率、同比）→ 统一字段。"""
    code = ts_code(symbol)
    if not code:
        return {"error": "Tushare 财务只接 A股"}
    try:
        inc = query("income", {"ts_code": code}, "ts_code,end_date,report_type,total_revenue,revenue,n_income_attr_p")
        ind = query("fina_indicator", {"ts_code": code},
                    "ts_code,end_date,grossprofit_margin,netprofit_margin,roe,roe_waa,eps,bps,debt_to_assets,or_yoy,netprofit_yoy,profit_dedt,ocfps")
    except Exception as e:
        return {"error": str(e)[:160]}
    by: dict[str, dict] = {}
    for r in inc:
        if str(r.get("report_type") or "1") != "1":
            continue
        p = _d(r.get("end_date"))
        by.setdefault(p, {}).update({"revenue": r.get("total_revenue") or r.get("revenue"), "net_profit": r.get("n_income_attr_p")})
    for r in ind:
        p = _d(r.get("end_date"))
        by.setdefault(p, {}).update({"gross_margin": r.get("grossprofit_margin"), "net_margin": r.get("netprofit_margin"),
                                     "roe": r.get("roe_waa") if r.get("roe_waa") is not None else r.get("roe"), "eps": r.get("eps"), "bps": r.get("bps"),
                                     "debt_ratio": r.get("debt_to_assets"), "revenue_yoy": r.get("or_yoy"), "net_profit_yoy": r.get("netprofit_yoy"),
                                     "deducted_net_profit": r.get("profit_dedt"), "ocf_ps": r.get("ocfps")})
    rows = [{"period": p, **{k: v for k, v in f.items() if v is not None}} for p, f in sorted(by.items(), reverse=True)][:periods]
    return {"source": "Tushare Pro", "rows": rows}


def daily(symbol: str, start: str, fields: str = "trade_date,close") -> list[tuple]:
    from ..data import A_INDEX, _split
    mkt, code = _split(symbol)
    if mkt in ("sh", "sz"):
        api, ts = ("index_daily" if symbol.upper() in A_INDEX else "daily"), f"{code}.{mkt.upper()}"
    elif mkt == "hk":
        api, ts = "hk_daily", f"{code.zfill(5)}.HK"
    elif mkt == "us":
        api, ts = "us_daily", code
    else:
        return []
    rows = query(api, {"ts_code": ts, "start_date": start}, fields)
    return [(r["trade_date"], r["close"]) for r in rows]


def _d(s) -> str:
    s = str(s or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s[:10]
