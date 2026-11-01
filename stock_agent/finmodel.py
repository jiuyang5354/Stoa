"""v0.38 可比公司倍数 + 财务建模（Vals Comparables / Financial Modeling 两类全 0 的修复）。

一个工具 fin_model，四种 mode，全部确定性计算、每一步都列出公式与输入（结果可逐行复算）：
- comps     同行倍数表：市值、EV（可含经营租赁）、P/E、P/B、EV/EBITDA、EV/Sales、EV/EBIT、D/E、D/总资本、EBITDA 利润率；
            每个指标给 均值 / 中位数 / 区间 / n，每家公司相对均值；价格日期、报表日期、币种不一致写 warnings；可选 target 用同行中位数倍数推隐含价值。
- dcf       无杠杆自由现金流 DCF：逐年营收 → EBITDA → EBIT → NOPAT → +D&A −CapEx −ΔNWC（+递延税 +其他经营项 −SBC 按题意）→ 折现；
            终值 退出倍数 / 永续增长 / 两者；期末或年中折现；EV → 股权价值（−债务 +现金 −少数股东 −优先股）→ 每股。
- lbo       简化 LBO：收购价（现价×(1+溢价)）× 股本 → 资金用途（股权 + 再融资旧债 + 交易费用）与来源（新债 + 超出最低现金的账上现金 + 发起人股权）；
            逐年营收/EBITDA 利润率路径、利息按期初期末平均余额（循环引用迭代求解）、现金利息收入、税、FCF 全部扫债；退出 EV / 净债务 / 股权价值、MOIC、IRR。
- coverage  FCCR / 利息保障：EBITDAR ÷（利息 + 租金 [+ 本金]）等，按给定口径算并写明。
口径默认值写在每个结果的 conventions 里（题目另有规定时由模型传参覆盖）。
"""
from __future__ import annotations

import math
import statistics

METRICS = ("market_cap", "ev", "pe", "pb", "ev_ebitda", "ev_sales", "ev_ebit", "debt_to_equity", "debt_to_capital", "ebitda_margin")
LABEL = {"market_cap": "市值", "ev": "企业价值 EV", "pe": "P/E", "pb": "P/B", "ev_ebitda": "EV/EBITDA", "ev_sales": "EV/Sales", "ev_ebit": "EV/EBIT",
         "debt_to_equity": "负债/权益", "debt_to_capital": "负债/总资本", "ebitda_margin": "EBITDA 利润率"}
FORMULA = {"market_cap": "price × shares", "ev": "market_cap + debt (+ operating_leases 若 include_leases) + preferred + minority − cash",
           "pe": "market_cap ÷ net_income（或 price ÷ eps）", "pb": "market_cap ÷ equity（普通股权益）", "ev_ebitda": "EV ÷ EBITDA",
           "ev_sales": "EV ÷ revenue", "ev_ebit": "EV ÷ EBIT", "debt_to_equity": "debt ÷ equity", "debt_to_capital": "debt ÷ (debt + equity)",
           "ebitda_margin": "EBITDA ÷ revenue"}
PCT = {"debt_to_capital", "ebitda_margin"}


def _n(x):
    if x is None or x == "":
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(float(x)) else None
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "mean": _r(statistics.fmean(vals)), "median": _r(statistics.median(vals)), "min": _r(min(vals)), "max": _r(max(vals))}


