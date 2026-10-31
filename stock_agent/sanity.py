"""数据合理性检查（v0.35）：工具返回的数字明显不合常理或已经过期时，在结果的 warnings 里写明，让模型转告用户、别当真。

只加提示，不改数字、不拦截结果（可能是真的极端行情）：
  - 行情日期离今天超过 STALE_DAYS 个自然日 → 可能停牌 / 退市 / 数据源没更新；
  - A股单日涨跌超过 30%（北交所上限）→ 超出涨跌停幅度（新股上市首日除外），数据可疑；
  - 分析师目标价均值 / 中位数离现价超过 3 倍或不到 1/3，或给出的空间超过 200% → 口径（单位、币种、拆股）错误或数据被篡改；
  - 财务比率超出常见范围（营收/利润增速绝对值 > 500%、ROE 绝对值 > 150%、股息率 > 30%、市盈率 > 3000）→ 可疑。
「今天」默认取北京时间；环境变量 STOA_TODAY=YYYY-MM-DD 可固定（评测用，保证固定数据不随真实日期变成「过期」）。
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
STALE_DAYS = 10  # 长假（国庆 / 春节）最多停 9 天左右


def today() -> date:
    v = (os.getenv("STOA_TODAY") or "").strip()
    if v:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            pass
    return datetime.now(CN_TZ).date()


def _day(v) -> date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _a_share(sym: str) -> bool:
    return str(sym).upper().endswith((".SS", ".SZ", ".BJ"))


def quote_warnings(sym: str, q: dict, now: date | None = None) -> list[str]:
    now = now or today()
    out = []
    d = _day(q.get("date"))
    if d and (now - d).days > STALE_DAYS:
        out.append(f"行情日期是 {d.isoformat()}，距今天 {(now - d).days} 天，可能已停牌、退市或数据源没更新——不要当作最新价，先核实。")
    chg = _num(q.get("chg_pct"))
    if chg is not None and _a_share(sym) and abs(chg) > 30.5:
        out.append(f"单日涨跌 {chg:+g}% 超出 A股涨跌停幅度（新股上市初期除外），数据可疑，以交易所行情为准。")
    return out


def consensus_warnings(r: dict) -> list[str]:
    out = []
    px = _num(r.get("price_now"))
    t = r.get("target") or {}
    if px and px > 0:
        for k, label in (("mean", "均值"), ("median", "中位数")):
            v = _num(t.get(k))
            if v and v > 0 and (v / px > 3 or v / px < 1 / 3):
                out.append(f"目标价{label} {v:g} 是现价 {px:g} 的 {v / px:.1f} 倍，偏离异常：可能是单位、币种、拆股口径错误或数据有误，核实前不要引用。")
                break
    up = _num(r.get("upside_pct"))
    if up is not None and abs(up) > 200 and not out:
        out.append(f"相对现价的空间 {up:g}% 异常大，可能是口径或数据错误，核实前不要引用。")
    return out


FIN_LIMITS = (
    (("revenue_growth", "营收同比%", "营业总收入同比%", "营业收入同比%", "earnings_growth", "净利润同比%"), 500, "增速"),
    (("roe", "ROE%", "ROE"), 150, "ROE"),
    (("dividend_yield", "股息率%", "股息率"), 30, "股息率"),
    (("pe", "市盈率TTM", "市盈率"), 3000, "市盈率"),
)


def financial_warnings(r: dict) -> list[str]:
    rows = [r] + [x for x in (r.get("valuation"),) if isinstance(x, dict)]
    out = []
    for keys, lim, label in FIN_LIMITS:
        for row in rows:
            hit = next((k for k in keys if _num(row.get(k)) is not None), None)
            if hit is None:
                continue
            v = _num(row[hit])
            if abs(v) > lim:
                out.append(f"{label} {v:g}（{hit}）超出常见范围，可能是单位或数据错误，核实前不要据此下结论。")
            break
    return out


def check(name: str, result, now: date | None = None):
    """返回 (加了 warnings 的结果副本, 新增的 warnings)；没问题返回 (原结果, [])。"""
    try:
        if name == "get_quotes" and isinstance(result, dict):
            out, added = None, []
            for sym, q in result.items():
                if not isinstance(q, dict):
                    continue
                ws = quote_warnings(sym, q, now)
                if ws:
                    out = out if out is not None else dict(result)
                    out[sym] = {**q, "warnings": list(q.get("warnings") or []) + ws}
                    added += [f"{sym}：{w}" for w in ws]
            return (out, added) if out is not None else (result, [])
        if isinstance(result, dict) and not result.get("error"):
            ws = consensus_warnings(result) if name == "get_consensus" else financial_warnings(result) if name == "get_financials" else []
            if ws:
                return {**result, "warnings": list(result.get("warnings") or []) + ws}, ws
    except Exception:
        pass
    return result, []
