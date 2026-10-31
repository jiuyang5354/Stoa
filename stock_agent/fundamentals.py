"""原始报表与专业分析（v0.27）：从三张报表的原始科目出发，所有比率、评分、估值都由代码算。

- statements：标准化三表科目（A股东方财富 F10 原始报表；港美股 Yahoo），A股季报累计口径自动拆成单季。
- earnings_quality：杜邦分解、现金含量、应计比率、应收/存货 vs 营收、非经常性损益占比、
  Beneish M-score、Piotroski F-score、Altman Z，以及报表勾稽检查（资产=负债+权益、现金流三项合计=现金变动等）。
- reverse_dcf：现价（企业价值）隐含的未来自由现金流增速，附折现率敏感性。
- dcf_scenarios：乐观/基准/悲观三情景估值与概率加权价值。
- regulatory_letters：A股/港股问询函、关注函、监管措施、处罚及回复；美股 SEC 意见信（UPLOAD）与公司回复（CORRESP）。
"""
from __future__ import annotations

import re

# ---------------- 标准化科目 ----------------
EM_MAP = {
    "income": {"revenue": "TOTAL_OPERATE_INCOME", "operating_revenue": "OPERATE_INCOME", "cogs": "OPERATE_COST", "taxes_surcharges": "OPERATE_TAX_ADD",
               "selling": "SALE_EXPENSE", "admin": "MANAGE_EXPENSE", "rd": "RESEARCH_EXPENSE", "finance_exp": "FINANCE_EXPENSE",
               "interest_exp": "FE_INTEREST_EXPENSE", "invest_income": "INVEST_INCOME", "fair_value_chg": "FAIRVALUE_CHANGE_INCOME",
               "credit_impair": "CREDIT_IMPAIRMENT_INCOME", "asset_impair": "ASSET_IMPAIRMENT_INCOME", "other_income": "OTHER_INCOME",
               "op_income": "OPERATE_PROFIT", "pretax": "TOTAL_PROFIT", "tax": "INCOME_TAX", "net_income": "NETPROFIT",
               "parent_ni": "PARENT_NETPROFIT", "minority_ni": "MINORITY_INTEREST", "deduct_ni": "DEDUCT_PARENT_NETPROFIT", "eps": "BASIC_EPS"},
    "balance": {"total_assets": "TOTAL_ASSETS", "current_assets": "TOTAL_CURRENT_ASSETS", "cash": "MONETARYFUNDS", "receivables": "ACCOUNTS_RECE",
                "notes_receivables": "NOTE_RECE", "prepayment": "PREPAYMENT", "inventory": "INVENTORY", "ppe": "FIXED_ASSET", "cip": "CIP",
                "intangible": "INTANGIBLE_ASSET", "goodwill": "GOODWILL", "lt_equity_invest": "LONG_EQUITY_INVEST",
                "current_liab": "TOTAL_CURRENT_LIAB", "total_liab": "TOTAL_LIABILITIES", "st_debt": "SHORT_LOAN", "lt_debt": "LONG_LOAN",
                "bonds": "BOND_PAYABLE", "debt_1y": "NONCURRENT_LIAB_1YEAR", "payables": "ACCOUNTS_PAYABLE", "contract_liab": "CONTRACT_LIAB",
                "advance_receipts": "ADVANCE_RECEIVABLES", "equity": "TOTAL_EQUITY", "parent_equity": "TOTAL_PARENT_EQUITY",
                "minority_equity": "MINORITY_EQUITY", "retained": "UNASSIGN_RPOFIT", "share_capital": "SHARE_CAPITAL", "liab_equity": "TOTAL_LIAB_EQUITY"},
    "cashflow": {"cfo": "NETCASH_OPERATE", "sales_cash": "SALES_SERVICES", "capex": "CONSTRUCT_LONG_ASSET", "cfi": "NETCASH_INVEST",
                 "cff": "NETCASH_FINANCE", "dividends_paid": "ASSIGN_DIVIDEND_PORFIT", "fx_effect": "RATE_CHANGE_EFFECT",
                 "cash_change": "CCE_ADD", "begin_cash": "BEGIN_CCE", "end_cash": "END_CCE",
                 "dep": "FA_IR_DEPR", "amort": "IA_AMORTIZE"},
}
YF_MAP = {
    "income": {"revenue": "Total Revenue", "cogs": "Cost Of Revenue", "sga": "Selling General And Administration", "rd": "Research And Development",
               "interest_exp": "Interest Expense", "op_income": "Operating Income", "pretax": "Pretax Income", "tax": "Tax Provision",
               "net_income": "Net Income", "parent_ni": "Net Income Common Stockholders", "eps": "Diluted EPS", "ebit": "EBIT"},
    "balance": {"total_assets": "Total Assets", "current_assets": "Current Assets", "cash": "Cash And Cash Equivalents", "receivables": "Accounts Receivable",
                "inventory": "Inventory", "ppe": "Net PPE", "goodwill": "Goodwill", "intangible": "Other Intangible Assets",
                "current_liab": "Current Liabilities", "total_liab": "Total Liabilities Net Minority Interest", "st_debt": "Current Debt",
                "lt_debt": "Long Term Debt", "total_debt": "Total Debt", "payables": "Accounts Payable", "equity": "Total Equity Gross Minority Interest",
                "parent_equity": "Stockholders Equity", "minority_equity": "Minority Interest", "retained": "Retained Earnings", "shares": "Ordinary Shares Number"},
    "cashflow": {"cfo": "Operating Cash Flow", "capex": "Capital Expenditure", "cfi": "Investing Cash Flow", "cff": "Financing Cash Flow",
                 "dividends_paid": "Cash Dividends Paid", "buyback": "Repurchase Of Capital Stock", "dep": "Depreciation And Amortization",
                 "cash_change": "Changes In Cash", "begin_cash": "Beginning Cash Position", "end_cash": "End Cash Position", "fx_effect": "Effect Of Exchange Rate Changes"},
}
FLOW = ("income", "cashflow")
YTD_NAME = {"03-31": "一季度（累计）", "06-30": "上半年（累计）", "09-30": "前三季度（累计）", "12-31": "全年"}
CALIBER = "revenue=营业总收入，operating_revenue=营业收入（两者不同，金融类子公司的利息收入等只在营业总收入里）；parent_ni=归母净利润，net_income=含少数股东净利润。"
EM_REPORTS = {"income": "RPT_F10_FINANCE_GINCOME", "balance": "RPT_F10_FINANCE_GBALANCE", "cashflow": "RPT_F10_FINANCE_GCASHFLOW"}


