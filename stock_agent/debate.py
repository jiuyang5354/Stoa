"""Agora · 多角色深度分析：分析师并行 → 多空辩论 → 风控 → 基金经理裁决，自动记账。

用户问「深度分析 X」「该不该买 X」时由 agent 走这里（也作为工具 deep_analysis 给模型调用）；普通快问仍走单 agent。

  分析师（线程池并行）：基本面 / 技术面 / 消息与情绪 / 宏观与行业，各自用现有工具取数，写带证据的短报告
  多空辩论：多方研究员 vs 空方研究员，第 1 轮各自立论，之后每轮反驳对方上一轮（rounds 可配，默认 1）
  风控官：读 USER.md 里的持仓与风险偏好，查集中度、止损、波动；可「降级」或「否决」结论（规则 + 模型，取更严的）
  基金经理：最终 看多/看空/中性 + 置信度 + 关键依据 + 失效条件，然后 record_call 进判断账本

预算：总 LLM 调用数、估算 token 上限、每角色超时。任何角色失败只降级（规则摘要 / 跳过），不让整体失败。
每次辩论存 数据目录/data/debates/<id>.json，网页可展开「辩论纪要」。

回测（backtest --agent debate）：只给技术面分析师截断后的行情，基本面/消息/宏观全部禁用，风控只看波动，不记账，保持未来屏蔽。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cite

CN_TZ = timezone(timedelta(hours=8))
VERSION = "agora-4"  # agora-4：无持仓/仓位信息时风控只做红线规则检查、默认通过，否决只在命中红线时； agora-2：置信度按证据强度分档（CONF_TIERS）；agora-3：风控降级改为按比例向 50 收拢，PM 不再自行二次下调
VIEWS = ("看多", "看空", "中性")
SIGN = {"看多": 1, "看空": -1, "中性": 0}
DEEP_RE = re.compile(r"深度分析|深入分析|全面分析|深度研究|该不该(?:买|卖|加仓|减仓|入手|清仓|抄底)|值不值得(?:买|入手|投资)|多空辩论|辩论一下|deep\s*analysis", re.I)
ALIAS = {"腾讯": "0700.HK", "茅台": "600519.SS", "贵州茅台": "600519.SS", "苹果": "AAPL", "英伟达": "NVDA", "阿里": "9988.HK",
         "阿里巴巴": "9988.HK", "美团": "3690.HK", "小米": "1810.HK", "宁德时代": "300750.SZ", "比亚迪": "002594.SZ", "五粮液": "000858.SZ",
         "中国平安": "601318.SS", "特斯拉": "TSLA", "微软": "MSFT", "谷歌": "GOOGL", "亚马逊": "AMZN", "汇丰": "0005.HK", "友邦": "1299.HK"}

ANALYSTS = [
    ("fundamental", "基本面", "Fundamentals"),
    ("technical", "技术面", "Technicals"),
    ("news", "消息与情绪", "Sentiment"),
    ("macro", "宏观与行业", "Macro"),
]
TITLES = {k: (zh, en) for k, zh, en in ANALYSTS}
TITLES.update({"bull": ("多方研究员", "Bull"), "bear": ("空方研究员", "Bear"), "risk": ("风控官", "Risk"),
               "pm": ("基金经理", "Arbiter"), "editor": ("核对修订", "Editor")})


def debates_dir() -> Path:
    from .memory import home
    d = home() / "data" / "debates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def wants_debate(text: str) -> bool:
    return bool(DEEP_RE.search(text or ""))


def resolve_symbol(text: str, known: dict[str, str] | None = None) -> str | None:
    """从提问里认出一只标的：代码优先，其次中文名（内置常用名 + 自选股名字）。"""
    up = text.upper()
    m = re.search(r"(?<![\d.])(\d{6})\.(SS|SZ|BJ|SH)\b", up)
    if m:
        return f"{m.group(1)}.{'SS' if m.group(2) == 'SH' else m.group(2)}"
    m = re.search(r"(?<![\d.])(\d{4,5})\.HK\b", up)
    if m:
        return f"{m.group(1)[-4:].zfill(4)}.HK"
    names = dict(ALIAS)
    for sym, nm in (known or {}).items():
        if nm and nm != sym:
            from .graph import aliases
            for a in aliases(nm):
                names.setdefault(a, sym)
    for nm in sorted(names, key=len, reverse=True):
        if nm in text:
            return names[nm]
    m = re.search(r"(?<![A-Za-z$])\$?([A-Z]{1,5})(?![A-Za-z])", text)
    if m and m.group(1) not in ("RSI", "SEC", "AI", "ETF", "PE", "ROE", "MA", "A", "I"):
        return m.group(1)
    return None


# ====================================================================== 预算
class BudgetError(Exception):
    pass


class Budget:
    """总调用数 / 估算 token 上限 / 每角色超时。线程安全。"""

    def __init__(self, max_calls: int = 14, max_tokens: int = 60000, role_timeout: float = 60.0):
        self.max_calls, self.max_tokens, self.role_timeout = int(max_calls), int(max_tokens), float(role_timeout)
        self.calls = self.tokens = self.refused = 0
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(8, thread_name_prefix="agora")

    def left(self) -> int:
        return self.max_calls - self.calls

    def chat(self, llm, messages: list[dict]) -> str:
        est = sum(len(m.get("content") or "") for m in messages) // 2
        with self.lock:
            if self.calls >= self.max_calls:
                self.refused += 1
                raise BudgetError(f"已到 LLM 调用上限 {self.max_calls}")
            if self.tokens + est > self.max_tokens:
                self.refused += 1
                raise BudgetError(f"已到 token 上限 {self.max_tokens}")
            self.calls += 1
            self.tokens += est
        fut = self.pool.submit(llm.chat, messages, None, 0.2, int(self.role_timeout) + 5)
        try:
            msg = fut.result(timeout=self.role_timeout)
        except FutTimeout:
            raise TimeoutError(f"超过 {self.role_timeout:g} 秒")
        out = (msg or {}).get("content") or ""
        with self.lock:
            self.tokens += len(out) // 2
        return out

    def usage(self) -> dict:
        return {"calls": self.calls, "max_calls": self.max_calls, "tokens_est": self.tokens, "max_tokens": self.max_tokens,
                "role_timeout": self.role_timeout, "refused": self.refused}

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("没有 JSON")
    return json.loads(m.group(0))


def norm_view(v) -> str:
    v = str(v or "").strip()
    v = {"看涨": "看多", "看跌": "看空", "震荡": "中性", "bull": "看多", "bear": "看空", "neutral": "中性", "bullish": "看多", "bearish": "看空"}.get(v.lower() if v.isascii() else v, v)
    return v if v in VIEWS else "中性"


def degrade_conf(c, shrink: float | None = None) -> int:
    """风控降级：置信度按比例向 50 收拢 50+(c−50)×shrink（默认 0.5，DEBATE_DEGRADE_SHRINK 可调，0 = 降级即中性置信度）。
    旧做法「−10、下限 50」会把 55–60 全压成 50，第二轮基准里 209 个方向判断有 146 个=50，置信度失去区分度。"""
    if shrink is None:
        try:
            shrink = float(os.getenv("DEBATE_DEGRADE_SHRINK") or 0.5)
        except ValueError:
            shrink = 0.5
    shrink = max(0.0, min(1.0, shrink))
    c = clamp_conf(c)
    return clamp_conf(math.floor(50 + (c - 50) * shrink + 0.5))


def clamp_conf(x, lo=50, hi=95) -> int:
    try:
        return int(max(lo, min(hi, float(x))))
    except (TypeError, ValueError):
        return lo


def _trim(obj, k):
    if isinstance(obj, dict):
        return {a: _trim(b, k) for a, b in obj.items()}
    if isinstance(obj, list):
        return [_trim(b, k) for b in obj[:max(3, k // 20)]]
    if isinstance(obj, str):
        return obj[:k]
    return obj


def _short(obj, n=5000) -> str:
    """压到 n 字以内，尽量保持合法 JSON（先截长字符串和长列表，最后才硬截）。"""
    t = json.dumps(obj, ensure_ascii=False, default=str)
    for k in (300, 160, 80, 40):
        if len(t) <= n:
            return t
        t = json.dumps(_trim(obj, k), ensure_ascii=False, default=str)
    return t[:n]


# ====================================================================== 提示词
COMMON = """你是 Stoa 投研团队里的「{title}」。只能使用下面给你的数据，不得编造数字、新闻或日期；
数据里每份资料带 `_cite` 编号，写到具体数字时在后面标 [编号]。没有数据就直说没有。用中文，简短。
[ROLE:{key}]"""

PROMPTS = {
    "fundamental": "看估值（市盈率/市净率/股息率）、盈利能力（ROE、毛利率、净利率）、增长（营收/净利同比）、负债与现金流、分析师一致预期（目标价区间、相对现价空间、评级分布、EPS 预测）；有年报对比时看今年年报新增/删除了哪些风险（写页码）。",
    "technical": "看趋势（价格与 20/60 日均线的位置关系）、动量（5/20 日涨跌幅）、RSI 超买超卖、52 周高低点位置与信号；A股看近 5 日主力/超大单资金流向，美股看期权 Put/Call 比、隐含波动率和最大痛点。",
    "news": "看最近新闻和公告：有没有回购、业绩、监管、减持、重大合同等会改变预期的事件，市场情绪偏向哪边；A股看最近有没有上龙虎榜、席位是机构还是游资。没有新闻就判中性。",
    "macro": "看大盘与指数环境、行业与政策动向（来自搜索结果），判断外部环境对这只股票是顺风还是逆风。",
}
CONF_TIERS = """confidence 按证据强度分档（同样强度给同样的分，档位之间要拉开，不要习惯性一律给 50–55）：
  50–54 信号互相矛盾、很弱或数据不足，接近抛硬币；
  55–62 只有一类信号支持（如只有趋势，或只有超买/超卖），或有明显反向信号；
  63–72 两类以上独立信号同向（如趋势 + 动量 + 资金/事件/基本面），没有明显反向信号；
  73–85 多类强信号同向且幅度大、证据有出处，少用；85 以上几乎不用。"""
ANALYST_OUT = CONF_TIERS + '\n只输出 JSON：{"stance": "看多|看空|中性", "confidence": 50-95 的整数, "summary": "不超过 80 字的判断", "evidence": ["证据1（带数字和 [编号]）", "证据2", "证据3"]}'

BULL = """你是多方研究员，立场是找出买入的理由。基于分析师报告{rebut}写出你的论点。要有证据，不要夸大，不许承诺收益。
只输出 JSON：{{"argument": "不超过 120 字的多方论点", "points": ["要点1", "要点2"], "rebuttal": "对空方的反驳（第一轮可留空）"}}
[ROLE:bull]"""
BEAR = """你是空方研究员，立场是找出风险和卖出的理由。基于分析师报告{rebut}写出你的论点。要有证据，不要危言耸听。
只输出 JSON：{{"argument": "不超过 120 字的空方论点", "points": ["要点1", "要点2"], "rebuttal": "对多方的反驳（第一轮可留空）"}}
[ROLE:bear]"""
RISK = """你是风控官。根据用户持仓与风险偏好、规则检查结果和多空双方的论点，决定这次结论能否放行：
- 通过：正常给结论（默认）；降级：置信度下调、仓位从轻；否决：不得给出「看多/加仓」结论。
- 只有命中明确红线（规则检查 redlines 非空：停牌、ST、流动性不足、极端波动、杠杆、单票仓位 ≥40%）才可以否决；
  看多/看空观点有分歧、股价涨了或跌了本身都不是降级理由——那是基金经理的置信度要管的事。
