"""同花顺金融数据服务（hithink-finance）—— 官方 A 股数据源。

契约来源：https://fuyao.aicubes.cn/docs/ 与仓库 Hithink-Tech-Org/Financial-API（MIT）
- REST，鉴权头 `X-api-key`；HTTP 状态恒为 200，业务结果看信封 `code`（0 = 成功）
- 限流：HTTP 429 或 `code=4001`
- 金额单位＝原币元；`null` 表示未披露，**不补零**
- 只覆盖 A 股。**不提供**港股/美股行情、分钟 K、tick、L2 —— 这些继续走 Yahoo / 东财

本模块先只做三期里的第一期三件事：A 股快照（给实时报价交叉校验）、日线历史、交易日历。
没有 Key（`HITHINK_API_KEY` / `HITHINK_FINANCE_API_KEY`）时 `ready()` 为 False，
所有调用方都退回原有数据源，行为与未接入时完全一致。
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://fuyao.aicubes.cn"
KEY_ENVS = ("HITHINK_API_KEY", "HITHINK_FINANCE_API_KEY")
CN = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = float(os.getenv("HITHINK_TIMEOUT") or 8)

# 交易所后缀：Yahoo 的 .SS 对应同花顺的 .SH
EX = {"SS": "SH", "SH": "SH", "SZ": "SZ", "BJ": "BJ"}

# A 股连续竞价时段（北京时间）；集合竞价与午休不算
SESSIONS = [((9, 30), (11, 30)), ((13, 0), (15, 0))]


class HithinkError(RuntimeError):
    """接口不可用/被限流/业务报错。调用方一律当作「这个源这次没数据」处理。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 配置
def key() -> str:
    for k in KEY_ENVS:
        v = (os.getenv(k) or "").strip()
        if v:
            return v
    return ""


def ready() -> bool:
    return bool(key())


# ---------------------------------------------------------------- 传输
def _http(path: str, params: dict | None = None) -> dict:
    """GET 一个接口并拆信封。测试通过 monkeypatch 本函数注入固定响应。"""
    r = requests.get(f"{BASE}{path}", params=params or {},
                     headers={**UA, "X-api-key": key()}, timeout=TIMEOUT)
    if r.status_code == 429:
        raise HithinkError("同花顺接口限流（429）", 4001)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError as e:
        raise HithinkError(f"同花顺返回不是 JSON：{str(e)[:80]}") from e
    code = j.get("code")
    if code not in (0, None):
        raise HithinkError(f"同花顺返回 code={code}：{str(j.get('message') or '')[:120]}", code)
    data = j.get("data")
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 代码
def thscode(symbol: str) -> str | None:
    """Yahoo 风格代码 → 同花顺 thscode；不是 A 股就返回 None（港股/美股/指数都走原来源）。"""
    s = (symbol or "").upper().strip()
    if re.fullmatch(r"\d{6}", s):
        s += ".SH" if s.startswith(("6", "9")) else ".BJ" if s.startswith(("4", "8")) else ".SZ"
    m = re.fullmatch(r"(\d{6})\.(SS|SH|SZ|BJ)", s)
    return f"{m.group(1)}.{EX[m.group(2)]}" if m else None


def quote_url(p: dict) -> str:
    return f"https://stockpage.10jqka.com.cn/{p.get('code', '')}/"


# ---------------------------------------------------------------- 时间
def _cn_date(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, CN).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _cn_time(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, CN).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return None


def _ms(day: str, end_of_day: bool = False) -> int:
    d = datetime.fromisoformat(str(day)[:10]).replace(tzinfo=CN)
    if end_of_day:
        d = d + timedelta(days=1) - timedelta(milliseconds=1)
    return int(d.timestamp() * 1000)


def _num(x):
    return None if x is None else x