# ====================================================================== comps
def comps(companies: list[dict], metrics=None, include_leases: bool = False, target: dict | None = None, exclude_negative: bool = True) -> dict:
    if not companies:
        return {"error": "companies 不能为空：每家给 name 和用到的输入（price, shares, debt, cash, equity, revenue, ebitda, net_income…）"}
    want = [m for m in (metrics or METRICS) if m in METRICS]
    rows, warns = [], []
    for c in companies:
        g = {k: _n(c.get(k)) for k in ("price", "shares", "market_cap", "debt", "cash", "equity", "preferred", "minority", "operating_leases",
                                         "revenue", "ebitda", "ebit", "net_income", "eps", "operating_income", "da")}
        name = str(c.get("name") or c.get("symbol") or f"#{len(rows) + 1}")
        if g["ebitda"] is None and g["operating_income"] is not None and g["da"] is not None:
            g["ebitda"] = g["operating_income"] + g["da"]
        if g["ebit"] is None and g["operating_income"] is not None:
            g["ebit"] = g["operating_income"]
        mcap = g["market_cap"] if g["market_cap"] is not None else (g["price"] * g["shares"] if g["price"] is not None and g["shares"] is not None else None)
        ev = None
        if mcap is not None and g["debt"] is not None and g["cash"] is not None:
            ev = mcap + g["debt"] + ((g["operating_leases"] or 0) if include_leases else 0) + (g["preferred"] or 0) + (g["minority"] or 0) - g["cash"]
        if include_leases and g["operating_leases"] is None:
            warns.append(f"{name}：include_leases=true 但没给 operating_leases，EV 未含租赁")
        pe = _div(mcap, g["net_income"]) if g["net_income"] is not None else _div(g["price"], g["eps"])
        val = {"market_cap": mcap, "ev": ev, "pe": pe, "pb": _div(mcap, g["equity"]), "ev_ebitda": _div(ev, g["ebitda"]), "ev_sales": _div(ev, g["revenue"]),
               "ev_ebit": _div(ev, g["ebit"]), "debt_to_equity": _div(g["debt"], g["equity"]),
               "debt_to_capital": _div(g["debt"], (g["debt"] + g["equity"]) if g["debt"] is not None and g["equity"] is not None else None),
               "ebitda_margin": _div(g["ebitda"], g["revenue"])}
        row = {"name": name, **{k: _r(val[k], 6 if k in PCT else 4) for k in want}}
        for k in PCT & set(want):
            if row.get(k) is not None:
                row[k + "_pct"] = round(row[k] * 100, 2)
        row["inputs"] = {k: v for k, v in g.items() if v is not None}
        for k in ("price_date", "balance_date", "period", "currency", "source"):
            if c.get(k):
                row[k] = c[k]
        rows.append(row)
    for k in ("price_date", "balance_date", "currency"):
        vs = {r.get(k) for r in rows if r.get(k)}
        if len(vs) > 1:
            warns.append(f"{k} 不一致：{sorted(vs)}——确认是题目要求的口径（如各自最近一期报表），否则统一后再比")
    stats, neg = {}, {}
    for k in want:
        vals = [r[k] for r in rows if r.get(k) is not None]
        if exclude_negative and k in ("pe", "pb", "ev_ebitda", "ev_sales", "ev_ebit"):
            bad = [r["name"] for r in rows if r.get(k) is not None and r[k] <= 0]
            if bad:
                neg[k] = bad
                vals = [v for v in vals if v > 0]
        stats[k] = _stats(vals)
        if k in PCT and stats[k].get("n"):
            stats[k].update({s + "_pct": round(stats[k][s] * 100, 2) for s in ("mean", "median", "min", "max")})
    for r in rows:  # 相对同行均值（倍数越高=越贵/杠杆越高）
        r["vs_mean"] = {k: _r(r[k] / stats[k]["mean"], 3) for k in want if r.get(k) is not None and stats[k].get("mean") not in (None, 0)}
    rank = {}
    for k in want:
        have = [r for r in rows if r.get(k) is not None]
        if len(have) >= 2:
            rank[k] = [r["name"] for r in sorted(have, key=lambda r: -r[k])]
    out = {"mode": "comps", "rows": rows, "stats": stats, "rank_desc": rank, "formulas": {k: FORMULA[k] for k in want},
           "conventions": ["peer average = 算术均值（mean），同时给中位数（median）；题目说 peer average/均值 用 mean",
                           "equity = 普通股权益（总权益 − 优先股 − 少数股东），题目说 total equity 时传总权益",
                           "debt = 有息负债（短期+长期借款/债券；题目要求时再加融资租赁），operating_leases 只在 include_leases=true 时计入 EV",
                           "负值倍数默认不计入统计（exclude_negative），已在 excluded_negative 列出"],
           "warnings": warns}
    if neg:
        out["excluded_negative"] = neg
    if target:
        out["implied"] = implied_from_peers(target, stats, include_leases)
    return out