- 只有和用户持仓/偏好直接相关的具体问题（如已偏重仓、偏好保守而波动大）才降级。
规则检查是硬约束，你只能比它更严，不能放松。
只输出 JSON：{"decision": "通过|降级|否决", "max_position_pct": 建议单票最大仓位百分比整数, "stop_loss": "止损参考（带数字和 [编号]，没有数据就写原则）", "notes": ["理由1", "理由2"]}
[ROLE:risk]"""
PM = """你是基金经理（主持人），综合分析师报告、多空辩论和风控意见，给出最终结论。
- view：看多/看空/中性（预计 {h} 个交易日内的方向）；confidence 50–95，要诚实，分歧大就低。
{tiers}
- 风控否决时不能给看多；风控降级时系统会按比例把置信度向 50 收拢，你按证据强度给原始置信度即可，不要自己再下调。
- key_points 写 3 条最关键的依据（带数字和 [编号]）；invalidation 写清楚「什么情况下这个结论失效」（如跌破某价位、某事件发生）。
只输出 JSON：{{"view": "看多|看空|中性", "confidence": 整数, "horizon_days": {h}, "summary": "不超过 80 字", "key_points": ["…", "…", "…"], "invalidation": "…"}}
[ROLE:pm]"""
EDITOR = """你是核对修订。下面这段分析里有些数字在数据里找不到出处，请改写：删掉这些数字或改成「数据未提供」，其它内容和 [编号] 保持不变。
只输出改写后的全文。
[ROLE:editor]"""


# ====================================================================== 规则兜底（降级时用，也是 mock 的依据）
def _find_quote(data) -> dict | None:
    """在一堆工具结果里找行情字典（实盘 get_quotes 或回测 snapshot_payload）。"""
    if isinstance(data, dict):
        if "close" in data and ("ma20" in data or "rsi" in data):
            return data
        if "指标" in data and isinstance(data["指标"], dict):
            return {**data["指标"], "_cite": data.get("_cite")}
        for v in data.values():
            q = _find_quote(v)
            if q:
                return q
    if isinstance(data, list):
        for v in data:
            q = _find_quote(v)
            if q:
                return q
    return None


def _c(d) -> str:
    return f"[{d['_cite']}]" if isinstance(d, dict) and d.get("_cite") else ""


def _find(data, key):
    """在工具结果里找带某个键的字典（资金流 rows、期权 expiries、一致预期 target…）。"""
    for d in data if isinstance(data, list) else [data]:
        if isinstance(d, dict) and key in d and not d.get("error"):
            return d
    return None


def flow_evidence(data) -> tuple[list[str], int]:
    """资金流/期权的证据行 + 方向分（+1 / -1 / 0），只作参考，不单独决定结论。"""
    ev, sc = [], 0
    f = _find(data, "main_net_5d")
    if f and f.get("main_net_5d") is not None:
        k = _c(f)
        if f.get("unit") == "%":  # 回测盲测：只有主力净占比
            ev.append(f"近 5 日主力净占比合计 {f['main_net_5d']:+}%{k}，{f.get('inflow_days_5d', 0)} 天净流入")
        else:
            ev.append(f"近 5 日主力净{'流入' if f['main_net_5d'] >= 0 else '流出'} {abs(f['main_net_5d'])} 亿元{k}，{f.get('inflow_days_5d', 0)} 天净流入")
        sc = 1 if f["main_net_5d"] > 0 and f.get("inflow_days_5d", 0) >= 3 else -1 if f["main_net_5d"] < 0 and f.get("inflow_days_5d", 5) <= 2 else 0
    o = _find(data, "expiries")
    if o and o.get("expiries"):
        e0, k = o["expiries"][0], _c(o)
        bits = [f"{e0['expiry']} 到期 Put/Call 持仓比 {e0['pcr_oi']}{k}" if e0.get("pcr_oi") is not None else ""]
        if e0.get("atm_iv_pct") is not None:
            bits.append(f"ATM 隐含波动率 {e0['atm_iv_pct']}%{k}")
        if e0.get("max_pain") is not None:
            bits.append(f"最大痛点 {e0['max_pain']}{k}")
        ev.append("，".join(b for b in bits if b))
        if e0.get("pcr_oi") is not None:
            sc = 1 if e0["pcr_oi"] < .7 else -1 if e0["pcr_oi"] > 1.2 else 0
    return [e for e in ev if e], sc


def rule_technical(data) -> dict:
    q = _find_quote(data)
    if not q or q.get("close") is None:
        return {"stance": "中性", "confidence": 50, "summary": "没有可用的行情数据。", "evidence": []}
    c, m20, m60, r = q.get("close"), q.get("ma20"), q.get("ma60"), q.get("rsi")
    ev, k = [f"收盘 {c}{_c(q)}"], _c(q)
    if m20:
        ev.append(f"20 日均线 {m20}{k}" + (f"，60 日均线 {m60}{k}" if m60 else ""))
    if r is not None:
        ev.append(f"RSI {r}{k}" + ("，超买" if r >= 70 else "，超卖" if r <= 30 else ""))
    if q.get("chg_20d") is not None:
        ev.append(f"近 20 日 {q['chg_20d']:+}%{k}")
    stance, conf = "中性", 50
    if m20 and m60 and c > m20 > m60:
        stance, conf = "看多", 60
    elif m20 and m60 and c < m20 < m60:
        stance, conf = "看空", 60
    if r is not None and ((stance == "看多" and r >= 70) or (stance == "看空" and r <= 30)):
        conf -= 5
    gap = (c / m20 - 1) * 100 if m20 else 0
    if stance != "中性":
        conf = clamp_conf(conf + min(15, abs(gap) * 2))
    summ = {"看多": "价格站上 20 日线且 20 日线在 60 日线上方，趋势偏多", "看空": "价格在 20 日线下且 20 日线低于 60 日线，趋势偏空",
            "中性": "均线纠缠，趋势不明"}[stance]
    fe, fs = flow_evidence(data)
    if fe:
        ev += fe
        if stance != "中性" and fs == SIGN[stance]:
            conf = clamp_conf(conf + 3)
            summ += "，资金面配合"
        elif stance != "中性" and fs == -SIGN[stance]:
            conf = clamp_conf(conf - 5)
            summ += "，但资金面背离"
    return {"stance": stance, "confidence": conf, "summary": summ, "evidence": ev}


def _val(fin: dict, *keys):
    for k in keys:
        for d in (fin, fin.get("valuation") or {}):
            if isinstance(d, dict) and isinstance(d.get(k), (int, float)):
                return d[k]
    return None


def rule_fundamental(data) -> dict:
    fin = next((d for d in (data or []) if isinstance(d, dict) and not d.get("error") and "passages" not in d and "counts" not in d
                and "target" not in d), None) if isinstance(data, list) else data
    cons = _find(data, "target") if isinstance(data, list) else None
    cmpf = _find(data, "counts") if isinstance(data, list) else None
    if (not isinstance(fin, dict) or fin.get("error") or not fin) and cons:
        fin = {}
    if not isinstance(fin, dict) or fin.get("error") or (not fin and not cons):
        return {"stance": "中性", "confidence": 50, "summary": "取不到财务数据，基本面无法判断。", "evidence": []}
    pe, roe, g = _val(fin, "pe", "市盈率TTM"), _val(fin, "roe", "ROE%"), _val(fin, "revenue_growth", "营收同比%")
    ev, k = [], _c(fin)
    if pe is not None:
        ev.append(f"市盈率 {pe}{k}")
    if roe is not None:
        ev.append(f"ROE {roe}%{k}")
    if g is not None:
        ev.append(f"营收同比 {g}%{k}")
    score = 0
    if pe is not None:
        score += 1 if pe < 18 else -1 if pe > 45 else 0
    if roe is not None:
        score += 1 if roe >= 15 else -1 if roe < 5 else 0
    if g is not None:
        score += 1 if g >= 10 else -1 if g < 0 else 0
    if cons:
        t, k = cons.get("target") or {}, _c(cons)
        if t.get("low") and t.get("high"):
            ev.append(f"分析师目标价 {t['low']}–{t['high']}{k}" + (f"，相对现价空间 {cons['upside_pct']:+}%{k}" if cons.get("upside_pct") is not None else ""))
        if cons.get("upside_pct") is not None:
            score += 1 if cons["upside_pct"] >= 20 else -1 if cons["upside_pct"] < 0 else 0
    if cmpf and cmpf.get("counts"):
        c, k = cmpf["counts"], _c(cmpf)
        ev.append(f"{cmpf.get('year_b')} 年报风险章节较上年新增 {c.get('added')} 句、删去 {c.get('removed')} 句{k}")
    stance = "看多" if score >= 2 else "看空" if score <= -2 else "中性"
    return {"stance": stance, "confidence": clamp_conf(50 + abs(score) * 6), "evidence": ev,
            "summary": {"看多": "估值合理、盈利能力强", "看空": "估值偏高或盈利走弱", "中性": "估值与盈利没有明显偏向"}[stance]}


POS_WORDS = re.compile(r"回购|增持|增长|超预期|中标|上调|创新高|分红|获批|走强|大涨")
NEG_WORDS = re.compile(r"减持|下调|调查|处罚|亏损|下滑|诉讼|退市|暴跌|不及预期|走弱|大跌")


def rule_news(data) -> dict:
    items = []
    lhb0 = _find(data or [], "listed_recently")
    if lhb0 and lhb0.get("listed_recently") and not any(isinstance(d, dict) and (d.get("news") or d.get("announcements")) for d in data or []):
        r0 = lhb0["records"][0]
        return {"stance": "中性", "confidence": 50, "summary": "最近没有新闻，只有龙虎榜记录。",
                "evidence": [f"{r0['date']} 上龙虎榜，龙虎榜净买 {r0.get('net_buy_yi')} 亿元{_c(lhb0)}"]}
    for d in data or []:
        if isinstance(d, dict):
            for key in ("news", "announcements"):
                items += [x for x in d.get(key) or [] if isinstance(x, dict)]
    if not items:
        return {"stance": "中性", "confidence": 50, "summary": "最近没有取到新闻或公告。", "evidence": []}
    pos = sum(bool(POS_WORDS.search(x.get("title", ""))) for x in items)
    neg = sum(bool(NEG_WORDS.search(x.get("title", ""))) for x in items)
    stance = "看多" if pos > neg else "看空" if neg > pos else "中性"
    ev = [f"{str(x.get('time') or x.get('date') or '')[:10]} {x.get('title', '')}{_c(x)}" for x in items[:3]]
    lhb = _find(data, "listed_recently")
    if lhb and lhb.get("listed_recently") and lhb.get("records"):
        r0 = lhb["records"][0]
        ev.append(f"{r0['date']} 上龙虎榜（{(r0.get('reason') or '')[:24]}），龙虎榜净买 {r0.get('net_buy_yi')} 亿元{_c(lhb)}")
    return {"stance": stance, "confidence": clamp_conf(50 + abs(pos - neg) * 6, hi=75), "evidence": ev,
            "summary": f"最近 {len(items)} 条资讯里偏正面 {pos} 条、偏负面 {neg} 条。"}


def rule_macro(data) -> dict:
    rows = []
    for d in data or []:
        if isinstance(d, dict) and isinstance(d.get("indices"), dict):
            for mk, xs in d["indices"].items():
                for x in xs:
                    if isinstance(x, dict) and not isinstance(x.get("chg_pct"), (int, float)) and isinstance(x.get("data"), dict):
                        x = {"name": x.get("name"), **x["data"]}  # market_overview 实盘格式：{symbol, name, data: {...}}
                    if isinstance(x, dict) and isinstance(x.get("chg_pct"), (int, float)):
                        rows.append((x, d))
    if not rows:
        return {"stance": "中性", "confidence": 50, "summary": "没有取到大盘数据。", "evidence": []}
    avg = sum(x["chg_pct"] for x, _ in rows) / len(rows)
    stance = "看多" if avg > .3 else "看空" if avg < -.3 else "中性"
    ev = [f"{x.get('name')} {x.get('chg_pct'):+}%{_c(d)}" for x, d in rows[:3]]
    return {"stance": stance, "confidence": 55 if stance != "中性" else 50, "evidence": ev,
            "summary": {"看多": "主要指数普遍走强，外部环境偏顺风", "看空": "主要指数走弱，外部环境偏逆风", "中性": "大盘涨跌不大，环境中性"}[stance]}


RULES = {"fundamental": rule_fundamental, "technical": rule_technical, "news": rule_news, "macro": rule_macro}


def holdings_check(user_entries: list[str], symbol: str, names: set[str], quote: dict | None) -> dict:
    """风控规则：持仓集中度、风险偏好、超买、止损参考。返回 {decision, findings, max_position_pct, stop_loss}。"""
    find, decision, cap = [], "通过", 20
    mine = [e for e in user_entries if symbol.upper() in e.upper() or any(n and n in e for n in names)]
    share = None
    for e in mine:
        m = re.search(r"(?:占|仓位)[^\d]{0,6}(\d{1,3}(?:\.\d+)?)\s?%", e) or re.search(r"(\d{1,3}(?:\.\d+)?)\s?%\s?(?:仓位|的仓|总仓)", e)
        if m:
            share = float(m.group(1))
    if share is not None:
        if share >= 40:
            decision = "否决"
            find.append(f"你在这只股票上的仓位约 {share:g}%，已严重集中，不建议再加仓")
        elif share >= 25:
            decision = "降级"
            find.append(f"你在这只股票上的仓位约 {share:g}%，偏集中，新增仓位要从轻")
        else:
            find.append(f"你在这只股票上的仓位约 {share:g}%，集中度可接受")
    elif mine:
        find.append("你已持有这只股票：" + re.sub(r"^\[[\d-]+\]\s*", "", mine[0])[:60])
    others = []
    for e in user_entries:
        for m in re.finditer(r"(\d{1,3}(?:\.\d+)?)\s?%", e):
            if ("仓" in e or "占" in e) and float(m.group(1)) >= 40 and e not in mine:
                others.append(float(m.group(1)))
    if others:
        find.append(f"组合里另有单票仓位约 {max(others):g}%，整体集中度偏高")
        decision = "降级" if decision == "通过" else decision
    conservative = any(re.search(r"保守|稳健|低风险|不碰杠杆", e) for e in user_entries)
    if conservative:
        cap = 10
        find.append("你的风险偏好偏保守，单票仓位上限按 10% 计")
    stop = None
    if quote and quote.get("close"):
        k = _c(quote)
        r = quote.get("rsi")
        if r is not None and r >= 70:
            find.append(f"RSI {r}{k} 处于超买区，追高风险大")
            if conservative:
                decision = "降级" if decision == "通过" else decision
        ref = quote.get("ma60") or quote.get("ma20")
        if ref:
            stop = f"收盘跌破 60 日均线 {ref}{k} 止损" if quote.get("ma60") else f"收盘跌破 20 日均线 {ref}{k} 止损"
    return {"decision": decision, "findings": find, "max_position_pct": cap, "stop_loss": stop or "单票亏损超过 8% 止损"}


STRICT = {"通过": 0, "降级": 1, "否决": 2}

# 风控红线阈值（明确、可解释的硬规则；没命中就默认通过）
RED = {"stale_days": 6,          # 最后一根 K 线距决策日超过 6 个工作日 → 视为停牌/无法交易
       "vol_veto": 150.0,        # 20 日年化波动率 ≥150% → 否决
       "vol_warn": 80.0,         # ≥80% → 降级
       "amount_min": 1e7,        # 近 20 日日均成交额 < 1000 万（本币）→ 流动性不足，否决（有成交额数据时才检查）
       "day_move": 15.0}         # 单日涨跌幅绝对值 ≥15% → 降级（异常波动）
LEVER_RE = re.compile(r"杠杆|融资买|配资|借钱|借款炒|两融|保证金|期货|爆仓|满仓加")
LEVER_HELD_RE = re.compile(r"(融资|两融)(买入|持仓|负债)|配资|杠杆\s*\d|\d\s*倍杠杆|借钱(炒|买)")
ST_RE = re.compile(r"^\s*(\*|S\*?)?ST\s?[\u4e00-\u9fff]|退市|退$")


def _today() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def has_position_info(user_entries: list[str], symbol: str = "", names: set[str] | None = None) -> bool:
    """用户是否给过持仓/仓位/风险偏好信息——只有给过，LLM 风控官才有东西可判。"""
    for e in user_entries or []:
        if re.search(r"持有|持仓|仓位|占.{0,6}\d{1,3}(\.\d+)?\s?%|买入了|成本价|保守|稳健|激进|风险偏好|杠杆|融资", e):
            return True
    return False


def redline_check(quote: dict | None, name: str = "", question: str = "", user_entries: list[str] | None = None,
                  meta: dict | None = None) -> dict:
    """明确红线：停牌/数据中断、ST/退市、流动性不足、极端波动、用杠杆。返回 {decision, red:[命中的红线], findings, checked}。
    这里只看能客观判定的东西，不看方向；没命中任何一条 → 通过。"""
    meta, red, warn, checked = meta or {}, [], [], []
    k = _c(quote) if quote else ""
    stale = meta.get("stale_days")
    if stale is None and quote and quote.get("date") and not meta.get("backtest"):
        try:
            import numpy as _np
            stale = int(_np.busday_count(str(quote["date"])[:10], _today()))
        except Exception:
            stale = None
    if stale is not None:
        checked.append("停牌")
        if stale > RED["stale_days"]:
            red.append(f"最近 {stale} 个工作日没有成交数据，疑似停牌或数据中断，无法正常交易")
    if name and name not in ("某股票",):
        checked.append("ST")
        if ST_RE.search(name):
            red.append(f"{name} 属于 ST/退市整理类股票，风险警示")
    amt = meta.get("amount_avg20") or (quote or {}).get("amount_avg20")
    if amt is not None:
        checked.append("流动性")
        if float(amt) < RED["amount_min"]:
            red.append(f"近 20 日日均成交额约 {float(amt) / 1e4:.0f} 万，流动性不足，进出困难")
    vol = (quote or {}).get("vol_20d")
    if vol is not None:
        checked.append("波动")
        if vol >= RED["vol_veto"]:
            red.append(f"20 日年化波动率 {vol}%{k}，极端波动")
        elif vol >= RED["vol_warn"]:
            warn.append(f"20 日年化波动率 {vol}%{k}，波动很大，仓位要轻")
    mv = (quote or {}).get("chg_pct")
    if mv is not None and abs(mv) >= RED["day_move"]:
        warn.append(f"单日涨跌 {mv}%{k}，异常波动")
    checked.append("杠杆")
    if (question and LEVER_RE.search(question)) or any(LEVER_HELD_RE.search(e) and not re.search(r"不(碰|用|加)杠杆", e) for e in user_entries or []):
        red.append("涉及杠杆/融资/借钱交易：亏损会被放大并可能被强平，不给看多/加仓结论")
    dec = "否决" if red else "降级" if warn else "通过"
    return {"decision": dec, "red": red, "findings": red + warn, "checked": checked}


# ====================================================================== 辩论
class Debate:
    def __init__(self, llm, symbol: str, name: str = "", question: str = "", tools=None, rounds: int = 1,
                 budget: Budget | None = None, user_entries: list[str] | None = None, sources: cite.Sources | None = None,
                 mode: str = "live", snapshot_payload: dict | None = None, horizon: int = 20, on_step=None, flow_payload: dict | None = None,
                 risk_meta: dict | None = None):
        self.llm, self.symbol, self.name, self.question = llm, symbol, name or symbol, question
        self.tools, self.rounds, self.mode = tools, max(1, min(3, int(rounds))), mode
        self.budget = budget or Budget()
        self.user_entries = user_entries or []
        self.sources = sources or cite.Sources()
        self.payload, self.horizon, self.on_step = snapshot_payload, int(horizon), on_step
        self.risk_meta = risk_meta  # 风控红线的额外事实（回测：stale_days；有成交额时 amount_avg20）
        self.flow_payload = flow_payload  # 回测：按决策日截断的资金流（只有 --with-flow 时才有）
        self.id = f"{datetime.now(CN_TZ):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        self.roles: dict[str, dict] = {}
        self.lock = threading.Lock()

    # -------------------------------------------------- 工具
    def tool(self, name, args):
        if self.mode == "backtest" or self.tools is None:
            raise PermissionError(f"回测模式禁止调用 {name}（防未来泄漏）")
        try:
            return self.tools(name, args)
        except Exception as e:
            return {"error": f"{name} 出错：{str(e)[:100]}"}

    def ask(self, role: str, system: str, user: str) -> str:
        return self.budget.chat(self.llm, [{"role": "system", "content": system}, {"role": "user", "content": user}])

    def _rec(self, key, **kw):
        zh, en = TITLES[key]
        with self.lock:
            r = self.roles.setdefault(key, {"key": key, "title": zh, "en": en})
            r.update(kw)
        if self.on_step:
            try:
                self.on_step(key, kw.get("status"))
            except Exception:
                pass
        return r

    def _flag(self, text: str) -> tuple[str, list[str]]:
        t, bad = cite.flag(text, self.sources)
        return re.sub(r"\n\n注：标「未核实」.*$", "", t, flags=re.S), bad

    # -------------------------------------------------- 分析师
    def market(self) -> str:
        s = self.symbol.upper()
        return "A" if re.fullmatch(r"\d{6}(\.(SS|SZ|BJ|SH))?", s) else "HK" if s.endswith(".HK") else "IDX" if s.startswith("^") else "US"

    def filing_brief(self):
        """基本面分析师用：最近两年年报「风险因素」对比。首次要下载解析两份报告，限时 FILING_TIMEOUT 秒，超时就跳过（后台继续缓存）。"""
        import os
        if os.getenv("DEBATE_FILINGS", "on").lower() in ("off", "0", "false"):
            return None
        y = datetime.now(CN_TZ).year - 1
        box = {}

        def go():
            for yb in (y, y - 1):
                r = self.tool("compare_filings", {"symbol": self.symbol, "year_a": yb - 1, "year_b": yb, "section": "risk"})
                box["r"] = r
                if not (isinstance(r, dict) and r.get("error") and "找不到" in str(r.get("error"))):
                    break

        th = threading.Thread(target=go, daemon=True)
        th.start()
        th.join(float(os.getenv("DEBATE_FILING_TIMEOUT") or 25))
        if th.is_alive():
            return {"error": "年报对比超时（首次需下载两份年报），已跳过；后台继续缓存，下次深度分析可用"}
        r = box.get("r")
        if isinstance(r, dict) and not r.get("error"):  # 只给分析师看关键句，控制篇幅
            return {k: r.get(k) for k in ("symbol", "section_name", "year_a", "year_b", "counts", "unchanged_ratio", "_cite")} | {
                "added": (r.get("added") or [])[:4], "removed": (r.get("removed") or [])[:3]}
        return r

    def gather(self, key) -> list:
        s = self.symbol
        if key == "technical":
            if self.mode == "backtest":
                return [self.payload] + ([self.flow_payload] if getattr(self, "flow_payload", None) else [])
            out = [self.tool("get_quotes", {"symbols": [s]})]
            if self.market() == "A":
                out.append(self.tool("get_fund_flow", {"symbol": s, "days": 10}))
            elif self.market() == "US":
                out.append(self.tool("get_options", {"symbol": s}))
            return out
        if self.mode == "backtest":
            return []
        if key == "fundamental":
            out = [self.tool("get_financials", {"symbol": s})]
            if self.market() != "IDX":
                out.append(self.tool("get_consensus", {"symbol": s}))
                fb = self.filing_brief()
                if fb is not None:
                    out.append(fb)
            return out
        if key == "news":
            out = [self.tool("get_news", {"symbol": s, "limit": 6}), self.tool("get_announcements", {"symbol": s, "limit": 6})]
            if self.market() == "A":
                out.append(self.tool("get_dragon_tiger", {"symbol": s, "limit": 3}))
            return out
        if key == "macro":
            return [self.tool("market_overview", {}), self.tool("web_search", {"query": f"{self.name} 行业 政策 最新", "limit": 4})]
        return []

    def analyst(self, key, zh):
        t0 = time.time()
        if self.mode == "backtest" and key != "technical":
            return self._rec(key, status="disabled", note="回测模式禁用（防未来泄漏）", stance="中性", confidence=50, summary="", evidence=[], seconds=0)
        data = self.gather(key)
        if key == "technical":
            self._quote = _find_quote(data)
        usable = [d for d in data if d and not (isinstance(d, dict) and d.get("error") and len(d) <= 2)]
        mk = self.market()
        tools_used = {"technical": ["get_quotes"] + (["get_fund_flow"] if mk == "A" else ["get_options"] if mk == "US" else []),
                      "fundamental": ["get_financials"] + (["get_consensus", "compare_filings"] if mk != "IDX" else []),
                      "news": ["get_news", "get_announcements"] + (["get_dragon_tiger"] if mk == "A" else []),
                      "macro": ["market_overview", "web_search"]}[key] if self.mode != "backtest" else (["截断行情"] + (["截断资金流"] if getattr(self, "flow_payload", None) else []))
        if not usable:
            out = RULES[key](data)
            return self._rec(key, status="no_data", note="数据源没有返回可用数据", tools=tools_used, seconds=round(time.time() - t0, 2), **out)
        sys = COMMON.format(title=f"{zh}分析师", key=key) + "\n" + PROMPTS[key] + "\n" + ANALYST_OUT
        user = f"标的：{'某股票（回测盲测）' if self.mode == 'backtest' else f'{self.name}（{self.symbol}）'}\n用户问题：{self.question or '深度分析'}\n数据：{_short(data)}"
        try:
            j = parse_json(self.ask(key, sys, user))
            out = {"stance": norm_view(j.get("stance")), "confidence": clamp_conf(j.get("confidence")),
                   "summary": str(j.get("summary", ""))[:200], "evidence": [str(x)[:160] for x in (j.get("evidence") or [])][:5]}
            status, err = "ok", None
        except Exception as e:  # 降级：用规则摘要代替
            out, status, err = RULES[key](data), "degraded", f"{type(e).__name__}: {str(e)[:100]}"
        bad = []
        if status == "ok":
            out["summary"], b1 = self._flag(out["summary"])
            ev = []
            for x in out["evidence"]:
                fx, b2 = self._flag(x)
                ev.append(fx)
                bad += b2
            out["evidence"], bad = ev, bad + b1
        return self._rec(key, status=status, error=err, tools=tools_used, unverified=bad, seconds=round(time.time() - t0, 2), **out)

    # -------------------------------------------------- 多空
    def reports_text(self) -> str:
        rows = []
        for k, zh, _ in ANALYSTS:
            r = self.roles.get(k) or {}
            if r.get("status") in ("disabled",):
                continue
            rows.append({"分析师": zh, "状态": r.get("status"), "判断": r.get("stance"), "置信度": r.get("confidence"),
                         "摘要": r.get("summary"), "证据": r.get("evidence")})
        return _short(rows, 6000)

    def side(self, key, rnd, opp_prev):
        rebut = "" if not opp_prev else "，并逐条反驳对方上一轮的论点"
        sys = (BULL if key == "bull" else BEAR).format(rebut=rebut)
        user = f"标的：{self.name if self.mode != 'backtest' else '某股票'}\n第 {rnd} 轮\n分析师报告：{self.reports_text()}"
        if opp_prev:
            user += f"\n对方上一轮：{_short(opp_prev, 1500)}"
        try:
            j = parse_json(self.ask(key, sys, user))
            arg, _ = self._flag(str(j.get("argument", ""))[:300])
            reb, _ = self._flag(str(j.get("rebuttal", ""))[:300])
            pts = [self._flag(str(p)[:160])[0] for p in (j.get("points") or [])][:3]
            return {"status": "ok", "argument": arg, "points": pts, "rebuttal": reb}
        except Exception as e:
            return {"status": "degraded", "error": f"{type(e).__name__}: {str(e)[:100]}", "argument": "", "points": [], "rebuttal": ""}

    def debate(self) -> list[dict]:
        rounds, prev = [], {"bull": None, "bear": None}
        with ThreadPoolExecutor(2) as ex:
            for rnd in range(1, self.rounds + 1):
                fb = ex.submit(self.side, "bull", rnd, prev["bear"])
                fr = ex.submit(self.side, "bear", rnd, prev["bull"])
                b, r = fb.result(), fr.result()
                rounds.append({"round": rnd, "bull": b, "bear": r})
                prev = {"bull": b if b["status"] == "ok" else None, "bear": r if r["status"] == "ok" else None}
                if b["status"] != "ok" and r["status"] != "ok":
                    break  # 双方都失败（多半是预算或超时），不再继续
        st = lambda k: "ok" if all(x[k]["status"] == "ok" for x in rounds) else ("degraded" if any(x[k]["status"] == "ok" for x in rounds) else "failed")
        self._rec("bull", status=st("bull"), rounds=len(rounds))
        self._rec("bear", status=st("bear"), rounds=len(rounds))
        return rounds

    # -------------------------------------------------- 风控
    def risk(self, rounds) -> dict:
        """风控 = 红线规则（总是跑）+ 持仓规则 + LLM 风控官（只有用户给过持仓/仓位/偏好时才请）。
        否决只在命中红线（含持仓 ≥40% 的集中度红线）时出现；LLM 想否决但没有红线 → 最多降级。"""
        from .graph import aliases
        quote = self._quote
        names = aliases(self.name) if self.name != self.symbol else set()
        entries = self.user_entries if self.mode != "backtest" else []
        rule = holdings_check(entries, self.symbol, names, quote)
        rl = redline_check(quote, self.name if self.mode != "backtest" else "", self.question, entries,
                           {**(self.risk_meta or {}), "backtest": self.mode == "backtest"})
        red = list(rl["red"]) + ([f for f in rule["findings"] if "严重集中" in f] if rule["decision"] == "否决" else [])
        base = max((rule["decision"], rl["decision"]), key=STRICT.get)
        findings = rl["findings"] + [f for f in rule["findings"] if f not in rl["findings"]]
        common = {"rule_decision": base, "redlines": red, "checked": rl["checked"], "findings": findings,
                  "max_position_pct": min(rule["max_position_pct"], 10) if base != "通过" else rule["max_position_pct"], "stop_loss": rule["stop_loss"]}
        if not has_position_info(entries, self.symbol, names):
            note = "没有持仓/仓位信息，只做红线规则检查（" + "、".join(rl["checked"]) + "）" + ("，未命中红线，默认通过" if not red else "")
            out = {"status": "rules", "mode": "rules_only", "decision": base, "notes": [note], **common}
            self._rec("risk", **out)
            return out
        user = (f"标的：{self.name}\n用户持仓与偏好（USER.md）：{_short(entries, 1500)}\n"
                f"规则检查：{_short({**rule, 'decision': base, 'findings': findings, 'redlines': red})}\n多空辩论：{_short(rounds, 3000)}")
        try:
            j = parse_json(self.ask("risk", RISK, user))
            dec = j.get("decision") if j.get("decision") in STRICT else "通过"
            if dec == "否决" and not red:
                dec = "降级"  # 没有红线不许否决
            final = dec if STRICT[dec] > STRICT[base] else base
            notes = [self._flag(str(x)[:160])[0] for x in (j.get("notes") or [])][:4]
            cap = min(int(j.get("max_position_pct") or common["max_position_pct"]), common["max_position_pct"]) if str(j.get("max_position_pct", "")).strip() else common["max_position_pct"]
            stop = self._flag(str(j.get("stop_loss") or rule["stop_loss"])[:160])[0]
            out = {"status": "ok", "mode": "llm", **common, "decision": final, "notes": notes, "max_position_pct": cap, "stop_loss": stop}
        except Exception as e:
            out = {"status": "degraded", "mode": "rules_only", "error": f"{type(e).__name__}: {str(e)[:100]}", **common, "decision": base, "notes": []}
        self._rec("risk", **out)
        return out

    # -------------------------------------------------- 裁决
    def vote(self) -> dict:
        score, w = 0.0, 0.0
        for k, _, _ in ANALYSTS:
            r = self.roles.get(k) or {}
            if r.get("status") in ("ok", "degraded", "no_data") and r.get("stance"):
                c = (r.get("confidence", 50) - 50) / 45
                score += SIGN[r["stance"]] * (0.3 + c)
                w += 1
        s = score / w if w else 0
        view = "看多" if s > .15 else "看空" if s < -.15 else "中性"
        return {"view": view, "confidence": clamp_conf(50 + abs(s) * 40, hi=80), "score": round(s, 3)}

    def pm(self, rounds, risk) -> dict:
        sys = PM.format(h=self.horizon, tiers=CONF_TIERS)
        user = (f"标的：{self.name if self.mode != 'backtest' else '某股票'}\n用户问题：{self.question or '深度分析'}\n分析师报告：{self.reports_text()}\n"
                f"多空辩论：{_short(rounds, 3000)}\n风控意见：{_short({k: risk.get(k) for k in ('decision', 'findings', 'notes', 'max_position_pct', 'stop_loss')})}")
        v = self.vote()
        try:
            j = parse_json(self.ask("pm", sys, user))
            out = {"status": "ok", "view": norm_view(j.get("view")), "confidence": clamp_conf(j.get("confidence")),
                   "horizon_days": int(j.get("horizon_days") or self.horizon), "summary": self._flag(str(j.get("summary", ""))[:240])[0],
                   "key_points": [self._flag(str(x)[:200])[0] for x in (j.get("key_points") or [])][:4],
                   "invalidation": self._flag(str(j.get("invalidation", ""))[:240])[0]}
        except Exception as e:  # 降级：按分析师投票
            q = self._quote
            inv = (f"收盘跌破 60 日均线 {q['ma60']}{_c(q)}" if v["view"] == "看多" else f"收盘站上 60 日均线 {q['ma60']}{_c(q)}") if q and q.get("ma60") and v["view"] != "中性" else "出现与上述依据相反的新数据或事件时"
            pts = [f"{TITLES[k][0]}：{(self.roles.get(k) or {}).get('summary', '')}" for k, _, _ in ANALYSTS if (self.roles.get(k) or {}).get("summary")][:3]
            out = {"status": "degraded", "error": f"{type(e).__name__}: {str(e)[:100]}", "view": v["view"], "confidence": v["confidence"],
                   "horizon_days": self.horizon, "summary": "基金经理未能完成裁决，按分析师投票得出。", "key_points": pts, "invalidation": inv}
        # 风控硬约束
        dec = risk.get("decision")
        if dec == "否决" and out["view"] == "看多":
            out["view"], out["confidence"], out["risk_override"] = "中性", min(out["confidence"], 55), "风控否决：不给看多结论"
        elif dec == "降级":
            raw = out["confidence"]
            out["confidence"] = degrade_conf(raw)
            out["raw_confidence"], out["risk_override"] = raw, f"风控降级：置信度按比例向 50 收拢（{raw} → {out['confidence']}）"
            if out["confidence"] <= 50 and out["view"] != "中性":  # 收拢后已无把握：直接中性
                out["view"], out["risk_override"] = "中性", out["risk_override"] + "，已无把握，改为中性"
        out["vote"] = v
        self._rec("pm", **out)
        return out

    # -------------------------------------------------- 主流程
    def run(self, record: bool = True, save: bool = True) -> dict:
        t0 = time.time()
        self._quote = None
        # 1. 分析师并行
        with ThreadPoolExecutor(len(ANALYSTS), thread_name_prefix="analyst") as ex:
            futs = {ex.submit(self._analyst_safe, k, zh): k for k, zh, _ in ANALYSTS}
            for f in futs:
                try:
                    f.result(timeout=self.budget.role_timeout * 2 + 30)
                except Exception as e:
                    k = futs[f]
                    self._rec(k, status="degraded", error=f"{type(e).__name__}: {str(e)[:100]}", stance="中性", confidence=50,
                              summary="分析师超时，未纳入", evidence=[])
        # 2. 多空辩论
        rounds = self.debate()
        # 3. 风控
        risk = self.risk(rounds)
        # 4. 裁决
        verdict = self.pm(rounds, risk)
        call = None
        if record and self.mode == "live" and self.tools is not None:
            try:
                cv = {"看多": "看涨", "看空": "看跌", "中性": "震荡"}[verdict["view"]]
                call = self.tool("record_call", {"symbol": self.symbol, "name": self.name if self.name != self.symbol else "", "view": cv,
                                                 "horizon_days": verdict["horizon_days"], "reason": f"Agora 辩论 {self.id}：{verdict['summary'][:60]}"})
            except Exception as e:
                call = f"记账失败：{e}"
        sid = self._vsid = self.sources.new("Agora 辩论纪要 · 本次结论", "", cite.today(), kind="debate", tool="deep_analysis")
        self.sources.bind({"confidence": verdict["confidence"], "horizon": verdict["horizon_days"], "cap": risk.get("max_position_pct"),
                           "risk": risk.get("findings"), "rounds": len(rounds)}, sid)
        answer = self.compose(rounds, risk, verdict, call)
        answer = self.fix(answer)
        rec = {"id": self.id, "version": VERSION, "created": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"), "mode": self.mode,
               "symbol": self.symbol, "name": self.name, "question": self.question, "rounds_cfg": self.rounds, "horizon": self.horizon,
               "analysts": [{k: v for k, v in (self.roles.get(k) or {}).items() if not k.startswith("_")} for k, _, _ in ANALYSTS],
               "debate": rounds, "risk": {k: v for k, v in (self.roles.get("risk") or {}).items()}, "verdict": verdict,
               "record_call": call if isinstance(call, str) else None, "budget": self.budget.usage(), "seconds": round(time.time() - t0, 1),
               "answer": answer, "degraded": [TITLES[k][0] for k, r in self.roles.items() if r.get("status") in ("degraded", "failed", "timeout")]}
        rec["sources"] = [it for it in self.sources.items if it["kind"] != "user"]
        if save:
            (debates_dir() / f"{self.id}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        self.budget.close()
        return rec

    def _analyst_safe(self, k, zh):
        return self.analyst(k, zh)

    def compose(self, rounds, risk, v, call) -> str:
        nm = f"{self.name}（{self.symbol}）" if self.name != self.symbol else self.symbol
        L = [f"**Agora · {nm} 深度分析**", "",
             f"**结论：{v['view']} · 置信度 {v['confidence']}%{self._vc()}**　观察周期约 {v['horizon_days']} 个交易日"]
        if v.get("summary"):
            L.append(v["summary"])
        if v.get("risk_override"):
            L.append(f"（{v['risk_override']}）")
        if v.get("key_points"):
            L += ["", "**关键依据**"] + [f"{i}. {p}" for i, p in enumerate(v["key_points"], 1)]
        last = rounds[-1] if rounds else None
        if last:
            L += ["", f"**多空交锋**（{len(rounds)} 轮）"]
            for side, zh in (("bull", "多方"), ("bear", "空方")):
                x = last[side]
                if x.get("status") == "ok":
                    L.append(f"- {zh}：{x.get('rebuttal') or x.get('argument')}" if len(rounds) > 1 and x.get("rebuttal") else f"- {zh}：{x.get('argument')}")
                else:
                    L.append(f"- {zh}：未能发言（{x.get('error', '降级')}）")
        L += ["", f"**风控**：{risk['decision']}" + (f"——{'；'.join((risk.get('findings') or []) + (risk.get('notes') or []))}" if (risk.get('findings') or risk.get('notes')) else ""),
              f"建议单票仓位不超过 {risk['max_position_pct']}%{self._vc()}；止损参考：{risk['stop_loss']}"]
        L += ["", f"**结论失效条件**：{v.get('invalidation') or '出现与上述依据相反的新数据或事件时'}"]
        if isinstance(call, str) and call.startswith("已记账"):
            L.append(f"\n{call}")
        deg = [f"{TITLES[k][0]}（{'无数据' if r.get('status') == 'no_data' else '降级'}）" for k, r in self.roles.items()
               if r.get("status") in ("degraded", "failed", "no_data")]
        if deg:
            L.append("\n部分角色未完整参与：" + "、".join(deg) + "，相关部分已用规则摘要或跳过。")
        L += ["", "看多/看空属于择时判断，经回测不具统计显著性（未胜过动量与随机基线），仅供参考，不构成投资建议。", f"[[debate:{self.id}]]"]
        return "\n".join(L)

    def _vc(self) -> str:
        """置信度、仓位上限这些数字出自辩论本身：直接标到「辩论纪要」来源，免得被偶然撞上的行情数字认领。"""
        return f"[{self._vsid}]" if getattr(self, "_vsid", None) else ""

    def fix(self, text: str) -> str:
        """最终稿核对：有找不到出处的数字，先请修订一次（预算允许时），仍不行就标「未核实」。"""
        v = cite.verify(text, self.sources)
        self.verify_log = {"first_unverified": v["unverified"], "fixed": False}
        if v["ok"]:
            return text
        if self.budget.left() > 0:
            try:
                new = self.ask("editor", EDITOR, cite.correction_prompt(v["unverified"]) + "\n\n" + text).strip()
                if new and "[[debate:" not in new:
                    new += f"\n[[debate:{self.id}]]"
                if new and cite.verify(new, self.sources)["ok"]:
                    self.verify_log["fixed"] = True
                    self._rec("editor", status="ok", fixed=v["unverified"])
                    return new
                self._rec("editor", status="degraded", error="修订后仍有无出处数字")
            except Exception as e:
                self._rec("editor", status="degraded", error=f"{type(e).__name__}: {str(e)[:80]}")
        return cite.flag(text, self.sources)[0]


# ====================================================================== 给 agent 用
def cfg_values(cfg: dict | None = None) -> dict:
    import os
    c = dict((cfg or {}).get("debate") or {})
    return {"rounds": int(os.getenv("DEBATE_ROUNDS") or c.get("rounds", 1)), "max_calls": int(os.getenv("DEBATE_MAX_CALLS") or c.get("max_calls", 14)),
            "max_tokens": int(os.getenv("DEBATE_MAX_TOKENS") or c.get("max_tokens", 60000)),
            "role_timeout": float(os.getenv("DEBATE_ROLE_TIMEOUT") or c.get("role_timeout", 60)), "horizon": int(c.get("horizon_days", 20))}


def run_for_agent(agent, symbol: str, question: str = "", rounds: int | None = None) -> dict:
    from .agent import load_cfg
    cfg = load_cfg()
    cv = cfg_values(cfg)
    known = {w["symbol"]: w.get("name", "") for w in cfg.get("watchlist") or []}
    inv = {v: k for k, v in ALIAS.items()}
    name = known.get(symbol) or inv.get(symbol, symbol)
    d = Debate(agent.llm, symbol, name, question, tools=agent._tool, rounds=rounds or cv["rounds"],
               budget=Budget(cv["max_calls"], cv["max_tokens"], cv["role_timeout"]), user_entries=agent.mem.entries("user"),
               sources=agent.sources, horizon=cv["horizon"])
    rec = d.run()
    rec["verify"] = getattr(d, "verify_log", {})
    return rec


def listing(limit: int = 30) -> list[dict]:
    out = []
    for f in sorted(debates_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        v = j.get("verdict") or {}
        out.append({"id": j.get("id"), "created": j.get("created"), "symbol": j.get("symbol"), "name": j.get("name"),
                    "view": v.get("view"), "confidence": v.get("confidence"), "degraded": j.get("degraded"), "budget": j.get("budget")})
    return out


def load(did: str) -> dict | None:
    did = re.sub(r"[^A-Za-z0-9_.-]", "", did)
    f = debates_dir() / f"{did}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


# ====================================================================== 回测
class DebateBTAgent:
    """backtest --agent debate：每个决策点跑一场「回测版」辩论（只有截断行情，其它角色禁用）。"""

    def __init__(self, llm=None, blind: bool = True, rounds: int = 1, max_calls: int = 600, per_debate_calls: int = 8,
                 role_timeout: float = 60, cache=None, band: float = 1.5, flow: bool = False, flow_loader=None):
        if llm is None:
            from .agent import load_cfg
            from .providers import Router
            llm = Router(load_cfg().get("llm", {}))
        from .backtest import LLMCache, bt_dir
        self.llm, self.blind, self.rounds, self.max_calls, self.per = llm, blind, rounds, max_calls, per_debate_calls
        self.role_timeout, self.band = role_timeout, band
        self.model = repr(llm)
        self.cache = cache or LLMCache(bt_dir() / "llm_cache.db")
        self.stats = {"calls": 0, "debates": 0, "cache_hits": 0, "errors": 0, "skipped_cap": 0, "degraded_roles": 0,
                      "tokens_in_est": 0, "seconds": 0.0}
        self.lock = threading.Lock()
        self.seen: list = []  # 测试用：每场辩论实际拿到的行情（检查未来泄漏）
        self.flow = flow  # 回测里唯一开放的新数据：资金流历史日线，按决策日严格截断（见 backtest.BT_TOOL_POLICY）
        self.flow_loader = flow_loader
        self.flow_hist: dict = {}
        self.flow_seen: list = []  # 测试用：(决策日, 实际用到的资金流最后日期)

    def flow_for(self, snap):
        if not self.flow:
            return None
        from .backtest import bt_tool
        with self.lock:
            if snap.symbol not in self.flow_hist:
                from . import marketdata
                self.flow_hist[snap.symbol] = (self.flow_loader or (lambda s: marketdata.flow_history(s)[0]))(snap.symbol)
        fp = bt_tool("get_fund_flow", {"rows": self.flow_hist[snap.symbol]}, snap.date)
        if fp:
            self.flow_seen.append((snap.date, fp.pop("_last_date")))
            fp["_cite"] = 2
        return fp

    def key(self, snap) -> str:
        raw = f"{VERSION}|{self.model}|{snap.symbol}|{snap.date:%Y-%m-%d}|{snap.horizon}|{int(self.blind)}|{self.rounds}" + ("|flow" if self.flow else "")
        return hashlib.sha1(raw.encode()).hexdigest()

    def __call__(self, snap) -> dict | None:
        from .backtest import snapshot_payload
        k = self.key(snap)
        hit = self.cache.get(k)
        if hit:
            with self.lock:
                self.stats["cache_hits"] += 1
            return hit
        with self.lock:
            if self.stats["calls"] + self.per > self.max_calls:
                self.stats["skipped_cap"] += 1
                return None
        payload = snapshot_payload(snap, self.blind)
        payload["_cite"] = 1
        self.seen.append((snap.date, snap.closes.index.max()))
        src = cite.Sources()
        src.new("截断行情（回测）", as_of="T", tool="backtest")
        src.bind(payload, 1)
        fp = self.flow_for(snap)
        if fp:
            src.new("截断资金流（回测）", as_of="T", tool="backtest")
            src.bind(fp, 2)
        budget = Budget(self.per, 40000, self.role_timeout)
        t0 = time.time()
        try:
            d = Debate(self.llm, snap.symbol, "某股票", "", tools=None, rounds=self.rounds, budget=budget, sources=src,
                       mode="backtest", snapshot_payload=payload, horizon=snap.horizon, flow_payload=fp,
                       risk_meta={"stale_days": _stale_days(snap)})
            rec = d.run(record=False, save=False)
        except Exception as e:
            with self.lock:
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)[:120]
            return None
        v = rec["verdict"]
        with self.lock:
            self.stats["calls"] += budget.calls
            self.stats["debates"] += 1
            self.stats["tokens_in_est"] += budget.tokens
            self.stats["seconds"] += time.time() - t0
            self.stats["degraded_roles"] += sum(1 for r in d.roles.values() if r.get("status") in ("degraded", "failed"))
        out = {"view": v["view"], "confidence": v["confidence"], "reason": (v.get("summary") or "")[:80],
               "raw_confidence": v.get("raw_confidence", v["confidence"]), "risk": (rec.get("risk") or {}).get("decision")}
        if v.get("status") == "ok" or budget.calls:
            self.cache.put(k, out)
        return out

    def summary(self) -> dict:
        s = dict(self.stats)
        s["seconds"] = round(s["seconds"], 1)
        s["model"] = self.model
        s["rounds"] = self.rounds
        return s


def _stale_days(snap) -> int | None:
    try:
        import numpy as _np
        return int(_np.busday_count(snap.closes.index.max().strftime("%Y-%m-%d"), snap.date.strftime("%Y-%m-%d")))
    except Exception:
        return None


# ====================================================================== mock（无密钥时跑通管线 / 测试）
class MockRoleLLM:
    """确定性的假模型：按 [ROLE:x] 扮演各角色，结论由规则得出、数字照抄数据。只说明管线是通的。"""
    ready = True

    def __init__(self, fail: dict | None = None, delay: dict | None = None):
        self.fail, self.delay = fail or {}, delay or {}  # {role: Exception}、{role: 秒}，测降级用
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __repr__(self):
        return "mock"

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        sys = messages[0]["content"] if messages else ""
        m = re.search(r"\[ROLE:(\w+)\]", sys)
        role = m.group(1) if m else "?"
        with self.lock:
            self.calls.append(role)
        if role in self.delay:
            time.sleep(self.delay[role])
        if role in self.fail:
            raise self.fail[role]
        user = messages[-1]["content"]
        return {"role": "assistant", "content": self.reply(role, user), "tool_calls": None}

    def _data(self, user: str):
        return self._json_after(user, "数据：")

    def reply(self, role, user) -> str:
        if role in RULES:
            return json.dumps(RULES[role](self._data(user) or []), ensure_ascii=False)
        if role in ("bull", "bear"):
            want = "看多" if role == "bull" else "看空"
            reps = self._json_after(user, "分析师报告：") or []
            mine = [r for r in reps if r.get("判断") == want]
            ev = [e for r in mine for e in (r.get("证据") or [])][:2]
            if mine:
                arg = f"{'、'.join(r['分析师'] for r in mine)}偏{want[-1]}：" + "；".join((r.get("摘要") or "").rstrip("。") for r in mine)
            else:
                arg = f"没有分析师给出{want}结论，{'多方' if role == 'bull' else '空方'}论据有限。"
            reb = "对方的论据主要来自单一维度，持续性存疑。" if "对方上一轮" in user else ""
            return json.dumps({"argument": arg[:120], "points": ev, "rebuttal": reb}, ensure_ascii=False)
        if role == "risk":
            rule = self._json_after(user, "规则检查：") or {}
            return json.dumps({"decision": rule.get("decision", "通过"), "max_position_pct": rule.get("max_position_pct", 20),
                               "stop_loss": rule.get("stop_loss", "单票亏损超过 8% 止损"), "notes": []}, ensure_ascii=False)
        if role == "pm":
            reps = self._json_after(user, "分析师报告：") or []
            score, w = 0.0, 0
            for r in reps:
                if r.get("判断") in SIGN:
                    score += SIGN[r["判断"]] * (0.3 + ((r.get("置信度") or 50) - 50) / 45)
                    w += 1
            s = score / w if w else 0
            view = "看多" if s > .15 else "看空" if s < -.15 else "中性"
            h = int(re.search(r"预计 (\d+) 个交易日", user).group(1)) if re.search(r"预计 (\d+) 个交易日", user) else 20
            pts = [f"{r['分析师']}：{(r.get('证据') or [r.get('摘要')])[0]}" for r in reps if r.get("判断") == view and (r.get("证据") or r.get("摘要"))][:3] \
                or [f"{r['分析师']}：{r.get('摘要')}" for r in reps if r.get("摘要")][:3]
            tech = next((r for r in reps if r.get("分析师") == "技术面"), {})
            ma = next((e for e in tech.get("证据") or [] if "60 日均线" in e), "")
            inv = (f"若{'跌破' if view == '看多' else '站上'} {ma.split('，')[-1]}，结论失效" if ma and view != "中性" else "若出现与上述依据相反的新数据或事件，结论失效")
            return json.dumps({"view": view, "confidence": clamp_conf(50 + abs(s) * 40, hi=80), "horizon_days": h,
                               "summary": f"综合各分析师，倾向{view}。", "key_points": pts, "invalidation": inv}, ensure_ascii=False)
        if role == "editor":
            bad = re.search(r"找不到出处：(.+?)。请重写", user)
            text = user.split("\n\n", 1)[1] if "\n\n" in user else user
            for tok in (bad.group(1).split("、") if bad else []):
                text = text.replace(tok.strip(), "（数据未提供）")
            return text
        return "{}"

    @staticmethod
    def _json_after(user, tag):
        i = user.find(tag)
        if i < 0:
            return None
        try:
            return json.loads(user[i + len(tag):].split("\n")[0])
        except Exception:
            return None