def _f(x):
    try:
        x = float(x)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _em_rows(secu: str, kind: str, n: int = 24) -> list[dict]:
    from .research import _get
    r = _get("https://datacenter.eastmoney.com/securities/api/data/v1/get",
             params={"reportName": EM_REPORTS[kind], "columns": "ALL", "filter": f'(SECUCODE="{secu}")', "pageSize": n,
                     "sortColumns": "REPORT_DATE", "sortTypes": "-1"}, ttl=3600)
    return (r.json().get("result") or {}).get("data") or []


def _statements_a(info: dict, freq: str, periods: int) -> dict:
    from .research import secucode
    secu = secucode(info["code"])
    raw = {k: {row["REPORT_DATE"][:10]: row for row in _em_rows(secu, k)} for k in EM_REPORTS}
    dates = sorted(set(raw["income"]) | set(raw["balance"]), reverse=True)
    if not dates:
        raise ValueError("东方财富没有返回报表")

    def pick(kind, d):
        row = raw[kind].get(d) or {}
        return {k: _f(row.get(v)) for k, v in EM_MAP[kind].items()}

    full = {d: {k: pick(k, d) for k in EM_REPORTS} for d in dates}
    out = []
    if freq == "annual":
        for d in [d for d in dates if d.endswith("12-31")][:periods]:
            out.append({"period": d[:4] + "年报", "end": d, "basis": "全年（报表原值）", **full[d]})
    elif freq == "ytd":  # 年初至今累计 = 定期报告原值（问「前三季度/上半年」用这个）
        for d in dates[:periods]:
            out.append({"period": d[:4] + YTD_NAME.get(d[5:], d[5:]), "end": d, "basis": "年初至今累计（报表原值）", **full[d]})
    else:  # 单季：流量科目 = 本期累计 − 上期累计（同一年内），存量科目照抄期末
        for d in dates[:periods]:
            y, md = d[:4], d[5:]
            prev = {"03-31": None, "06-30": f"{y}-03-31", "09-30": f"{y}-06-30", "12-31": f"{y}-09-30"}.get(md)
            q = {"period": f"{y}Q{ {'03-31': 1, '06-30': 2, '09-30': 3, '12-31': 4}.get(md, '?') } 单季", "end": d,
                 "basis": "单季（本期累计−上期累计，代码相减）" if prev else "单季（一季度即累计）", "balance": full[d]["balance"]}
            for k in FLOW:
                cur = full[d][k]
                if prev is None:
                    q[k] = dict(cur)
                elif prev in full:
                    q[k] = {f: (None if cur[f] is None or full[prev][k][f] is None else cur[f] - full[prev][k][f]) for f in cur}
                else:
                    q[k] = {f: None for f in cur}
            q["income"]["eps"] = None if prev else q["income"].get("eps")  # EPS 不能相减
            if prev:  # 同时给出年初至今累计原值，避免把单季数当成「前三季度」（盲评 Q21：投资收益单季 23.43 亿被写成前三季度，实际累计 53.83 亿）
                q["ytd"] = {"period": y + YTD_NAME.get(md, md), **{k: full[d][k] for k in FLOW}}
            out.append(q)
    from .cite import f10_url
    note = {"annual": "年报口径。", "ytd": "A股定期报告原值，年初至今累计口径（前三季度=1–9 月合计）。",
            "quarter": "每期是单季（代码相减）；ytd 是同一报告期的年初至今累计原值。说「前三季度/上半年」时用 ytd，说「第三季度/单季」时用单季，不要混用。"}[freq]
    return {"symbol": info.get("symbol"), "name": info.get("name"), "currency": "CNY", "freq": freq, "periods": out,
            "source": "东方财富 F10 原始报表（利润表/资产负债表/现金流量表）", "url": f10_url(str(info.get("symbol") or "")),
            "note": note, "caliber": CALIBER}