def implied_from_peers(t: dict, stats: dict, include_leases: bool = False) -> dict:
    g = {k: _n(t.get(k)) for k in ("ebitda", "revenue", "net_income", "equity", "debt", "cash", "preferred", "minority", "operating_leases", "shares", "price")}
    bridge = (g["debt"] or 0) + ((g["operating_leases"] or 0) if include_leases else 0) + (g["preferred"] or 0) + (g["minority"] or 0) - (g["cash"] or 0)
    res = {}
    for k, base, ev_based in (("ev_ebitda", "ebitda", True), ("ev_sales", "revenue", True), ("pe", "net_income", False), ("pb", "equity", False)):
        st = stats.get(k) or {}
        if st.get("median") is None or g[base] is None:
            continue
        lo, med, hi = st["min"] * g[base], st["median"] * g[base], st["max"] * g[base]
        eq = [x - bridge for x in (lo, med, hi)] if ev_based else [lo, med, hi]
        item = {"multiple_median": st["median"], "range": [st["min"], st["max"]], "equity_low": _r(eq[0], 2), "equity_median": _r(eq[1], 2), "equity_high": _r(eq[2], 2)}
        if g["shares"]:
            item.update({"per_share_low": _r(eq[0] / g["shares"], 4), "per_share_median": _r(eq[1] / g["shares"], 4), "per_share_high": _r(eq[2] / g["shares"], 4)})
            if g["price"]:
                item["upside_median_pct"] = _r((item["per_share_median"] / g["price"] - 1) * 100, 2)
        res[k] = item
    return {"name": t.get("name") or t.get("symbol"), "net_debt_bridge": _r(bridge, 4), "by_multiple": res,
            "formula": "EV 类倍数：隐含 EV = 同行倍数 × 目标指标，股权 = EV − (债务 + 优先股 + 少数股东 − 现金)；P/E、P/B 直接得股权价值；每股 = 股权 ÷ 股本"}


# ====================================================================== dcf
def _series(v, n, name):
    if isinstance(v, (list, tuple)):
        vals = [_n(x) for x in v]
        if len(vals) < n:
            vals += [vals[-1]] * (n - len(vals))
        return vals[:n]
    x = _n(v)
    return [x] * n if x is not None else [None] * n


