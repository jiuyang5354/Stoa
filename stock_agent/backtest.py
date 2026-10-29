"""Probatio · 时间穿越回测：在历史上每个时点只给 Agent 当时能看到的数据，让它给出 看多/看空/中性 + 置信度，再用之后的真实收盘价打分。

  python -m stock_agent backtest                         # 默认 12 只 A/港/美股，近一年，每 5 个交易日，持有 5/20 日，规则 Agent
  python -m stock_agent backtest --agent llm --max-calls 200
  python -m stock_agent backtest --symbols AAPL,0700.HK --start 2025-06-01 --end 2026-06-01 --step 10 --horizons 5,20
  python -m stock_agent backtest --synthetic             # 不联网，用合成行情跑通管线

未来数据屏蔽（最要紧）：
- 行情：PointInTime.asof(symbol, date) 只返回 date 当天及以前的收盘价副本；Agent 拿到的 Snapshot 里只有这份截断序列。
  打分用的 forward() 只在打分阶段调用，Agent 看不到。
- 新闻、公告、财报、联网搜索、读网页、记忆、对话检索、判断账本：回测模式全部禁用（宁可少信息，也不泄漏）。
- 大模型自己的训练知识也会泄漏未来，默认 blind：隐去代码和日期，价格归一化到 100，只留市场名。
结果写到 数据目录/data/backtests/<id>.json（+ <id>.png 图，装了 matplotlib 才有）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import indicators

CN_TZ = timezone(timedelta(hours=8))
PROMPT_VERSION = "bt-1"
MIN_HISTORY = 60  # 至少有这么多天历史才出判断
VIEWS = ("看多", "看空", "中性")
SIGN = {"看多": 1, "看空": -1, "中性": 0}
MARKETS = ("A股", "港股", "美股")
DEFAULT_SYMBOLS = ["600519.SS", "000858.SZ", "601318.SS", "300750.SZ",
                   "0700.HK", "9988.HK", "0005.HK", "1299.HK",
                   "AAPL", "MSFT", "NVDA", "JPM"]
CONF_BUCKETS = [(50, 60), (60, 70), (70, 80), (80, 101)]


def market(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith((".SS", ".SZ", ".BJ")):
        return "A股"
    if s.endswith(".HK") or s in ("^HSI", "^HSCE", "^HSTECH"):
        return "港股"
    return "美股"


def bt_dir() -> Path:
    from .memory import home
    d = home() / "data" / "backtests"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ====================================================================== 时点数据
class PointInTime:
    """持有完整行情，但对外只按日期截断给出。Agent 永远只拿 asof() 的结果。"""

    def __init__(self, closes: dict[str, pd.Series]):
        self._full = {}
        for s, ser in closes.items():
            ser = pd.Series(ser).dropna().astype(float).sort_index()
            ser.index = pd.to_datetime(ser.index)
            self._full[s] = ser[~ser.index.duplicated(keep="last")]

    @property
    def symbols(self) -> list[str]:
        return list(self._full)

    def dates(self, symbol: str) -> pd.DatetimeIndex:
        return self._full[symbol].index

    def asof(self, symbol: str, date) -> pd.Series:
        """date 当天及以前的收盘价（副本）。截断后再检查一次，绝不带出未来的行。"""
        date = pd.Timestamp(date)
        ser = self._full[symbol]
        out = ser.loc[ser.index <= date].copy()
        if len(out) and out.index.max() > date:  # 防御：理论上不会发生
            raise AssertionError("future leak")
        return out

    def forward(self, symbol: str, date, n: int) -> tuple[pd.Timestamp, float] | None:
        """打分专用：date 之后第 n 个交易日的 (日期, 收盘)。不交给 Agent。"""
        ser = self._full[symbol]
        idx = ser.index.searchsorted(pd.Timestamp(date), side="right") - 1
        if idx < 0 or ser.index[idx] != pd.Timestamp(date) or idx + n >= len(ser):
            return None
        return ser.index[idx + n], float(ser.iloc[idx + n])


@dataclass(frozen=True)
class Snapshot:
    """Agent 在某个历史时点能看到的全部东西。"""
    symbol: str
    market: str
    date: pd.Timestamp
    closes: pd.Series  # 已截断到 date
    horizon: int

    def __post_init__(self):
        if len(self.closes) and self.closes.index.max() > self.date:
            raise AssertionError("Snapshot 含有未来数据")


# ====================================================================== 行情获取
def load_prices(symbols: list[str], start: str, end: str, synthetic: bool = False, seed: int = 7) -> tuple[dict, str]:
    if synthetic:
        return synthetic_prices(symbols, start, end, seed), "synthetic"
    from . import data
    need = (pd.Timestamp.now() - pd.Timestamp(start)).days * 252 / 365 + MIN_HISTORY + 40
    period = next((p for p, d in sorted(data.PERIOD_DAYS.items(), key=lambda x: x[1]) if d >= need), "5y")
    closes = data.fetch_closes(symbols, period=period)
    closes = {s: v for s, v in closes.items() if v is not None and len(v) > MIN_HISTORY + 5}
    return closes, "live:" + ",".join(data.provider_order())


def synthetic_prices(symbols: list[str], start: str, end: str, seed: int = 7) -> dict[str, pd.Series]:
    """几何布朗运动 + 弱动量的合成行情，只用于离线跑通管线和测试。"""
    rng = random.Random(seed)
    idx = pd.bdate_range(pd.Timestamp(start) - pd.Timedelta(days=200), pd.Timestamp(end) + pd.Timedelta(days=60))
    out = {}
    for s in symbols:
        p, drift, vol, mom, prev = 100.0, rng.uniform(-.0004, .0008), rng.uniform(.012, .025), rng.uniform(0, .15), 0.0
        vals = []
        for _ in idx:
            r = drift + mom * prev + rng.gauss(0, vol)
            p *= math.exp(r)
            prev = r
            vals.append(round(p, 3))
        out[s] = pd.Series(vals, index=idx, name=s)
    return out


# ====================================================================== Agent
def rule_agent(snap: Snapshot) -> dict:
    """规则 Agent（无需 LLM）：均线多头排列看多、空头排列看空，否则中性；离均线越远越有信心。"""
    c = snap.closes
    last, ma20, ma60 = float(c.iloc[-1]), float(c.tail(20).mean()), float(c.tail(60).mean())
    gap = (last / ma20 - 1) * 100
    if last > ma20 > ma60:
        return {"view": "看多", "confidence": int(min(85, 55 + abs(gap) * 4)), "reason": f"价在20日线上 {gap:+.1f}%，20日线在60日线上"}
    if last < ma20 < ma60:
        return {"view": "看空", "confidence": int(min(85, 55 + abs(gap) * 4)), "reason": f"价在20日线下 {gap:+.1f}%，20日线在60日线下"}
    return {"view": "中性", "confidence": 50, "reason": "均线纠缠"}


def momentum_baseline(snap: Snapshot) -> dict:
    c = snap.closes
    r = float(c.iloc[-1] / c.iloc[-21] - 1) if len(c) > 21 else 0.0
    return {"view": "看多" if r > 0 else "看空", "confidence": 60, "reason": f"20日 {r * 100:+.1f}%"}


def long_baseline(snap: Snapshot) -> dict:
    return {"view": "看多", "confidence": 60, "reason": "一直看多"}


def random_baseline(seed: int):
    def f(snap: Snapshot) -> dict:
        h = int(hashlib.md5(f"{seed}|{snap.symbol}|{snap.date:%Y-%m-%d}".encode()).hexdigest(), 16)
        return {"view": "看多" if h % 2 else "看空", "confidence": 50 + h % 30, "reason": "随机"}
    return f


BT_SYSTEM = """你是「Stoa」的回测模式。现在是历史上的某一天，你只能看到下面给出的、截至这一天的价格数据，没有新闻、公告和其它资料。
任务：判断这只标的在之后 {h} 个交易日的方向。
- 看多：预计上涨超过 {band}% ；看空：预计下跌超过 {band}% ；中性：涨跌在 ±{band}% 以内。
- confidence 是你对这个判断的把握，50 = 抛硬币，100 = 完全确定；要诚实，别习惯性给高分。
- 不要猜这是哪只股票、哪一年；只看数据。
只输出一行 JSON：{{"view": "看多|看空|中性", "confidence": 50-100 的整数, "reason": "不超过 40 字"}}"""


def snapshot_payload(snap: Snapshot, blind: bool = True) -> dict:
    c = snap.closes.tail(250)
    if blind:
        c = c / float(c.iloc[-1]) * 100  # 最新价归一化为 100
    a = indicators.analyze(c, {})
    a.pop("date", None)
    tail = [round(float(v), 2) for v in c.tail(60)]
    return {"标的": f"某{snap.market}股票" if blind else snap.symbol,
            "截至": "T 日（日期已隐去）" if blind else f"{snap.date:%Y-%m-%d}",
            "指标": a, "最近60个交易日收盘": tail, "持有期（交易日）": snap.horizon}


class LLMCache:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS c(k TEXT PRIMARY KEY, v TEXT, ts TEXT)")
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            r = self.db.execute("SELECT v FROM c WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def put(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO c VALUES(?,?,?)", (k, json.dumps(v, ensure_ascii=False), datetime.now(CN_TZ).isoformat()))
            self.db.commit()


def parse_view(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("没有 JSON")
    j = json.loads(m.group(0))
    v = str(j.get("view", "")).strip()
    v = {"看涨": "看多", "看跌": "看空", "震荡": "中性", "bull": "看多", "bear": "看空", "neutral": "中性"}.get(v, v)
    if v not in VIEWS:
        raise ValueError(f"view 不合法：{v}")
    conf = int(max(50, min(100, float(j.get("confidence", 50)))))
    return {"view": v, "confidence": conf, "reason": str(j.get("reason", ""))[:80]}


class LLMAgent:
    """单轮、无工具的 LLM 判断：只喂截断后的行情，研究类工具与记忆全部不给。带缓存、并发上限与调用统计。"""

    def __init__(self, llm=None, blind: bool = True, max_calls: int = 300, band: float = 1.5,
                 price_in: float = 0.0, price_out: float = 0.0, cache: LLMCache | None = None):
        if llm is None:
            from .agent import load_cfg
            from .providers import Router
            llm = Router(load_cfg().get("llm", {}))
        self.llm, self.blind, self.max_calls, self.band = llm, blind, max_calls, band
        self.model = repr(llm)
        self.cache = cache or LLMCache(bt_dir() / "llm_cache.db")
        self.price_in, self.price_out = price_in, price_out
        self.stats = {"calls": 0, "cache_hits": 0, "errors": 0, "skipped_cap": 0, "tokens_in_est": 0, "tokens_out_est": 0, "seconds": 0.0}
        self.lock = threading.Lock()

    def key(self, snap: Snapshot) -> str:
        raw = f"{PROMPT_VERSION}|{self.model}|{snap.symbol}|{snap.date:%Y-%m-%d}|{snap.horizon}|{int(self.blind)}|{self.band}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def __call__(self, snap: Snapshot) -> dict | None:
        k = self.key(snap)
        hit = self.cache.get(k)
        if hit:
            with self.lock:
                self.stats["cache_hits"] += 1
            return hit
        with self.lock:
            if self.stats["calls"] >= self.max_calls:
                self.stats["skipped_cap"] += 1
                return None
            self.stats["calls"] += 1
        band = round(self.band * math.sqrt(snap.horizon / 5), 2)
        msgs = [{"role": "system", "content": BT_SYSTEM.format(h=snap.horizon, band=band)},
                {"role": "user", "content": json.dumps(snapshot_payload(snap, self.blind), ensure_ascii=False)}]
        t0 = time.time()
        try:
            msg = self.llm.chat(msgs, None, temperature=0.0, timeout=90)
            out = parse_view(msg.get("content") or "")
        except Exception as e:
            with self.lock:
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)[:120]
            return None
        with self.lock:
            self.stats["seconds"] += time.time() - t0
            self.stats["tokens_in_est"] += sum(len(m["content"]) for m in msgs) // 2
            self.stats["tokens_out_est"] += len(msg.get("content") or "") // 2
        self.cache.put(k, out)
        return out

    def summary(self) -> dict:
        s = dict(self.stats)
        s["seconds"] = round(s["seconds"], 1)
        s["cost_est_usd"] = round(s["tokens_in_est"] / 1e6 * self.price_in + s["tokens_out_est"] / 1e6 * self.price_out, 4)
        s["model"] = self.model
        return s


# ====================================================================== 回测主循环
def decision_points(pit: PointInTime, symbol: str, start, end, step: int) -> list[pd.Timestamp]:
    ds = pit.dates(symbol)
    ok = [i for i, d in enumerate(ds) if pd.Timestamp(start) <= d <= pd.Timestamp(end) and i >= MIN_HISTORY]
    return [ds[i] for i in ok[::max(1, step)]]


def predict(pit: PointInTime, agent, start, end, step: int, horizon: int, concurrency: int = 1) -> list[dict]:
    jobs = []
    for s in pit.symbols:
        for d in decision_points(pit, s, start, end, step):
            jobs.append(Snapshot(s, market(s), d, pit.asof(s, d), horizon))

    def one(snap):
        try:
            r = agent(snap)
        except Exception as e:
            r = None
        if not r:
            return None
        return {"symbol": snap.symbol, "market": snap.market, "date": f"{snap.date:%Y-%m-%d}",
                "close": round(float(snap.closes.iloc[-1]), 4), **r}

    if concurrency > 1:
        with ThreadPoolExecutor(concurrency) as ex:
            rows = list(ex.map(one, jobs))
    else:
        rows = [one(j) for j in jobs]
    return [r for r in rows if r]


def band_for(h: int, band5: float) -> float:
    return band5 * math.sqrt(h / 5)


def hit(view: str, ret_pct: float, band: float) -> bool:
    if view == "看多":
        return ret_pct > 0
    if view == "看空":
        return ret_pct < 0
    return abs(ret_pct) <= band


def attach_returns(preds: list[dict], pit: PointInTime, horizons: list[int], step: int) -> list[dict]:
    """给每条判断补上之后 h 日收益（打分用）和到下一个决策点的收益（组合用）。不改原列表。"""
    out = []
    for p in preds:
        q = dict(p)
        for h in horizons:
            f = pit.forward(p["symbol"], p["date"], h)
            q[f"ret{h}"] = round((f[1] / p["close"] - 1) * 100, 4) if f else None
        f = pit.forward(p["symbol"], p["date"], step)
        q["ret_step"] = round((f[1] / p["close"] - 1) * 100, 4) if f else None
        out.append(q)
    return out


def _curve(rows: list[dict], weight) -> list[tuple[str, float]]:
    """按决策日等权：每期收益 = mean(weight(view) * 到下一决策点收益)。返回累计收益曲线（%）。"""
    by = {}
    for r in rows:
        if r.get("ret_step") is not None:
            by.setdefault(r["date"], []).append(weight(r) * r["ret_step"] / 100)
    eq, pts = 1.0, []
    for d in sorted(by):
        eq *= 1 + sum(by[d]) / len(by[d])
        pts.append((d, round((eq - 1) * 100, 3)))
    return pts


def _mdd(curve):
    peak, mdd = 1.0, 0.0
    for _, v in curve:
        e = 1 + v / 100
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    return round(mdd * 100, 2)


def metrics(rows: list[dict], horizons: list[int], band5: float = 1.5) -> dict:
    """方向胜率、含中性的总命中、置信度分组胜率（校准）、多空组合与买入持有。"""
    out = {"n": len(rows), "views": {v: sum(r["view"] == v for r in rows) for v in VIEWS}, "horizons": {}}
    for h in horizons:
        rs = [r for r in rows if r.get(f"ret{h}") is not None]
        band = band_for(h, band5)
        d = [r for r in rs if r["view"] != "中性"]
        hits_d = [hit(r["view"], r[f"ret{h}"], band) for r in d]
        hits_all = [hit(r["view"], r[f"ret{h}"], band) for r in rs]
        cal = []
        for lo, hi in CONF_BUCKETS:
            b = [hit(r["view"], r[f"ret{h}"], band) for r in d if lo <= r["confidence"] < hi]
            cal.append({"bucket": f"{lo}-{min(hi, 100)}", "n": len(b), "hit%": round(sum(b) / len(b) * 100, 1) if b else None})
        out["horizons"][str(h)] = {
            "n": len(rs), "n_dir": len(d), "band%": round(band, 2),
            "hit_dir%": round(sum(hits_d) / len(hits_d) * 100, 1) if hits_d else None,
            "hit_all%": round(sum(hits_all) / len(hits_all) * 100, 1) if hits_all else None,
            "avg_signed_ret%": round(sum(SIGN[r["view"]] * r[f"ret{h}"] for r in d) / len(d), 3) if d else None,
            "calibration": cal}
    ls = _curve(rows, lambda r: SIGN[r["view"]])
    bh = _curve(rows, lambda r: 1)
    out["portfolio"] = {"long_short%": ls[-1][1] if ls else None, "buy_hold%": bh[-1][1] if bh else None,
                        "mdd%": _mdd(ls) if ls else None, "periods": len(ls)}
    out["curve"] = ls
    out["curve_bh"] = bh
    return out


def by_market(rows, horizons, band5):
    res = {"全部": metrics(rows, horizons, band5)}
    for m in MARKETS:
        sub = [r for r in rows if r["market"] == m]
        if sub:
            res[m] = metrics(sub, horizons, band5)
    return res


def run(symbols=None, start=None, end=None, step=5, horizons=(5, 20), agent="baseline", synthetic=False,
        concurrency=4, max_calls=300, blind=True, band5=1.5, seed=7, llm=None, price_in=0.0, price_out=0.0,
        run_id=None, save=True, closes=None, progress=None) -> dict:
    horizons = sorted({int(h) for h in horizons})
    today = pd.Timestamp.now().normalize()
    end = pd.Timestamp(end) if end else today - pd.Timedelta(days=int(max(horizons) * 1.6) + 3)
    start = pd.Timestamp(start) if start else end - pd.Timedelta(days=365)
    symbols = symbols or DEFAULT_SYMBOLS
    t0 = time.time()
    run_id = run_id or f"{datetime.now(CN_TZ):%Y%m%d-%H%M%S}-{agent}"
    if progress:
        progress("fetching")
    if closes is not None:
        source = "given"
    else:
        closes, source = load_prices(symbols, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", synthetic, seed)
        if not closes and not synthetic:  # 网络或数据源全挂：退回合成数据，并在结果里注明
            closes, source = synthetic_prices(symbols, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", seed), "synthetic(fallback)"
    pit = PointInTime(closes)
    h0 = horizons[0]
    llm_agent = None
    if agent == "llm":
        llm_agent = LLMAgent(llm=llm, blind=blind, max_calls=max_calls, band=band5, price_in=price_in, price_out=price_out)
        if hasattr(llm_agent.llm, "ready") and not llm_agent.llm.ready:
            raise SystemExit("没有可用的大模型：设置 LLM_PROVIDER / LLM_API_KEY，或用 --agent baseline。")
        main_fn, conc = llm_agent, concurrency
    else:
        main_fn, conc = rule_agent, 1
    if progress:
        progress("predicting")
    contenders = {"agent": main_fn, "random": random_baseline(seed), "always_long": long_baseline, "momentum20": momentum_baseline}
    results, preds_main = {}, []
    for name, fn in contenders.items():
        preds = predict(pit, fn, start, end, step, h0, conc if name == "agent" else 1)
        rows = attach_returns(preds, pit, horizons, step)
        results[name] = by_market(rows, horizons, band5)
        if name == "agent":
            preds_main = rows
    out = {"id": run_id, "status": "done", "created": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
           "params": {"symbols": list(pit.symbols), "requested": list(symbols), "start": f"{start:%Y-%m-%d}", "end": f"{end:%Y-%m-%d}",
                      "step": step, "horizons": horizons, "agent": agent, "blind": blind if agent == "llm" else None,
                      "band5%": band5, "seed": seed, "data": source,
                      "missing": [s for s in symbols if s not in pit.symbols]},
           "results": results, "predictions": preds_main,
           "llm": llm_agent.summary() if llm_agent else None, "seconds": round(time.time() - t0, 1),
           "headline": headline(results, horizons)}
    if save:
        d = bt_dir()
        (d / f"{run_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            out["chart"] = chart(out, d / f"{run_id}.png")
            (d / f"{run_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except ImportError:
            out["chart"] = None
    return out


def headline(results: dict, horizons) -> dict:
    a, h = results["agent"]["全部"], str(horizons[0])
    return {"hit_dir%": a["horizons"][h]["hit_dir%"], "hit_all%": a["horizons"][h]["hit_all%"], "n": a["horizons"][h]["n"],
            "long_short%": a["portfolio"]["long_short%"], "buy_hold%": a["portfolio"]["buy_hold%"],
            "baselines": {k: {"hit_dir%": results[k]["全部"]["horizons"][h]["hit_dir%"], "long_short%": results[k]["全部"]["portfolio"]["long_short%"]}
                          for k in results if k != "agent"},
            "by_market": {m: {"hit_dir%": v["horizons"][h]["hit_dir%"], "long_short%": v["portfolio"]["long_short%"]}
                          for m, v in results["agent"].items() if m != "全部"}}


# ====================================================================== 图
IVORY, INK, MUTE, ACCENT = "#F6F3EC", "#181715", "#8A847A", "#9E7B56"
MK_EN = {"全部": "ALL", "A股": "CN · A-SHARES", "港股": "HONG KONG", "美股": "UNITED STATES"}


def chart(res: dict, path: Path) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter

    mk = [m for m in MARKETS if m in res["results"]["agent"]] or ["全部"]
    h = str(res["params"]["horizons"][0])
    plt.rcParams.update({"font.family": "serif", "font.serif": ["DejaVu Serif"], "axes.edgecolor": INK, "axes.linewidth": .5,
                         "xtick.color": MUTE, "ytick.color": MUTE, "xtick.labelsize": 7, "ytick.labelsize": 7})
    fig, axes = plt.subplots(1, len(mk), figsize=(3.6 * len(mk) + .4, 3.1), facecolor=IVORY, squeeze=False)
    for ax, m in zip(axes[0], mk):
        ax.set_facecolor(IVORY)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.axhline(0, color=INK, lw=.4, alpha=.35)
        lines = [("momentum20", ":", .45), ("always_long", "--", .45), ("random", "-", .22)]
        for name, ls, al in lines:
            c = res["results"][name].get(m, {}).get("curve") or []
            if c:
                ax.plot(pd.to_datetime([d for d, _ in c]), [v for _, v in c], color=INK, lw=.7, ls=ls, alpha=al)
        c = res["results"]["agent"][m]["curve"]
        if c:
            ax.plot(pd.to_datetime([d for d, _ in c]), [v for _, v in c], color=ACCENT, lw=1.4)
        r = res["results"]["agent"][m]
        hd = r["horizons"][h]["hit_dir%"]
        ax.set_title(f"{MK_EN.get(m, m)}\nhit {hd if hd is not None else '—'}% · L/S {r['portfolio']['long_short%']:+.1f}%",
                     loc="left", fontsize=8.5, color=INK, linespacing=1.6)
        ax.xaxis.set_major_formatter(DateFormatter("%y·%m"))
        ax.tick_params(length=2, width=.4)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:+.0f}%"))
    fig.text(.01, .015, f"PROBATIO · {res['params']['agent'].upper()} · step {res['params']['step']}d · horizon {h}d   "
                        f"—  agent (bronze)   ·· momentum   -- always long   — random (faint)", fontsize=6.5, color=MUTE)
    fig.tight_layout(rect=(0, .05, 1, 1))
    fig.savefig(path, dpi=160, facecolor=IVORY)
    plt.close(fig)
    return path.name


# ====================================================================== 列表 / CLI
def listing(limit: int = 30) -> list[dict]:
    out = []
    for f in sorted(bt_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        p = j.get("params") or {}
        out.append({"id": j.get("id", f.stem), "status": j.get("status"), "created": j.get("created"), "agent": p.get("agent"),
                    "start": p.get("start"), "end": p.get("end"), "step": p.get("step"), "horizons": p.get("horizons"),
                    "symbols": len(p.get("symbols") or []), "data": p.get("data"), "headline": j.get("headline"),
                    "error": j.get("error"), "chart": j.get("chart")})
    return out


def load(run_id: str) -> dict | None:
    run_id = re.sub(r"[^A-Za-z0-9_.-]", "", run_id)
    f = bt_dir() / f"{run_id}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def run_async(params: dict) -> str:
    """网页 POST /api/backtest：后台线程跑，先写一份 running 状态。"""
    agent = params.get("agent", "baseline") if params.get("agent") in ("baseline", "llm") else "baseline"
    run_id = f"{datetime.now(CN_TZ):%Y%m%d-%H%M%S}-{agent}"
    f = bt_dir() / f"{run_id}.json"
    base = {"id": run_id, "status": "running", "created": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"), "params": {"agent": agent}}
    f.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

    def go():
        try:
            syms = [s.strip() for s in params.get("symbols") or [] if str(s).strip()] if isinstance(params.get("symbols"), list) \
                else [s.strip() for s in str(params.get("symbols") or "").split(",") if s.strip()]
            run(symbols=syms or None, start=params.get("start") or None, end=params.get("end") or None,
                step=int(params.get("step") or 5), horizons=[int(x) for x in (params.get("horizons") or [5, 20])],
                agent=agent, max_calls=int(params.get("max_calls") or 200), run_id=run_id)
        except BaseException as e:
            f.write_text(json.dumps({**base, "status": "error", "error": str(e)[:300]}, ensure_ascii=False), encoding="utf-8")

    threading.Thread(target=go, daemon=True).start()
    return run_id


def print_report(res: dict):
    p = res["params"]
    print(f"Probatio · 回测 {res['id']}  Agent={p['agent']}  {p['start']} → {p['end']}  每 {p['step']} 交易日  数据 {p['data']}")
    if p.get("missing"):
        print("  取不到数据：", ", ".join(p["missing"]))
    hs = [str(h) for h in p["horizons"]]
    for m in ["全部", *MARKETS]:
        if m not in res["results"]["agent"]:
            continue
        print(f"\n【{m}】")
        print(f"  {'':12}" + "".join(f"{'胜率' + h + '日':>10}{'含中性':>8}" for h in hs) + f"{'多空累计':>10}{'买入持有':>10}{'回撤':>8}")
        for name in ("agent", "momentum20", "always_long", "random"):
            r = res["results"][name].get(m)
            if not r:
                continue
            cells = "".join(f"{_f(r['horizons'][h]['hit_dir%']):>12}{_f(r['horizons'][h]['hit_all%']):>10}" for h in hs)
            pf = r["portfolio"]
            print(f"  {name:12}{cells}{_f(pf['long_short%'], '+'):>12}{_f(pf['buy_hold%'], '+'):>12}{_f(pf['mdd%']):>9}")
        cal = res["results"]["agent"][m]["horizons"][hs[0]]["calibration"]
        print("  校准（置信度 → 方向胜率）：" + "  ".join(f"{c['bucket']}:{_f(c['hit%'])}({c['n']})" for c in cal))
    if res.get("llm"):
        print("\nLLM：", json.dumps(res["llm"], ensure_ascii=False))
    print(f"\n结果：{bt_dir() / (res['id'] + '.json')}" + (f"  图：{bt_dir() / res['chart']}" if res.get("chart") else "  （未装 matplotlib，没有出图）"))


def _f(v, sign=""):
    return "—" if v is None else (f"{v:+.1f}%" if sign else f"{v:.1f}%")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m stock_agent backtest", description="Probatio · 时间穿越回测")
    p.add_argument("--symbols", help="逗号分隔，Yahoo 格式；默认 12 只 A/港/美股")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--step", type=int, default=5, help="每隔几个交易日做一次判断")
    p.add_argument("--horizons", default="5,20", help="持有期（交易日），逗号分隔，第一个是主周期")
    p.add_argument("--agent", choices=["baseline", "llm"], default="baseline")
    p.add_argument("--synthetic", action="store_true", help="用合成行情（离线）")
    p.add_argument("--concurrency", type=int, default=4, help="LLM 并发上限")
    p.add_argument("--max-calls", type=int, default=300, help="本次最多调用 LLM 次数（缓存命中不算）")
    p.add_argument("--no-blind", action="store_true", help="LLM 看到真实代码与日期（更容易借训练记忆作弊）")
    p.add_argument("--band", type=float, default=1.5, help="5 日中性区间 ±%%，其它周期按 √(h/5) 缩放")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--price-in", type=float, default=float(os.getenv("LLM_PRICE_IN", 0) or 0), help="每百万输入 token 美元，用于估算费用")
    p.add_argument("--price-out", type=float, default=float(os.getenv("LLM_PRICE_OUT", 0) or 0))
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    from . import settings
    settings.load_env_file()
    res = run(symbols=[s.strip() for s in a.symbols.split(",")] if a.symbols else None, start=a.start, end=a.end, step=a.step,
              horizons=[int(x) for x in a.horizons.split(",")], agent=a.agent, synthetic=a.synthetic, concurrency=a.concurrency,
              max_calls=a.max_calls, blind=not a.no_blind, band5=a.band, seed=a.seed, price_in=a.price_in, price_out=a.price_out)
    if a.json:
        print(json.dumps({k: v for k, v in res.items() if k != "predictions"}, ensure_ascii=False, indent=1))
    else:
        print_report(res)
