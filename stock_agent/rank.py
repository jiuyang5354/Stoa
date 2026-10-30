"""Probatio · 相对强弱排序评估（backtest --mode rank）。

择时（单只股票涨/跌）在第二轮基准里不显著，这里换一个问题：每个决策日把同一批股票一起交给模型，按「未来 h 个交易日的相对表现」
打分排序，用横截面 Rank IC 评估。每个决策日只调用一次模型（整批打分），调用量 = 决策日数。

  python -m stock_agent backtest --mode rank --step 5 --horizons 20,5 --start 2025-09-24 --end 2026-09-24
  python -m stock_agent backtest --mode rank --with-events ...     # 加 point-in-time 公告与已披露财报（见 pit_events.py）
  python -m stock_agent backtest --mode rank --llm mock --synthetic  # 离线跑通

指标（每个方法同口径）：
- 每日 Rank IC = Spearman(分数, 未来 h 日收益)；另算「市场中性」IC（收益先减去同市场当日均值，去掉 A/港/美 大盘差异）。
- IC 均值、标准差、ICIR = 均值/标准差；t 检验（朴素 + Newey–West，滞后 = ceil(h/step)−1，因为持有期重叠）；
  按日移动块自助（块长 = ceil(h/step)）的 IC 均值 95% 区间；
- 头尾分组：每日分数前 3 名等权 − 后 3 名等权的未来 h 日收益（多空差），均值 / NW t / 为正的比例；
- 配对检验：模型 IC − 动量 IC 的逐日差，NW t（Diebold–Mariano 式）。
基线：动量（过去 20 日收益）、反转（过去 5 日收益取负）、随机（固定种子；另给 200 个种子的 IC 均值分布作经验 p）。

防前视：只用 PointInTime.asof()；行情盲测（隐去代码与日期、价格归一到 100、每日随机打乱字母标签）；
公告/财报只给发布日严格早于决策日的，标题里公司名换成「该公司」、年份换成「今年/去年」。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

from . import indicators

RANK_VERSION = "rank-1"
TOP_K = 3
STALE_DAYS = 6  # 某市场放长假（如 A股国庆）时，最后收盘距决策日超过这么多天就不参加当日排序
LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NAMES = {  # 盲测时从公告标题里抹掉的公司名
    "600519.SS": ["贵州茅台", "茅台"], "000858.SZ": ["五粮液", "宜宾五粮液"], "601318.SS": ["中国平安", "平安"],
    "300750.SZ": ["宁德时代", "宁德", "CATL", "Contemporary Amperex"], "0700.HK": ["腾讯控股", "腾讯", "Tencent"],
    "9988.HK": ["阿里巴巴集团", "阿里巴巴", "阿里", "Alibaba"], "0005.HK": ["汇丰控股", "汇丰", "HSBC"],
    "1299.HK": ["友邦保险", "友邦", "AIA"], "AAPL": ["Apple", "苹果"], "MSFT": ["Microsoft", "微软"],
    "NVDA": ["NVIDIA", "英伟达"], "JPM": ["JPMorgan", "摩根大通", "JPMorgan Chase"],
}
CN_DIGITS = "零一二三四五六七八九"


# ====================================================================== 统计小工具（不依赖 scipy）
def rankdata(x: list[float]) -> list[float]:
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def spearman(a, b):
    return pearson(rankdata(list(a)), rankdata(list(b)))


def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-30 else 1e-30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-30 else 1e-30
        de = d * c
        h *= de
        if abs(de - 1) < 1e-10:
            break
    return h


def t_pvalue(t: float, df: int) -> float | None:
    """双边 t 检验 p 值：p = I_{df/(df+t²)}(df/2, 1/2)。"""
    if t is None or df <= 0 or t != t:
        return None
    x = df / (df + t * t)
    a, b = df / 2, 0.5
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) if 0 < x < 1 else (0.0 if x == 0 else 1.0)
    if x == 0 or x == 1:
        return round(front if x == 1 else 0.0, 4)
    if x < (a + 1) / (a + b + 2):
        ib = front * _betacf(a, b, x) / a
    else:
        ib = 1 - front * _betacf(b, a, 1 - x) / b
    return round(min(1.0, max(0.0, ib)), 4)


def nw_se(x: list[float], lag: int) -> float | None:
    """均值的 Newey–West 标准误（Bartlett 权重）。"""
    n = len(x)
    if n < 3:
        return None
    m = sum(x) / n
    e = [v - m for v in x]
    s = sum(v * v for v in e) / n
    for L in range(1, min(lag, n - 1) + 1):
        w = 1 - L / (lag + 1)
        s += 2 * w * sum(e[i] * e[i - L] for i in range(L, n)) / n
    return math.sqrt(max(s, 1e-18) / n)


def block_boot_ci(x: list[float], block: int, reps: int = 2000, seed: int = 7) -> list[float] | None:
    n = len(x)
    if n < 8:
        return None
    rnd, block, vals = random.Random(seed), max(1, block), []
    for _ in range(reps):
        s = []
        while len(s) < n:
            i = rnd.randrange(0, n - block + 1)
            s += x[i:i + block]
        s = s[:n]
        vals.append(sum(s) / n)
    vals.sort()
    return [round(vals[int(reps * .025)], 4), round(vals[int(reps * .975) - 1], 4)]


def summarize(x: list[float], lag: int, block: int) -> dict:
    x = [v for v in x if v is not None]
    n = len(x)
    if n < 3:
        return {"n": n}
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1)) if n > 1 else 0.0
    t = m / (sd / math.sqrt(n)) if sd > 0 else None
    se = nw_se(x, lag)
    tnw = m / se if se else None
    return {"n": n, "mean": round(m, 4), "std": round(sd, 4), "ir": round(m / sd, 3) if sd > 0 else None,
            "t": round(t, 2) if t is not None else None, "p": t_pvalue(t, n - 1) if t is not None else None,
            "t_nw": round(tnw, 2) if tnw is not None else None, "p_nw": t_pvalue(tnw, n - 1) if tnw is not None else None,
            "pos%": round(100 * sum(v > 0 for v in x) / n, 1), "ci95": block_boot_ci(x, block)}


# ====================================================================== 决策日横截面
def cross_sections(pit, start, end, step: int, horizons: list[int]):
    """返回 [(决策日, {symbol: Series 截断行情}, {symbol: {h: 未来收益%}})]。决策日 = 工作日序列每 step 个取一个。"""
    days = [d for d in pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))]
    out = []
    for d in days[::max(1, step)]:
        closes, fwd = {}, {}
        for s in pit.symbols:
            c = pit.asof(s, d)
            if len(c) < 61 or (d - c.index[-1]).days > STALE_DAYS:
                continue
            last_d = c.index[-1]
            f = {}
            for h in horizons:
                x = pit.forward(s, last_d, h)
                f[h] = None if x is None else (x[1] / float(c.iloc[-1]) - 1) * 100
            closes[s], fwd[s] = c, f
        if len(closes) >= 2 * TOP_K:
            out.append((d, closes, fwd))
    return out


def _market(s: str) -> str:
    from .backtest import market
    return market(s)


# ====================================================================== 打分者
def momentum_scores(day, closes, **_):
    return {s: float(c.iloc[-1] / c.iloc[-21] - 1) for s, c in closes.items()}


def reversal_scores(day, closes, **_):
    return {s: -float(c.iloc[-1] / c.iloc[-6] - 1) for s, c in closes.items()}


def momentum60_scores(day, closes, **_):
    return {s: float(c.iloc[-1] / c.iloc[-61] - 1) for s, c in closes.items()}


def lowvol_scores(day, closes, **_):
    return {s: -float(c.pct_change().tail(20).std()) for s, c in closes.items()}


BASELINES = {"momentum20": momentum_scores, "reversal5": reversal_scores, "momentum60": momentum60_scores, "lowvol20": lowvol_scores}


def similarity(agent_rows, xs) -> dict:
    """模型分数与各基线分数的逐日 Spearman 均值：看模型是不是在复述某个简单因子。"""
    cl = {d: c for d, c, _ in xs}
    out = {}
    for name, fn in BASELINES.items():
        v = []
        for d, sc, _ in agent_rows:
            b = fn(d, cl[d])
            k = [s for s in sc if s in b]
            r = spearman([sc[s] for s in k], [b[s] for s in k])
            if r is not None:
                v.append(r)
        out[name] = round(sum(v) / len(v), 3) if v else None
    return out


def random_scores(seed: int):
    def f(day, closes, **_):
        return {s: int(hashlib.md5(f"{seed}|{s}|{day:%Y-%m-%d}".encode()).hexdigest()[:8], 16) for s in closes}
    return f


def _cn_year(y: int) -> str:
    return "".join(CN_DIGITS[int(ch)] for ch in str(y))


def relative_years(text: str, year: int) -> str:
    """盲测：把绝对年份换成相对说法，避免模型借日期调用训练记忆。"""
    rel = {year: "今年", year - 1: "去年", year - 2: "前年", year + 1: "明年"}
    for y, w in rel.items():
        text = text.replace(f"{y}年", w).replace(_cn_year(y) + "年", w).replace(f"{y}-", f"{w}-").replace(str(y), w)
    return re.sub(r"(19|20)\d\d", "某年", text)


def stock_block(c: pd.Series, market: str, blind: bool = True) -> dict:
    c = c.tail(250)
    if blind:
        c = c / float(c.iloc[-1]) * 100
    a = indicators.analyze(c, {})
    last = float(c.iloc[-1])

    def r(n):
        return round((last / float(c.iloc[-n - 1]) - 1) * 100, 2) if len(c) > n else None
    rets = pd.Series(c).pct_change().dropna().tail(60)
    return {"市场": market, "近5日%": r(5), "近20日%": r(20), "近60日%": r(60), "近120日%": r(120),
            "20日年化波动%": round(float(rets.tail(20).std() * math.sqrt(252) * 100), 1) if len(rets) >= 20 else None,
            "RSI14": a.get("rsi"), "距MA20%": round((last / a["ma20"] - 1) * 100, 2) if a.get("ma20") else None,
            "距MA60%": round((last / a["ma60"] - 1) * 100, 2) if a.get("ma60") else None,
            "52周位置%": round((last - a["low_52w"]) / (a["high_52w"] - a["low_52w"]) * 100, 1) if a.get("high_52w") and a["high_52w"] > a["low_52w"] else None,
            "最近40日收盘(归一)": [round(float(v), 1) for v in c.tail(40)]}


RANK_SYSTEM = """你是「Stoa」的回测排序模式。现在是历史上的某一天，下面是同一天的一批股票（来自 A股/港股/美股，代号随机、价格已归一化到最新价 = 100），
你只能看到截至这一天的数据{extra}。
任务：按「之后 {h} 个交易日的相对表现（涨跌幅，在这批股票里比高低）」给每只股票打分 0–100，分数越高 = 预计相对越强。
- 必须给每个代号打分，分数尽量拉开、不要并列；这是相对排序，不是判断大盘涨跌。
- 不要猜是哪只股票、哪一年；只看数据。纯价格数据对短期相对表现的预测力有限，诚实即可。
只输出一行 JSON：{{"scores": {{"A": 整数, "B": 整数, ...}}, "reason": "不超过 60 字"}}"""
EVENTS_NOTE = "，以及每只股票在这一天之前已经发布的公告标题（近 30 天）和最近一期已披露的财报指标（公司名已隐去，年份为相对说法）"


class LLMRanker:
    """每个决策日一次调用，对整批打分。带缓存、并发上限与统计。"""

    def __init__(self, llm=None, blind: bool = True, horizon: int = 20, max_calls: int = 300, events: bool = False,
                 cache=None, events_loader=None):
        from .backtest import LLMCache, bt_dir
        if llm is None:
            from .agent import load_cfg
            from .providers import Router
            llm = Router(load_cfg().get("llm", {}))
        self.llm, self.blind, self.h, self.max_calls, self.events = llm, blind, horizon, max_calls, events
        self.model = repr(llm)
        self.cache = cache or LLMCache(bt_dir() / "llm_cache.db")
        self.events_loader = events_loader
        self._ev = {}
        self.stats = {"calls": 0, "cache_hits": 0, "errors": 0, "skipped_cap": 0, "tokens_in_est": 0, "tokens_out_est": 0,
                      "seconds": 0.0, "events_stocks_days": 0, "events_items": 0}
        self.lock = threading.Lock()

    def ev(self, s):
        if s not in self._ev:
            if self.events_loader:
                self._ev[s] = self.events_loader(s)
            else:
                from . import pit_events
                self._ev[s] = pit_events.load(s)
        return self._ev[s]

    def key(self, day, syms) -> str:
        raw = f"{RANK_VERSION}|{self.model}|{day:%Y-%m-%d}|{','.join(sorted(syms))}|{self.h}|{int(self.blind)}|{int(self.events)}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def labels(self, day, syms) -> dict:
        """每日打乱的代号（同一天有/无公告两组用同一套，消融只差信息）。"""
        order = sorted(syms, key=lambda s: hashlib.md5(f"lab|{day:%Y-%m-%d}|{s}".encode()).hexdigest())
        return {s: LABELS[i] for i, s in enumerate(order)}

    def payload(self, day, closes) -> tuple[dict, dict]:
        from . import pit_events
        lab = self.labels(day, closes)
        out = {}
        for s in sorted(closes, key=lambda s: lab[s]):
            b = stock_block(closes[s], _market(s), self.blind)
            if self.events:
                e = pit_events.asof(self.ev(s), day, names=NAMES.get(s, []) if self.blind else None)
                if self.blind:
                    e = json.loads(relative_years(json.dumps(e, ensure_ascii=False), day.year))
                b["近期公告"], b["最近已披露财报"] = e["近期公告"], e["最近已披露财报"]
                with self.lock:
                    self.stats["events_stocks_days"] += 1
                    self.stats["events_items"] += len(e["近期公告"]) + (1 if e["最近已披露财报"] else 0)
            out[lab[s] if self.blind else s] = b
        return out, lab

    def __call__(self, day, closes, **_) -> dict | None:
        k = self.key(day, closes)
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
        pl, lab = self.payload(day, closes)
        msgs = [{"role": "system", "content": RANK_SYSTEM.format(h=self.h, extra=EVENTS_NOTE if self.events else "")},
                {"role": "user", "content": json.dumps({"截至": "T 日（日期已隐去）" if self.blind else f"{day:%Y-%m-%d}",
                                                         "持有期（交易日）": self.h, "股票": pl}, ensure_ascii=False)}]
        t0 = time.time()
        try:
            msg = self.llm.chat(msgs, None, temperature=0.0, timeout=150)
            sc = parse_scores(msg.get("content") or "", {(lab[s] if self.blind else s): s for s in closes})
        except Exception as e:
            with self.lock:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            return None
        with self.lock:
            self.stats["seconds"] += time.time() - t0
            self.stats["tokens_in_est"] += sum(len(m["content"]) for m in msgs) // 2
            self.stats["tokens_out_est"] += len(msg.get("content") or "") // 2
        self.cache.put(k, sc)
        return sc

    def summary(self) -> dict:
        s = dict(self.stats)
        s["seconds"] = round(s["seconds"], 1)
        s["model"] = self.model
        return s


def parse_scores(text: str, label_to_sym: dict) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("没有 JSON")
    j = json.loads(m.group(0))
    raw = j.get("scores") if isinstance(j.get("scores"), dict) else j
    out = {}
    for lab, s in label_to_sym.items():
        v = raw.get(lab)
        if v is None:
            continue
        try:
            out[s] = float(v)
        except (TypeError, ValueError):
            continue
    if len(out) < max(2 * TOP_K, int(len(label_to_sym) * 0.8)):
        raise ValueError(f"只给了 {len(out)}/{len(label_to_sym)} 只的分数")
    return out


class MockRanker:
    """离线：按 20 日动量 + 小扰动打分，只说明管线是通的。"""
    ready = True

    def __call__(self, day, closes, **_):
        return {s: float(c.iloc[-1] / c.iloc[-21] - 1) + (int(hashlib.md5(f"{s}{day}".encode()).hexdigest()[:4], 16) / 65535 - .5) * .02
                for s, c in closes.items()}

    def summary(self):
        return {"model": "mock"}


# ====================================================================== 评估
def evaluate(scores_by_day: list[tuple], horizons: list[int], step: int) -> dict:
    """scores_by_day: [(day, {sym: score}, {sym: {h: ret}})]。"""
    res = {}
    for h in horizons:
        lag = max(0, math.ceil(h / step) - 1)
        block = max(1, math.ceil(h / step))
        ic, icn, ls, top, bot, days = [], [], [], [], [], []
        for day, sc, fwd in scores_by_day:
            syms = [s for s in sc if fwd.get(s, {}).get(h) is not None]
            if len(syms) < 2 * TOP_K:
                continue
            x = [sc[s] for s in syms]
            y = [fwd[s][h] for s in syms]
            mk = {}
            for s in syms:
                mk.setdefault(_market(s), []).append(fwd[s][h])
            yn = [fwd[s][h] - sum(mk[_market(s)]) / len(mk[_market(s)]) for s in syms]
            a, b = spearman(x, y), spearman(x, yn)
            if a is None:
                continue
            order = sorted(syms, key=lambda s: sc[s])
            t_ = sum(fwd[s][h] for s in order[-TOP_K:]) / TOP_K
            b_ = sum(fwd[s][h] for s in order[:TOP_K]) / TOP_K
            ic.append(a), icn.append(b if b is not None else 0.0), ls.append(t_ - b_), top.append(t_), bot.append(b_), days.append(f"{day:%Y-%m-%d}")
        res[str(h)] = {"days": len(days), "ic": summarize(ic, lag, block), "ic_mkt_neutral": summarize(icn, lag, block),
                       "long_short%": summarize(ls, lag, block), "top_mean%": round(sum(top) / len(top), 3) if top else None,
                       "bottom_mean%": round(sum(bot) / len(bot), 3) if bot else None, "nw_lag": lag, "block": block,
                       "_series": {"days": days, "ic": [round(v, 4) for v in ic], "ic_mn": [round(v, 4) for v in icn], "ls": [round(v, 3) for v in ls]}}
    return res


def paired(a: dict, b: dict, h: str, lag: int, block: int, key: str = "ic") -> dict:
    """逐日差 a−b（同日对齐）。"""
    da = dict(zip(a[h]["_series"]["days"], a[h]["_series"][key]))
    db = dict(zip(b[h]["_series"]["days"], b[h]["_series"][key]))
    d = [da[x] - db[x] for x in da if x in db]
    return summarize(d, lag, block)


def run_rank(symbols=None, start=None, end=None, step=5, horizons=(20, 5), synthetic=False, concurrency=2, max_calls=300,
             blind=True, seed=7, llm=None, llm_kind="auto", events=False, run_id=None, save=True, closes=None,
             ranker=None, random_seeds: int = 200) -> dict:
    from .backtest import DEFAULT_SYMBOLS, PointInTime, bt_dir, load_prices, new_run_id, synthetic_prices, CN_TZ
    from .providers import usage_since
    horizons = [int(h) for h in horizons]
    h0 = horizons[0]
    today = pd.Timestamp.now().normalize()
    end = pd.Timestamp(end) if end else today - pd.Timedelta(days=int(max(horizons) * 1.6) + 3)
    start = pd.Timestamp(start) if start else end - pd.Timedelta(days=365)
    symbols = symbols or DEFAULT_SYMBOLS
    t0, u0 = time.time(), usage_since()
    agent_name = "rank-events" if events else "rank"
    if not run_id:
        run_id = new_run_id(agent_name) if save else f"{datetime.now(CN_TZ):%Y%m%d-%H%M%S}-{agent_name}"
    if closes is not None:
        source = "given"
    else:
        closes, source = load_prices(symbols, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", synthetic, seed)
        if not closes and not synthetic:
            closes, source = synthetic_prices(symbols, f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", seed), "synthetic(fallback)"
    pit = PointInTime(closes)
    xs = cross_sections(pit, start, end, step, horizons)
    if ranker is None:
        if llm_kind == "mock":
            ranker = MockRanker()
        else:
            ranker = LLMRanker(llm=llm, blind=blind, horizon=h0, max_calls=max_calls, events=events)
            if hasattr(ranker.llm, "ready") and not ranker.llm.ready:
                raise SystemExit("没有可用的大模型：设置 LLM_PROVIDER / LLM_API_KEY，或加 --llm mock 只跑通管线。")

    def one(item):
        d, c, f = item
        try:
            return d, ranker(d, c), f
        except Exception:
            return d, None, f
    if concurrency > 1:
        with ThreadPoolExecutor(concurrency) as ex:
            got = list(ex.map(one, xs))
    else:
        got = [one(x) for x in xs]
    agent_rows = [(d, s, f) for d, s, f in got if s]
    methods = {"agent": agent_rows,
               **{k: [(d, fn(d, c), f) for d, c, f in xs] for k, fn in BASELINES.items()},
               "random": [(d, random_scores(seed)(d, c), f) for d, c, f in xs]}
    ok_days = {d for d, _, _ in agent_rows}
    results = {k: evaluate([r for r in v if r[0] in ok_days] if k != "agent" else v, horizons, step) for k, v in methods.items()}
    # 随机基线的经验分布：200 个种子的 IC 均值
    dist = {}
    for h in horizons:
        means = []
        for sd in range(random_seeds):
            e = evaluate([(d, random_scores(1000 + sd)(d, c), f) for d, c, f in xs if d in ok_days], [h], step)
            m = e[str(h)]["ic"].get("mean")
            if m is not None:
                means.append(m)
        a = results["agent"][str(h)]["ic"].get("mean")
        means.sort()
        dist[str(h)] = {"seeds": len(means), "p95": means[int(len(means) * .95) - 1] if means else None,
                        "p05": means[int(len(means) * .05)] if means else None,
                        "agent_pctile": round(100 * sum(m < a for m in means) / len(means), 1) if means and a is not None else None,
                        "p_emp_one_sided": round(sum(m >= a for m in means) / len(means), 3) if means and a is not None else None}
    pairs = {}
    for h in horizons:
        r0 = results["agent"][str(h)]
        pairs[str(h)] = {f"agent_vs_{b}": paired(results["agent"], results[b], str(h), r0["nw_lag"], r0["block"]) for b in ("momentum20", "reversal5", "random")}
    out = {"id": run_id, "status": "done", "mode": "rank", "created": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
           "params": {"symbols": list(pit.symbols), "requested": list(symbols), "start": f"{start:%Y-%m-%d}", "end": f"{end:%Y-%m-%d}",
                      "step": step, "horizons": horizons, "agent": agent_name, "blind": blind, "events": bool(events), "top_k": TOP_K,
                      "decision_days": len(xs), "scored_days": len(agent_rows), "seed": seed, "data": source, "llm_kind": llm_kind,
                      "missing": [s for s in symbols if s not in pit.symbols]},
           "results": results, "random_dist": dist, "paired": pairs, "similarity": similarity(agent_rows, xs),
           "scores": [{"date": f"{d:%Y-%m-%d}", "scores": {k: round(v, 4) for k, v in s.items()},
                       "fwd": {k: {str(h): (round(x, 3) if x is not None else None) for h, x in v.items()} for k, v in f.items()}} for d, s, f in agent_rows],
           "llm": ranker.summary() if hasattr(ranker, "summary") else None, "seconds": round(time.time() - t0, 1),
           "usage": usage_since(u0)}
    if save:
        (bt_dir() / f"{run_id}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def print_rank(res: dict):
    p = res["params"]
    print(f"Probatio · 相对强弱排序 {res['id']}  {'有公告/财报' if p['events'] else '只有行情'}  {p['start']} → {p['end']}  每 {p['step']} 个工作日  "
          f"{len(p['symbols'])} 只  评分日 {p['scored_days']}/{p['decision_days']}  数据 {p['data']}")
    for h in [str(x) for x in p["horizons"]]:
        print(f"\n【未来 {h} 日】NW 滞后 {res['results']['agent'][h]['nw_lag']}，自助块长 {res['results']['agent'][h]['block']}")
        print(f"  {'':11}{'IC均值':>8}{'ICIR':>7}{'t(NW)':>8}{'p(NW)':>8}{'IC>0':>7}  {'95%区间':<18}{'中性IC':>8}{'p':>7}{'多空%':>8}{'p':>7}")
        for k in ("agent", "momentum20", "reversal5", "random", "momentum60", "lowvol20"):
            if k not in res["results"]:
                continue
            r = res["results"][k][h]
            ic, mn, ls = r["ic"], r["ic_mkt_neutral"], r["long_short%"]
            if ic.get("n", 0) < 3:
                print(f"  {k:11} 样本不足")
                continue
            print(f"  {k:11}{ic['mean']:>8.3f}{ic['ir'] if ic['ir'] is not None else 0:>7.2f}{ic['t_nw'] or 0:>8.2f}{ic['p_nw'] or 1:>8.3f}{ic['pos%']:>6.0f}%  "
                  f"{str(ic['ci95']):<18}{mn['mean']:>8.3f}{mn['p_nw'] or 1:>7.3f}{ls['mean']:>8.2f}{ls['p_nw'] or 1:>7.3f}")
        d = res["random_dist"][h]
        print(f"  随机 {d['seeds']} 个种子 IC 均值 5%–95%：{d['p05']} ~ {d['p95']}；模型位于第 {d['agent_pctile']} 百分位（经验单边 p={d['p_emp_one_sided']}）")
        for k, v in res["paired"][h].items():
            if v.get("n", 0) >= 3:
                print(f"  配对 {k}：逐日 IC 差均值 {v['mean']}，NW t={v['t_nw']}，p={v['p_nw']}")
    if res.get("similarity"):
        print("\n模型分数与基线分数的逐日 Spearman 均值：", json.dumps(res["similarity"], ensure_ascii=False))
    if res.get("llm"):
        print("\nLLM：", json.dumps(res["llm"], ensure_ascii=False))
    print("接口用量：", json.dumps(res.get("usage"), ensure_ascii=False), f" 耗时 {res['seconds']} s")