def dcf(a: dict) -> dict:
    n = int(a.get("years") or 5)
    base = _n(a.get("base_revenue"))
    revs = a.get("revenues")
    if revs:
        rev = [_n(x) for x in revs][:n]
        n = len(rev)
    else:
        if base is None:
            return {"error": "给 base_revenue（基期营收）+ growth，或直接给 revenues 列表"}
        gr = _series(a.get("growth", 0), n, "growth")
        rev, r = [], base
        for g in gr:
            r = r * (1 + (g or 0))
            rev.append(r)
    em = _series(a.get("ebitda_margin"), n, "ebitda_margin")
    ebm = _series(a.get("ebit_margin"), n, "ebit_margin")
    da_p = _series(a.get("da_pct", 0), n, "da_pct")
    cap_p = _series(a.get("capex_pct", 0), n, "capex_pct")
    nwc_p = _series(a.get("nwc_change_pct", 0), n, "nwc_change_pct")  # ΔNWC 占当年营收 %（正数=投资，减现金）
    sbc_p = _series(a.get("sbc_pct", 0), n, "sbc_pct")
    tax = _series(a.get("tax_rate", 0.25), n, "tax_rate")
    other = _series(a.get("other_cf", 0), n, "other_cf")  # 每年固定加回的其他经营项（如递延税、其他），正数加、负数减
    deferred = _series(a.get("deferred_tax", 0), n, "deferred_tax")
    wacc = _n(a.get("wacc"))
    if wacc is None:
        return {"error": "给 wacc（折现率，如 0.08）"}
    mid = bool(a.get("mid_year"))
    sbc_mode = str(a.get("sbc_treatment") or "ignore")  # ignore | expense（作为现金成本扣除）
    rows, pv_sum = [], 0.0
    for i in range(n):
        R = rev[i]
        da = R * (da_p[i] or 0)
        if em[i] is not None:
            ebitda = R * em[i]
            ebit = ebitda - da
        elif ebm[i] is not None:
            ebit = R * ebm[i]
            ebitda = ebit + da
        else:
            return {"error": "给 ebitda_margin 或 ebit_margin"}
        t = tax[i] or 0
        nopat = ebit * (1 - t)
        capex = R * (cap_p[i] or 0)
        dnwc = R * (nwc_p[i] or 0)
        sbc = R * (sbc_p[i] or 0)
        fcf = nopat + da - capex - dnwc + (other[i] or 0) + (deferred[i] or 0) - (sbc if sbc_mode == "expense" else 0)
        per = i + 0.5 if mid else i + 1
        df = 1 / (1 + wacc) ** per
        pv = fcf * df
        pv_sum += pv
        rows.append({"year": i + 1, **({"label": a["labels"][i]} if a.get("labels") and i < len(a["labels"]) else {}), "revenue": _r(R, 4),
                     "growth_pct": _r(((R / (rev[i - 1] if i else base)) - 1) * 100, 4) if (i or base) else None, "ebitda": _r(ebitda, 4), "da": _r(da, 4),
                     "ebit": _r(ebit, 4), "tax": _r(ebit * t, 4), "nopat": _r(nopat, 4), "capex": _r(capex, 4), "delta_nwc": _r(dnwc, 4),
                     **({"sbc": _r(sbc, 4)} if sbc_mode == "expense" else {}), "other": _r((other[i] or 0) + (deferred[i] or 0), 4),
                     "ufcf": _r(fcf, 4), "discount_period": per, "discount_factor": _r(df, 6), "pv": _r(pv, 4)})
    term = a.get("terminal") or {}
    method = str(term.get("method") or ("both" if term.get("multiple") and term.get("growth") is not None else "exit" if term.get("multiple") else "gordon"))
    last = rows[-1]
    tv_df = 1 / (1 + wacc) ** n  # 终值按第 n 年末折现（年中折现时退出倍数法仍按期末，永续法按约定）
    res = {}
    debt, cash = _n(a.get("debt")) or 0, _n(a.get("cash")) or 0
    minority, pref = _n(a.get("minority")) or 0, _n(a.get("preferred")) or 0
    other_adj = _n(a.get("other_equity_adjustments")) or 0  # 正数加到股权（如投资），负数减
    shares = _n(a.get("shares"))

    def bridge(tv, label, formula):
        pv_tv = tv * tv_df
        ev = pv_sum + pv_tv
        eq = ev - debt + cash - minority - pref + other_adj
        d = {"terminal_value": _r(tv, 4), "pv_terminal": _r(pv_tv, 4), "enterprise_value": _r(ev, 4), "equity_value": _r(eq, 4),
             "tv_share_of_ev_pct": _r(pv_tv / ev * 100, 2) if ev else None, "formula": formula}
        if shares:
            d["per_share"] = _r(eq / shares, 4)
        res[label] = d

    if method in ("exit", "both"):
        m = _n(term.get("multiple"))
        if m is None:
            return {"error": "退出倍数法要给 terminal.multiple"}
        bridge(m * last["ebitda"], "exit_multiple", f"TV = {m} × EBITDA_{n} = {m} × {last['ebitda']}；PV = TV ÷ (1+WACC)^{n}")
    if method in ("gordon", "both"):
        g = _n(term.get("growth"))
        if g is None or g >= wacc:
            return {"error": "永续增长法要给 terminal.growth 且小于 wacc"}
        bridge(last["ufcf"] * (1 + g) / (wacc - g), "perpetual_growth", f"TV = UFCF_{n} × (1+g) ÷ (WACC − g) = {last['ufcf']} × (1+{g}) ÷ ({wacc} − {g})；PV = TV ÷ (1+WACC)^{n}")
    out = {"mode": "dcf", "rows": rows, "pv_fcf_sum": _r(pv_sum, 4), "results": res,
           "equity_bridge": {"debt": debt, "cash": cash, "minority": minority, "preferred": pref, "other_equity_adjustments": other_adj, "shares": shares,
                             "formula": "股权价值 = EV − 债务 + 现金 − 少数股东 − 优先股 + 其他调整；每股 = 股权 ÷ 股本"},
           "formulas": {"ufcf": "EBIT × (1 − t) + D&A − CapEx − ΔNWC + 其他经营项/递延税" + (" − SBC" if sbc_mode == "expense" else ""),
                        "discount": f"PV = UFCF ÷ (1+WACC)^{'(t−0.5)' if mid else 't'}"},
           "conventions": [f"折现：{'年中' if mid else '期末'}；终值在第 {n} 年末折现", "税按 EBIT × 税率（无杠杆）",
                           "「从某年起保持占营收比例不变」默认用最近一个实际财年的比例（题目给指引/指定年份时按题目）",
                           "SBC：默认不扣（sbc_treatment=expense 时作为现金成本扣除）"]}
    if len(res) == 2 and shares:
        hi = max(res, key=lambda k: res[k].get("per_share") or 0)
        out["higher_per_share"] = {"method": hi, "diff": _r(abs(res["exit_multiple"]["per_share"] - res["perpetual_growth"]["per_share"]), 4)}
    return out