def _statements_yf(symbol: str, info: dict, freq: str, periods: int) -> dict:
    import yfinance as yf
    t = yf.Ticker(symbol)
    dfs = {"income": t.income_stmt, "balance": t.balance_sheet, "cashflow": t.cashflow} if freq == "annual" else \
          {"income": t.quarterly_income_stmt, "balance": t.quarterly_balance_sheet, "cashflow": t.quarterly_cashflow}
    cols = sorted({c for df in dfs.values() if df is not None for c in df.columns}, reverse=True)[:periods]
    out = []
    for c in cols:
        p = {"period": str(c.date()), "end": str(c.date())}
        for k, df in dfs.items():
            p[k] = {f: (_f(df.at[v, c]) if df is not None and v in df.index and c in df.columns else None) for f, v in YF_MAP[k].items()}
        cf = p["cashflow"]
        if cf.get("capex") is not None:
            cf["capex"] = abs(cf["capex"])  # Yahoo 资本开支记为负数，统一成正数（流出）
        if cf.get("dividends_paid") is not None:
            cf["dividends_paid"] = abs(cf["dividends_paid"])
        p["basis"] = "全年" if freq == "annual" else "单季"
        out.append(p)
    try:
        fin_cur = (t.info or {}).get("financialCurrency")
    except Exception:
        fin_cur = None
    return {"symbol": symbol, "name": info.get("name"), "currency": fin_cur, "freq": freq, "periods": out,
            "source": "Yahoo Finance 财务报表", "note": "年报口径。" if freq == "annual" else "单季口径。"}


def statements(symbol: str, freq: str = "annual", periods: int = 4) -> dict:
    from . import research
    info = {**research.resolve(symbol), "symbol": symbol}
    f = str(freq).lower()
    freq = "quarter" if f.startswith("q") else "ytd" if f in ("ytd", "cumulative", "累计") else "annual"
    periods = max(1, min(int(periods or 4), 12))
    if info.get("market") == "A":
        try:
            return _statements_a(info, freq, periods)
        except Exception as e:
            err = str(e)[:100]
            try:
                return {**_statements_yf(symbol, info, freq, periods), "warnings": [f"东方财富失败，改用 Yahoo：{err}"]}
            except Exception:
                return {"symbol": symbol, "error": f"取不到报表：{err}"}
    try:
        return _statements_yf(symbol, info, "quarter" if freq == "ytd" else freq, periods)
    except Exception as e:
        return {"symbol": symbol, "error": f"取不到报表：{str(e)[:100]}"}


def compact(st: dict) -> dict:
    """给模型看的精简版：去掉全空科目。"""
    if st.get("error"):
        return st
    ps = []
    for p in st["periods"]:
        row = {"period": p["period"], **({"basis": p["basis"]} if p.get("basis") else {}),
               **{k: {f: v for f, v in p[k].items() if v is not None} for k in ("income", "balance", "cashflow")}}
        if p.get("ytd"):
            row["ytd"] = {"period": p["ytd"]["period"], **{k: {f: v for f, v in p["ytd"][k].items() if v is not None} for k in FLOW}}
        ps.append(row)
    return {**{k: v for k, v in st.items() if k != "periods"}, "periods": ps}


