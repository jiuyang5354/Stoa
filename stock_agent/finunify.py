"""财务字段统一口径层（v0.37）：不管哪个源，get_financials 的 unified 都用同一套英文字段名；源差异在 adapter 里消化。

FIELDS：revenue 营业总收入（元/原币）· revenue_yoy % · net_profit 归母净利润 · net_profit_yoy % · deducted_net_profit 扣非净利 ·
gross_margin % · net_margin % · roe %（加权）· eps · bps · debt_ratio %（资产负债率）· ocf_ps 每股经营现金流。
cross() 套用报价交叉校验（marketdata.cross_check）的做法：同一期同一字段两源都有时比差额，超过 FIN_DIFF_PCT（默认 5%）写 warnings，
不静默挑一个；卡片上列出各源数值与差异。
"""
from __future__ import annotations

import os

FIELDS = ("revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "deducted_net_profit", "gross_margin", "net_margin", "roe", "eps", "bps", "debt_ratio", "ocf_ps")
LABELS = {"revenue": "营业总收入", "revenue_yoy": "营收同比%", "net_profit": "归母净利润", "net_profit_yoy": "净利同比%", "deducted_net_profit": "扣非净利润",
          "gross_margin": "毛利率%", "net_margin": "净利率%", "roe": "ROE%", "eps": "每股收益", "bps": "每股净资产", "debt_ratio": "资产负债率%", "ocf_ps": "每股经营现金流"}
# 只拿口径一致的字段做双源比较：同比、扣非等各源定义差异大，不比
COMPARE = ("revenue", "net_profit", "gross_margin", "roe", "eps")  # 净利率不比：东财 XSJLL 是含少数股东的销售净利率，Yahoo 是归母口径


def diff_pct() -> float:
    try:
        return float(os.getenv("FIN_DIFF_PCT") or 5)
    except ValueError:
        return 5.0


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


EM_MAP = {"TOTALOPERATEREVE": "revenue", "TOTALOPERATEREVETZ": "revenue_yoy", "PARENTNETPROFIT": "net_profit", "PARENTNETPROFITTZ": "net_profit_yoy",
          "KCFJCXSYJLR": "deducted_net_profit", "XSMLL": "gross_margin", "XSJLL": "net_margin", "ROEJQ": "roe", "EPSJB": "eps", "BPS": "bps",
          "ZCFZL": "debt_ratio", "MGJYXJJE": "ocf_ps"}


def from_eastmoney(raw_rows: list[dict]) -> list[dict]:
    """东方财富 F10 主要指标（累计口径，A股人民币）。"""
    out = []
    for r in raw_rows or []:
        p = str(r.get("REPORT_DATE") or "")[:10]
        if not p:
            continue
        row = {"period": p, "basis": "cumulative", "currency": "CNY"}
        for k, f in EM_MAP.items():
            v = _f(r.get(k))
            if v is not None:
                row[f] = v
        out.append(row)
    return out


def from_yahoo_annual(stmt, currency: str | None = None) -> list[dict]:
    """yfinance income_stmt（年度）→ 统一字段。stmt 是 DataFrame（index 科目，columns 财年末日期）。"""
    out = []
    if stmt is None or getattr(stmt, "empty", True):
        return out
    for c in list(stmt.columns)[:4]:
        def g(name):
            return _f(stmt.at[name, c]) if name in stmt.index else None
        rev, gp, ni = g("Total Revenue"), g("Gross Profit"), g("Net Income Common Stockholders") or g("Net Income")
        row = {"period": str(getattr(c, "date", lambda: c)())[:10], "basis": "annual", "currency": currency}
        if rev is not None:
            row["revenue"] = rev
        if ni is not None:
            row["net_profit"] = ni
        if rev and gp is not None:
            row["gross_margin"] = round(gp / rev * 100, 4)
        if rev and ni is not None:
            row["net_margin"] = round(ni / rev * 100, 4)
        eps = g("Diluted EPS") or g("Basic EPS")
        if eps is not None:
            row["eps"] = eps
        out.append(row)
    return out


def cross(by_source: dict[str, list[dict]], threshold: float | None = None) -> dict:
    """by_source = {来源名: [统一行]}。同一 period 同一字段 ≥2 源时比较；返回 items（全部比较）与 warnings（超阈值）。"""
    from .marketdata import cross_check
    thr = diff_pct() if threshold is None else threshold
    periods: dict[str, dict[str, dict]] = {}
    for src, rows in by_source.items():
        for r in rows or []:
            periods.setdefault(r["period"], {})[src] = r
    items, warns = [], []
    for p in sorted(periods, reverse=True):
        srcs = periods[p]
        if len(srcs) < 2:
            continue
        for f in COMPARE:
            vals = {s: r[f] for s, r in srcs.items() if isinstance(r.get(f), (int, float))}
            if len(vals) < 2:
                continue
            spread, w = cross_check(vals, thr, f"{p} {LABELS[f]}")
            items.append({"period": p, "field": f, "label": LABELS[f], "values": vals, "spread_pct": spread, "flag": bool(w)})
            if w:
                warns.append(w[0].replace("请以交易所/公司公告为准", "请以公司定期报告原文为准"))
    return {"threshold_pct": thr, "compared": len(items), "items": items[:24], "warnings": warns[:6]}