# ====================================================================== lbo
def _irr(flows: list[float]) -> float | None:
    lo, hi = -0.99, 10.0
    f = lambda r: sum(c / (1 + r) ** i for i, c in enumerate(flows))
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def lbo(a: dict) -> dict:
    g = lambda k, d=None: _n(a.get(k)) if _n(a.get(k)) is not None else d
    price, prem, shares = g("price"), g("premium", 0), g("shares")
    offer = g("offer_price") or (price * (1 + prem) if price is not None else None)
    if offer is None or shares is None:
        return {"error": "给 price + premium（或 offer_price）和 shares"}
    rev0 = g("revenue")
    ebitda0 = g("ebitda")
    if ebitda0 is None and g("operating_income") is not None and g("da") is not None:
        ebitda0 = g("operating_income") + g("da")
    if rev0 is None or ebitda0 is None:
        return {"error": "给基期 revenue 和 ebitda（或 operating_income + da）"}
    lev = g("leverage")
    new_debt = g("new_debt") if g("new_debt") is not None else (lev * ebitda0 if lev is not None else None)
    if new_debt is None:
        return {"error": "给 leverage（倍 EBITDA）或 new_debt"}
    r_d, r_c = g("interest_rate", 0), g("cash_interest_rate", 0)
    fees = g("transaction_fees", 0) + g("financing_fees", 0)
    old_debt = g("existing_debt", 0)
    cash0, min_cash = g("cash", 0), g("min_cash", 0)
    use_cash = a.get("use_excess_cash")
    use_cash = (min_cash > 0 or bool(a.get("min_cash") is not None)) if use_cash is None else bool(use_cash)
    excess = max(0.0, cash0 - min_cash) if use_cash else 0.0
    eq_purchase = offer * shares
    uses = {"equity_purchase": eq_purchase, "refinance_existing_debt": old_debt, "fees": fees}
    total_uses = sum(uses.values())
    sponsor = total_uses - new_debt - excess
    sources = {"new_debt": new_debt, "excess_cash": excess, "sponsor_equity": sponsor}
    n = int(a.get("years") or 5)
    gr = _series(a.get("growth", 0), n, "growth")
    m0 = ebitda0 / rev0
    if a.get("margins"):
        margins = _series(a["margins"], n, "margins")
    else:
        step = g("margin_step", 0)
        margins = [m0 + step * (i + 1) for i in range(n)]
    da_p, cap_p, nwc_p = _series(a.get("da_pct", 0), n, ""), _series(a.get("capex_pct", 0), n, ""), _series(a.get("nwc_change_pct", 0), n, "")
    tax = g("tax_rate", 0.25)
    avg = a.get("average_balances", True) is not False
    debt, cash = new_debt, cash0 - excess
    rows, R = [], rev0
    for i in range(n):
        R = R * (1 + (gr[i] or 0))
        e = R * margins[i]
        da, capex, dnwc = R * (da_p[i] or 0), R * (cap_p[i] or 0), R * (nwc_p[i] or 0)
        ebit = e - da
        pay, end_cash = 0.0, cash
        for _ in range(100):  # 平均余额计息 → 循环引用，迭代到收敛
            end_debt = debt - pay
            intx = r_d * ((debt + end_debt) / 2 if avg else debt)
            inti = r_c * ((cash + end_cash) / 2 if avg else cash)
            ebt = ebit - intx + inti
            taxes = max(0.0, ebt) * tax if a.get("no_tax_shield_loss") else ebt * tax
            ni = ebt - taxes
            fcf = ni + da - capex - dnwc
            avail = fcf + cash - min_cash
            new_pay = max(0.0, min(debt, avail))
            new_end_cash = cash + fcf - new_pay
            if abs(new_pay - pay) < 1e-9 and abs(new_end_cash - end_cash) < 1e-9:
                break
            pay, end_cash = new_pay, new_end_cash
        rows.append({"year": i + 1, "revenue": _r(R, 4), "ebitda_margin_pct": _r(margins[i] * 100, 4), "ebitda": _r(e, 4), "da": _r(da, 4), "ebit": _r(ebit, 4),
                     "interest_expense": _r(intx, 4), "interest_income": _r(inti, 4), "ebt": _r(ebt, 4), "taxes": _r(taxes, 4), "net_income": _r(ni, 4),
                     "capex": _r(capex, 4), "delta_nwc": _r(dnwc, 4), "fcf": _r(fcf, 4), "debt_paydown": _r(pay, 4),
                     "debt_begin": _r(debt, 4), "debt_end": _r(debt - pay, 4), "cash_begin": _r(cash, 4), "cash_end": _r(end_cash, 4)})
        debt, cash = debt - pay, end_cash
    exit_mult = g("exit_multiple")
    if exit_mult is None:
        return {"error": "给 exit_multiple"}
    exit_ev = exit_mult * rows[-1]["ebitda"]
    exit_eq = exit_ev - debt + cash
    moic = exit_eq / sponsor if sponsor else None
    irr = _irr([-sponsor] + [0.0] * (n - 1) + [exit_eq]) if sponsor and sponsor > 0 else None
    return {"mode": "lbo", "offer_price": _r(offer, 4), "entry_ebitda": _r(ebitda0, 4), "entry_ebitda_margin_pct": _r(m0 * 100, 4),
            "entry_ev_multiple": _r((eq_purchase + old_debt - cash0) / ebitda0, 4),
            "uses": {k: _r(v, 4) for k, v in uses.items()}, "total_uses": _r(total_uses, 4),
            "sources": {k: _r(v, 4) for k, v in sources.items()}, "total_sources": _r(sum(sources.values()), 4), "rows": rows,
            "exit": {"exit_ebitda": rows[-1]["ebitda"], "exit_multiple": exit_mult, "exit_ev": _r(exit_ev, 4), "debt_at_exit": _r(debt, 4), "cash_at_exit": _r(cash, 4),
                     "net_debt_at_exit": _r(debt - cash, 4), "exit_equity": _r(exit_eq, 4)},
            "returns": {"moic": _r(moic, 4), "irr_pct": _r(irr * 100, 4) if irr is not None else None},
            "formulas": {"offer": "offer = price × (1 + premium)", "uses": "股权收购价 = offer × shares；总用途 = 股权 + 再融资旧债 + 交易/融资费用",
                         "sources": "新债 = leverage × 基期 EBITDA；账上超出最低现金的部分作为资金来源（use_excess_cash）；发起人股权 = 总用途 − 新债 − 超额现金",
                         "interest": "利息 = 利率 × (期初 + 期末余额)/2（average_balances，迭代求解）；现金利息收入同理",
                         "fcf": "FCF = 净利润 + D&A − CapEx − ΔNWC，全部偿还新债（不低于最低现金），还清后累积现金",
                         "exit": "退出 EV = 退出倍数 × 末年 EBITDA；股权 = EV − 剩余债务 + 现金；MOIC = 股权/投入；IRR 由 [−投入, 0…, 股权] 求解"},
            "conventions": ["旧债在交割时按账面余额再融资（existing_debt）", "题目给了最低现金余额 → 默认超出部分用于支付对价（use_excess_cash=false 可关）",
                            "EBITDA 默认 = 经营利润 + D&A（GAAP）", "税 = 税前利润 × 税率（亏损年份按负税处理，可设 no_tax_shield_loss）"]}