# ---------------------------------------------------------------- 行情快照
def snapshot(symbols) -> dict[str, dict]:
    """A 股快照。返回 {thscode: {price, prev_close, date, volume, turnover, ...}}。"""
    one = isinstance(symbols, str)
    codes = [c for c in (thscode(s) for s in ([symbols] if one else list(symbols or []))) if c]
    if not codes:
        return {}
    data = _http("/api/a-share/prices/snapshot", {"thscodes": ",".join(codes)})
    stamp = data.get("timestamp")
    out: dict[str, dict] = {}
    for it in data.get("item") or []:
        code = it.get("thscode")
        if not code:
            continue
        out[code] = {
            "thscode": code, "ticker": it.get("ticker"),
            "price": it.get("last_price"), "prev_close": it.get("prev_price"),
            "change_pct": it.get("price_change_ratio_pct"),
            "open": it.get("open_price"), "high": it.get("high_price"), "low": it.get("low_price"),
            "volume": it.get("volume"), "turnover": it.get("turnover"),
            "date": _cn_date(stamp), "time": _cn_time(stamp), "as_of_ms": stamp, "currency": "CNY",
        }
    return out


def quote(p: dict) -> dict | None:
    """marketdata.parse() 的代码字典 → 实时报价（只支持 A 股；其它市场返回 None 让调用方跳过）。"""
    if p.get("market") != "A" or not ready():
        return None
    want = thscode(f"{p.get('code')}.{str(p.get('ex') or '').upper()}")
    if not want:
        return None
    got = snapshot(want).get(want)
    if not got or got.get("price") in (None, 0):
        return None
    return {"price": got["price"], "prev_close": got.get("prev_close"),
            "date": got.get("date"), "time": got.get("time"), "currency": "CNY"}


# ---------------------------------------------------------------- 日线
def historical(symbol: str, start: str, end: str, adjust: str = "forward") -> list[dict]:
    """日线 OHLC（区间上限 10 年）。`null` 原样保留，不当 0。"""
    code = thscode(symbol)
    if not code:
        return []
    data = _http("/api/a-share/prices/historical",
                 {"thscode": code, "interval": "1d", "start": _ms(start),
                  "end": _ms(end, end_of_day=True), "adjust": adjust})
    rows = []
    for it in data.get("item") or []:
        rows.append({"date": _cn_date(it.get("date_ms")),
                     "open": _num(it.get("open_price")), "high": _num(it.get("high_price")),
                     "low": _num(it.get("low_price")), "close": _num(it.get("close_price")),
                     "volume": _num(it.get("volume")), "turnover": _num(it.get("turnover"))})
    return sorted((r for r in rows if r["date"]), key=lambda r: r["date"])


# ---------------------------------------------------------------- 交易日历
_CAL: dict = {"at": 0.0, "days": None}
_CAL_LOCK = threading.Lock()
CAL_TTL = 21600  # 6 小时


def trading_days(ttl: int = CAL_TTL) -> set[str] | None:
    """A 股近一年交易日（'YYYY-MM-DD'）。取不到时返回上一次缓存（没有就是 None）。"""
    with _CAL_LOCK:
        if _CAL["days"] and time.time() - _CAL["at"] < ttl:
            return _CAL["days"]
    try:
        data = _http("/api/a-share/calendar/trading-days")
    except Exception:
        return _CAL["days"]
    days = set()
    for it in data.get("item") or []:
        raw = str(it.get("date") or "")
        if re.fullmatch(r"\d{8}", raw):
            days.add(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
    if not days:
        return _CAL["days"]
    with _CAL_LOCK:
        _CAL.update(at=time.time(), days=days)
    return days


def session_state(symbol: str, at: datetime | None = None) -> dict | None:
    """A 股是否交易日 / 是否在连续竞价时段。

    返回 None 表示「这个源这次给不了答案」（非 A 股、没有 Key、日历取不到），
    调用方应当退回原来的按工作日+钟点判断。
    """
    if thscode(symbol) is None:
        return None
    days = trading_days()
    if not days:
        return None
    t = (at or datetime.now(CN)).astimezone(CN)
    day = t.strftime("%Y-%m-%d")
    trading = day in days
    in_session = trading and any(a <= (t.hour, t.minute) < b for a, b in SESSIONS)
    if in_session:
        reason = None
    elif not trading:
        reason = "weekend" if t.weekday() >= 5 else "holiday"
    else:
        reason = "outside_hours"
    return {"trading_day": trading, "in_session": in_session, "reason": reason,
            "calendar_verified": True, "date": day}


def describe() -> dict:
    """接入状态（给设置页/自检用，不暴露 Key）。"""
    return {"configured": ready(), "base": BASE, "source": "同花顺金融数据服务（官方）",
            "calendar_days": len(_CAL["days"] or ()), "covers": "A股",
            "not_supported": ["港股/美股行情", "分钟K", "tick", "L2"]}