# ---------------- 盈利质量 ----------------
def _g(d, *ks):
    for k in ks:
        v = (d or {}).get(k)
        if v is not None:
            return v
    return None


def _div(a, b):
    return None if a is None or b in (None, 0) else a / b


def _r(x, n=3):
    return None if x is None else round(x, n)


def _debt(b):
    if b.get("total_debt") is not None:
        return b["total_debt"]
    parts = [b.get(k) for k in ("st_debt", "lt_debt", "bonds", "debt_1y")]
    return sum(x for x in parts if x is not None) if any(x is not None for x in parts) else None


def _sga(i):
    if i.get("sga") is not None:
        return i["sga"]
    parts = [i.get(k) for k in ("selling", "admin")]
    return sum(x for x in parts if x is not None) if any(x is not None for x in parts) else None


def tie_outs(p: dict, tol: float = 0.01) -> list[dict]:
    """报表勾稽：差异超过 tol（相对）就标出来。"""
    i, b, c = p["income"], p["balance"], p["cashflow"]
    checks = []

    def chk(name, lhs, rhs, base=None):
        if lhs is None or rhs is None:
            return
        diff = lhs - rhs
        rel = abs(diff) / max(abs(base or rhs) or 1, 1)
        checks.append({"check": name, "lhs": lhs, "rhs": rhs, "diff": diff, "ok": rel <= tol})

    chk("资产 = 负债 + 权益", b.get("total_assets"), (b.get("total_liab") or 0) + (b.get("equity") or 0) if b.get("total_liab") is not None and b.get("equity") is not None else None)
    if b.get("liab_equity") is not None:
        chk("资产 = 负债和权益合计", b.get("total_assets"), b.get("liab_equity"))
    fx = c.get("fx_effect") or 0  # 东方财富的现金净增加含汇率影响，Yahoo 的不含：两种口径任一对上即可
    if all(c.get(k) is not None for k in ("cfo", "cfi", "cff")) and c.get("cash_change") is not None:
        s3 = c["cfo"] + c["cfi"] + c["cff"]
        best = min((s3, s3 + fx), key=lambda v: abs(v - c["cash_change"]))
        chk("经营+投资+筹资(+汇率) = 现金净增加", best, c["cash_change"], base=c["cfo"])
    if c.get("begin_cash") is not None and c.get("end_cash") is not None:
        s2 = c["begin_cash"] + (c.get("cash_change") or 0)
        chk("期初现金 + 净增加(+汇率) = 期末现金", min((s2, s2 + fx), key=lambda v: abs(v - c["end_cash"])), c["end_cash"])
    if i.get("parent_ni") is not None and i.get("minority_ni") is not None:
        chk("归母 + 少数股东 = 净利润", i["parent_ni"] + i["minority_ni"], i.get("net_income"))
    return checks