# ====================================================================== coverage
def coverage(a: dict) -> dict:
    g = lambda k: _n(a.get(k))
    ebitda = g("ebitda")
    parts = {}
    if ebitda is None:
        if g("operating_income") is not None and g("da") is not None:
            ebitda = g("operating_income") + g("da")
            parts["ebitda"] = "operating_income + da"
        elif g("ebit") is not None and g("da") is not None:
            ebitda = g("ebit") + g("da")
            parts["ebitda"] = "ebit + da"
    rent, intr, princ = g("rent") or 0, g("interest"), g("principal") or 0
    if ebitda is None or intr is None:
        return {"error": "给 ebitda（或 operating_income + da）和 interest；FCCR 另给 rent（固定租赁费用）"}
    ebitdar = ebitda + rent
    fixed = intr + rent + princ
    out = {"mode": "coverage", "ebitda": _r(ebitda, 4), "ebitdar": _r(ebitdar, 4), "fixed_charges": _r(fixed, 4), "fccr": _r(ebitdar / fixed, 4) if fixed else None,
           "interest_coverage_ebitda": _r(ebitda / intr, 4) if intr else None,
           "formulas": {"ebitdar": "EBITDA + rent", "fixed_charges": "interest + rent" + (" + principal" if princ else ""), "fccr": "EBITDAR ÷ fixed charges"},
           "conventions": ["EBITDA 默认 = 经营利润（Operating income）+ D&A", "interest 用利润表上报告的利息费用科目：公司只报「净利息费用」时就用净额，并在回答里写明口径",
                           "rent = 固定经营租赁成本（不含短期与可变租赁），题目另给数时用题目数"], **({"derived": parts} if parts else {})}
    if g("ebit") is not None:
        out["interest_coverage_ebit"] = _r(g("ebit") / intr, 4) if intr else None
    return out


def compare(a: dict) -> dict:
    """多家 coverage 一起算并给差值（FCCR 比较题）。"""
    items = a.get("companies") or []
    res = []
    for c in items:
        r = coverage(c)
        r["name"] = c.get("name") or c.get("symbol")
        res.append(r)
    ok = [r for r in res if r.get("fccr") is not None]
    out = {"mode": "coverage", "companies": res}
    if len(ok) >= 2:
        s = sorted(ok, key=lambda r: -r["fccr"])
        out["stronger"] = s[0]["name"]
        out["fccr_diff"] = _r(s[0]["fccr"] - s[1]["fccr"], 4)
    return out


# ====================================================================== 工具
TOOL_NAMES = {"fin_model"}


def run_tool(name: str, a: dict, agent=None):
    mode = str(a.get("mode") or "").lower()
    try:
        if mode == "comps":
            return comps(a.get("companies") or [], a.get("metrics"), bool(a.get("include_leases")), a.get("target"), a.get("exclude_negative", True) is not False)
        if mode == "dcf":
            return dcf(a)
        if mode == "lbo":
            return lbo(a)
        if mode == "coverage":
            return compare(a) if a.get("companies") else coverage(a)
    except (TypeError, ValueError, ZeroDivisionError) as e:
        return {"error": f"fin_model 输入有误：{e}"}
    return {"error": "mode 只能是 comps / dcf / lbo / coverage"}


TOOL_SPECS = [
    {"name": "fin_model", "description": "可比公司倍数与财务建模（确定性计算，返回逐年表、公式与输入，可复算）。"
     "mode=comps：companies=[{name, price, shares 或 market_cap, debt, cash, equity, revenue, ebitda 或 operating_income+da, ebit, net_income, operating_leases, preferred, minority, price_date, balance_date}]，"
     "输出每家 P/E、P/B、EV/EBITDA、EV/Sales、EV/EBIT、D/E、D/总资本、EBITDA 利润率 + 同行均值/中位数/区间 + 排名；include_leases 把经营租赁计入 EV；target 用同行中位数倍数推隐含价值。"
     "mode=dcf：base_revenue + growth（数或逐年列表）或 revenues，ebitda_margin 或 ebit_margin，da_pct、capex_pct、nwc_change_pct（ΔNWC 占营收）、tax_rate、other_cf、deferred_tax、sbc_pct+sbc_treatment，"
     "wacc，terminal={method: exit|gordon|both, multiple, growth}，mid_year，debt、cash、minority、preferred、shares → EV、股权、每股。"
     "mode=lbo：price、premium、shares、revenue、ebitda（或 operating_income+da）、leverage、interest_rate、transaction_fees、existing_debt、cash、min_cash、cash_interest_rate、growth、margin_step、"
     "da_pct、capex_pct、nwc_change_pct、tax_rate、years、exit_multiple → 资金来源与用途、逐年扫债、退出 EV/股权、MOIC、IRR。"
     "mode=coverage：ebitda（或 operating_income+da）、rent、interest、principal → EBITDAR、FCCR；companies=[…] 多家比较给差值。比例一律用小数（8% 写 0.08）。",
     "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["comps", "dcf", "lbo", "coverage"]},
                                                     "companies": {"type": "array", "items": {"type": "object"}}, "target": {"type": "object"},
                                                     "metrics": {"type": "array", "items": {"type": "string"}}, "include_leases": {"type": "boolean"},
                                                     "terminal": {"type": "object"}}, "required": ["mode"]}},
]