def _beneish(t, p):
    """Beneish M-score（8 变量）。t=本年，p=上年。缺折旧时 DEPI 取 1 并注明。"""
    it, ip, bt, bp, ct, cp = t["income"], p["income"], t["balance"], p["balance"], t["cashflow"], p["cashflow"]
    rev_t, rev_p = it.get("revenue"), ip.get("revenue")
    if not rev_t or not rev_p or not bt.get("total_assets") or not bp.get("total_assets"):
        return None, ["缺营收或总资产"]
    notes = []
    dsri = _div(_div(bt.get("receivables"), rev_t), _div(bp.get("receivables"), rev_p)) or 1
    gm = lambda i, r: _div(r - i["cogs"], r) if i.get("cogs") is not None else None
    gmi = _div(gm(ip, rev_p), gm(it, rev_t)) or 1
    hard = lambda b: (b.get("current_assets") or 0) + (b.get("ppe") or 0)
    aqi = _div(1 - hard(bt) / bt["total_assets"], 1 - hard(bp) / bp["total_assets"]) or 1
    sgi = rev_t / rev_p
    dep_t, dep_p = ct.get("dep"), cp.get("dep")
    if dep_t is not None and dep_p is not None and bt.get("ppe") is not None and bp.get("ppe") is not None:
        depi = _div(_div(dep_p, dep_p + bp["ppe"]), _div(dep_t, dep_t + bt["ppe"])) or 1
    else:
        depi = 1
        notes.append("缺折旧，DEPI 取 1")
    sgai = _div(_div(_sga(it), rev_t), _div(_sga(ip), rev_p)) or 1
    lev = lambda b: _div(b.get("total_liab"), b["total_assets"])
    lvgi = _div(lev(bt), lev(bp)) or 1
    ni = _g(it, "net_income")
    tata = _div((ni or 0) - (ct.get("cfo") or 0), bt["total_assets"]) if ni is not None and ct.get("cfo") is not None else 0
    m = -4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi
    comp = {"DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi, "DEPI": depi, "SGAI": sgai, "LVGI": lvgi, "TATA": tata}
    return {"m_score": _r(m, 2), "components": {k: _r(v, 3) for k, v in comp.items()},
            "verdict": "高于 −1.78，存在操纵盈利的统计迹象，需要细查" if m > -1.78 else "低于 −1.78，未见操纵迹象"}, notes


def _piotroski(t, p):
    it, ip, bt, bp, ct = t["income"], p["income"], t["balance"], p["balance"], t["cashflow"]
    ta, tp = bt.get("total_assets"), bp.get("total_assets")
    ni, nip, cfo = it.get("net_income"), ip.get("net_income"), ct.get("cfo")
    sig = {}
    roa, roap = _div(ni, ta), _div(nip, tp)
    sig["ROA>0"] = roa is not None and roa > 0
    sig["经营现金流>0"] = cfo is not None and cfo > 0
    sig["ROA 提升"] = roa is not None and roap is not None and roa > roap
    sig["经营现金流 > 净利润"] = cfo is not None and ni is not None and cfo > ni
    lev = lambda b: _div((_debt(b) or 0), b.get("total_assets"))
    sig["杠杆下降"] = lev(bt) is not None and lev(bp) is not None and lev(bt) <= lev(bp)
    cr = lambda b: _div(b.get("current_assets"), b.get("current_liab"))
    sig["流动比率提升"] = cr(bt) is not None and cr(bp) is not None and cr(bt) > cr(bp)
    sh = lambda b: _g(b, "shares", "share_capital")
    sig["未增发股份"] = sh(bt) is not None and sh(bp) is not None and sh(bt) <= sh(bp) * 1.001
    gm = lambda i: _div(i["revenue"] - i["cogs"], i["revenue"]) if i.get("cogs") is not None and i.get("revenue") else None
    sig["毛利率提升"] = gm(it) is not None and gm(ip) is not None and gm(it) > gm(ip)
    at, atp = _div(it.get("revenue"), ta), _div(ip.get("revenue"), tp)
    sig["资产周转率提升"] = at is not None and atp is not None and at > atp
    score = sum(sig.values())
    return {"f_score": score, "signals": sig, "verdict": "强（≥7）" if score >= 7 else "弱（≤3）" if score <= 3 else "中等"}


def _altman(t, mcap):
    i, b = t["income"], t["balance"]
    ta = b.get("total_assets")
    if not ta or b.get("total_liab") in (None, 0):
        return None
    wc = (b.get("current_assets") or 0) - (b.get("current_liab") or 0)
    ebit = _g(i, "ebit") or ((i.get("pretax") or 0) + (i.get("interest_exp") or 0) if i.get("pretax") is not None else i.get("op_income"))
    z = 1.2 * wc / ta + 1.4 * (b.get("retained") or 0) / ta + 3.3 * (ebit or 0) / ta + (0.6 * mcap / b["total_liab"] if mcap else 0) + 1.0 * (i.get("revenue") or 0) / ta
    return {"z_score": _r(z, 2), "used_market_cap": bool(mcap),
            "verdict": "安全区（>2.99）" if z > 2.99 else "灰色区（1.81–2.99）" if z >= 1.81 else "困境区（<1.81）",
            "note": "原始 Altman Z 针对制造业；金融、轻资产公司参考意义有限。"}


def market_cap(symbol: str, st: dict | None = None) -> dict:
    """市值 = 最新收盘 × 股本（报表最新一期）；港股报表币种与股价不同时按汇率换算。"""
    from .data import fetch_closes
    ser = fetch_closes([symbol], period="1mo").get(symbol)
    price = float(ser.iloc[-1]) if ser is not None and len(ser) else None
    st = st or statements(symbol, "annual", 1)
    shares = None
    for p in st.get("periods") or []:
        shares = _g(p["balance"], "shares", "share_capital")
        if shares:
            break
    out = {"price": price, "shares": shares, "price_as_of": ser.index[-1].strftime("%Y-%m-%d") if ser is not None and len(ser) else None}
    if price and shares:
        out["market_cap"] = price * shares
        fin = st.get("currency")
        if symbol.upper().endswith(".HK") and fin and fin != "HKD":
            try:
                fx = fetch_closes([f"HKD{fin}=X"], period="1mo").get(f"HKD{fin}=X")
                rate = float(fx.iloc[-1])
                out["market_cap_in_report_ccy"] = out["market_cap"] * rate
                out["fx"] = {f"HKD→{fin}": rate}
            except Exception:
                out["warnings"] = [f"报表币种 {fin} 与股价 HKD 不同，汇率没取到，市值未换算"]
    return out


def earnings_quality(symbol: str) -> dict:
    st = statements(symbol, "annual", 4)
    if st.get("error"):
        return st
    ps = st["periods"]
    if len(ps) < 2:
        return {"symbol": symbol, "error": "年报少于两期，算不了同比类指标"}
    t, p = ps[0], ps[1]
    it, bt, ct, ip, bp = t["income"], t["balance"], t["cashflow"], p["income"], p["balance"]
    avg = lambda k: (bt[k] + bp[k]) / 2 if bt.get(k) is not None and bp.get(k) is not None else bt.get(k)
    ni = _g(it, "parent_ni", "net_income")
    rev, rev_p = it.get("revenue"), ip.get("revenue")
    dupont = {"净利率": _r(_div(ni, rev), 4), "总资产周转率": _r(_div(rev, avg("total_assets")), 4),
              "权益乘数": _r(_div(avg("total_assets"), avg("parent_equity") or avg("equity")), 4)}
    if all(v is not None for v in dupont.values()):
        dupont["ROE（乘积）"] = _r(dupont["净利率"] * dupont["总资产周转率"] * dupont["权益乘数"], 4)
    growth = lambda a, b: _r(_div(a - b, abs(b)) if a is not None and b not in (None, 0) else None, 4)
    q = {"经营现金流/净利润": _r(_div(ct.get("cfo"), it.get("net_income")), 3),
         "销售收现/营收": _r(_div(ct.get("sales_cash"), rev), 3),
         "应计比率（(净利−经营现金流)/平均总资产）": _r(_div((it.get("net_income") or 0) - (ct.get("cfo") or 0), avg("total_assets")), 4) if ct.get("cfo") is not None else None,
         "营收同比": growth(rev, rev_p), "应收账款同比": growth(bt.get("receivables"), bp.get("receivables")),
         "存货同比": growth(bt.get("inventory"), bp.get("inventory")),
         "自由现金流（经营现金流−资本开支）": (ct["cfo"] - (ct.get("capex") or 0)) if ct.get("cfo") is not None else None}
    if it.get("deduct_ni") is not None and it.get("parent_ni"):
        q["非经常性损益占归母净利"] = _r((it["parent_ni"] - it["deduct_ni"]) / it["parent_ni"], 4)
    flags = []
    if q["经营现金流/净利润"] is not None and q["经营现金流/净利润"] < 0.8:
        flags.append("经营现金流不到净利润的 80%，利润含金量偏低")
    for k in ("应收账款同比", "存货同比"):
        if q[k] is not None and q["营收同比"] is not None and q[k] - q["营收同比"] > 0.15:
            flags.append(f"{k[:-2]}增速比营收快 {round((q[k] - q['营收同比']) * 100, 1)} 个百分点")
    if q.get("非经常性损益占归母净利") is not None and abs(q["非经常性损益占归母净利"]) > 0.2:
        flags.append("非经常性损益占归母净利超过 20%")
    ben, notes = _beneish(t, p)
    if ben and ben["m_score"] is not None and ben["m_score"] > -1.78:
        flags.append(f"Beneish M-score {ben['m_score']} 高于 −1.78")
    pio = _piotroski(t, p)
    mc = market_cap(symbol, st)
    mcap = mc.get("market_cap_in_report_ccy") or (mc.get("market_cap") if not mc.get("warnings") else None)
    alt = _altman(t, mcap)
    ties = [dict(x, period=pp["period"]) for pp in ps[:3] for x in tie_outs(pp)]
    bad = [x for x in ties if not x["ok"]]
    if bad:
        flags.append(f"{len(bad)} 项报表勾稽对不上：" + "；".join(f"{x['period']} {x['check']}" for x in bad[:3]))
    return {"symbol": symbol, "name": st.get("name"), "period": t["period"], "prior": p["period"], "currency": st.get("currency"),
            "dupont": dupont, "quality": q, "beneish": ben, "piotroski": pio, "altman": alt, "market_cap": mc,
            "tie_outs": {"checked": len(ties), "failed": bad[:5]}, "flags": flags, "notes": notes,
            "source": st["source"], "method": "全部由三张报表原始科目按公式计算；口径：最新两期年报。"}


# ---------------- 估值 ----------------
def _pv(fcf0, g, years, r, tg, fade=True):
    """两段式：前 years 年增速从 g 线性衰减到 tg（fade=True）或恒定 g，之后按 tg 永续。"""
    pv, f = 0.0, fcf0
    for y in range(1, years + 1):
        gy = g + (tg - g) * (y - 1) / max(1, years - 1) if fade else g
        f *= 1 + gy
        pv += f / (1 + r) ** y
    tv = f * (1 + tg) / (r - tg)
    return pv + tv / (1 + r) ** years


def _base_fcf(st: dict, years: int = 3) -> tuple[float | None, list]:
    vals = []
    for p in st["periods"][:years]:
        c = p["cashflow"]
        if c.get("cfo") is not None:
            vals.append((p["period"], c["cfo"] - (c.get("capex") or 0)))
    return (sum(v for _, v in vals) / len(vals) if vals else None), vals


def _ev_parts(symbol, st):
    b = st["periods"][0]["balance"]
    mc = market_cap(symbol, st)
    mcap = mc.get("market_cap_in_report_ccy") or mc.get("market_cap")
    debt, cash = _debt(b) or 0, b.get("cash") or 0
    minority = b.get("minority_equity") or 0
    return mc, mcap, debt, cash, minority


def _default_r(symbol):
    s = symbol.upper()
    return 0.09 if s.endswith((".SS", ".SZ", ".BJ", ".HK")) else 0.085


def reverse_dcf(symbol: str, discount: float | None = None, terminal_growth: float = 0.025, years: int = 10, fcf_years: int = 3) -> dict:
    st = statements(symbol, "annual", 4)
    if st.get("error"):
        return st
    fcf0, hist = _base_fcf(st, fcf_years)
    if not fcf0 or fcf0 <= 0:
        return {"symbol": symbol, "error": "近几年平均自由现金流不为正，反向 DCF 没有意义；改看营收和利润率情景", "fcf_history": hist}
    mc, mcap, debt, cash, minority = _ev_parts(symbol, st)
    if not mcap:
        return {"symbol": symbol, "error": "算不出市值（缺股价或股本）", "market_cap": mc}
    ev = mcap + debt - cash + minority
    r = float(discount or _default_r(symbol))
    tg = float(terminal_growth)

    def solve(rr):
        lo, hi = -0.5, 1.5
        if _pv(fcf0, hi, years, rr, tg) < ev:
            return None
        for _ in range(80):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if _pv(fcf0, mid, years, rr, tg) < ev else (lo, mid)
        return (lo + hi) / 2

    g = solve(r)
    sens = {f"{round(x * 100, 1)}%": _r(solve(x), 4) for x in (r - 0.02, r - 0.01, r, r + 0.01, r + 0.02) if x > tg + 0.005}
    rev = [p["income"].get("revenue") for p in st["periods"]]
    hist_cagr = None
    if len(rev) >= 3 and rev[0] and rev[-1] and rev[-1] > 0:
        hist_cagr = (rev[0] / rev[-1]) ** (1 / (len(rev) - 1)) - 1
    return {"symbol": symbol, "name": st.get("name"), "currency": st.get("currency"),
            "implied_initial_growth": _r(g, 4), "discount_rate": r, "terminal_growth": tg, "years": years,
            "fcf_base": fcf0, "fcf_history": hist, "enterprise_value": ev,
            "ev_parts": {"market_cap": mcap, "debt": debt, "cash": cash, "minority": minority},
            "sensitivity_by_discount": sens, "hist_revenue_cagr": _r(hist_cagr, 4), "market_cap_detail": mc,
            "method": f"两段式 DCF：自由现金流=经营现金流−资本开支（近 {len(hist)} 年平均），前 {years} 年增速从隐含值线性降到永续增速 {tg:.1%}，二分法反解使现值=企业价值。",
            "note": "隐含增速是「现价要求公司做到什么」，不是预测。折现率是假设，看敏感性。", "source": st["source"]}


def dcf_scenarios(symbol: str, scenarios: list[dict] | None = None, discount: float | None = None, terminal_growth: float = 0.025, years: int = 10) -> dict:
    st = statements(symbol, "annual", 4)
    if st.get("error"):
        return st
    fcf0, hist = _base_fcf(st)
    if not fcf0 or fcf0 <= 0:
        return {"symbol": symbol, "error": "自由现金流不为正，DCF 情景不适用", "fcf_history": hist}
    mc, mcap, debt, cash, minority = _ev_parts(symbol, st)
    shares = mc.get("shares")
    r0 = float(discount or _default_r(symbol))
    defaulted = not scenarios
    if defaulted:
        rev = [p["income"].get("revenue") for p in st["periods"]]
        h = (rev[0] / rev[-1]) ** (1 / (len(rev) - 1)) - 1 if len(rev) >= 3 and rev[0] and rev[-1] and rev[-1] > 0 else 0.05
        scenarios = [{"name": "悲观", "prob": 0.25, "growth": min(h * 0.4, h - 0.05)}, {"name": "基准", "prob": 0.5, "growth": h},
                     {"name": "乐观", "prob": 0.25, "growth": h * 1.5 if h > 0 else h + 0.05}]
    out, wsum, psum = [], 0.0, 0.0
    fx = (mc.get("fx") or {})
    rate = next(iter(fx.values()), None)
    for s in scenarios:
        r = float(s.get("discount") or r0)
        g = float(s.get("growth", 0))
        tg = float(s.get("terminal_growth", terminal_growth))
        mult = float(s.get("fcf_multiplier", 1))  # 例如利润率改善 → 基期现金流 ×1.1
        ev = _pv(fcf0 * mult, g, int(s.get("years", years)), r, tg)
        eq = ev - debt + cash - minority
        ps = eq / shares if shares else None
        if ps is not None and rate:
            ps = ps / rate  # 报表币种 → 港币
        prob = float(s.get("prob", 1 / len(scenarios)))
        out.append({"name": s.get("name"), "prob": prob, "growth": g, "discount": r, "terminal_growth": tg, "equity_value": eq, "per_share": _r(ps, 2)})
        if ps is not None:
            wsum += prob * ps
            psum += prob
    weighted = wsum / psum if psum else None
    price = mc.get("price")
    return {"symbol": symbol, "name": st.get("name"), "scenarios": out, "weighted_per_share": _r(weighted, 2), "price": price,
            "upside_pct": _r((weighted / price - 1) * 100, 2) if weighted and price else None, "fcf_base": fcf0,
            "defaulted_scenarios": defaulted, "source": st["source"],
            "note": ("情景假设由代码按历史营收复合增速默认生成（悲观/基准/乐观），应换成你自己的判断。" if defaulted else "情景假设由调用方给定。")
                    + " 每股价值按股价币种。"}


# ---------------- 监管文件 ----------------
REG_PAT = re.compile(r"问询函|关注函|监管函|警示函|监管工作函|监管措施|纪律处分|行政处罚|处罚决定|立案|责令改正|通报批评|公开谴责|审核问询|inquiry|regulatory", re.I)


def regulatory_letters(symbol: str, pages: int = 4) -> dict:
    from . import research
    info = research.resolve(symbol)
    mk, code = info["market"], info["code"]
    hits = []
    if mk in ("A", "HK"):
        for page in range(1, max(1, min(pages, 8)) + 1):
            r = research._get("https://np-anotice-stock.eastmoney.com/api/security/ann",
                              params={"sr": -1, "page_size": 100, "page_index": page, "ann_type": "A" if mk == "A" else "H",
                                      "client_source": "web", "stock_list": code}, ttl=3600)
            lst = (r.json().get("data") or {}).get("list") or []
            for a in lst:
                t = research._clean(a["title"])
                if REG_PAT.search(t):
                    hits.append({"time": a["display_time"][:10], "title": t, "is_reply": "回复" in t,
                                 "url": f"https://data.eastmoney.com/notices/detail/{code}/{a['art_code']}.html"})
            if len(lst) < 100:
                break
        span = f"最近 {pages * 100} 条公告"
        return {"symbol": symbol, "name": info["name"], "letters": hits, "count": len(hits), "scanned": span, "source": "东方财富公告中心",
                "note": "没有命中不代表没有监管关注（交易所也会在官网单独公示）；回复函里通常有公司对问题的逐条解释，值得用 fetch_url 读原文。"}
    hit = research._cik(code)
    if not hit:
        return {"symbol": symbol, "error": "SEC 里找不到这个代码"}
    cik, title = hit
    r = research._get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=research.SEC_UA)
    rec = r.json()["filings"]["recent"]
    for i, form in enumerate(rec["form"]):
        if form in ("UPLOAD", "CORRESP"):
            acc = rec["accessionNumber"][i].replace("-", "")
            hits.append({"time": rec["filingDate"][i], "title": "SEC 意见信" if form == "UPLOAD" else "公司回复 SEC", "form": form, "is_reply": form == "CORRESP",
                         "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{rec['primaryDocument'][i]}"})
        if len(hits) >= 20:
            break
    return {"symbol": symbol, "name": title, "letters": hits, "count": len(hits), "source": "SEC EDGAR（UPLOAD/CORRESP）",
            "note": "SEC 意见信一般在审阅结束至少 20 个工作日后才公开。"}
