"""快问回答的事后检查（规则，不调模型）：
  1. 回答里给了方向判断（看多/看空/可以买/目标价…）却没写失效条件 → 按本轮行情数据补一句「失效条件」（价格优先，没有数据就写事件条件）。
  2. 提问里有「全仓 / 满仓 / 借钱 / 加杠杆 / 保证涨」这类诱导，而回答没点出对应风险（集中度 / 杠杆 / 回撤）→ 补一句风险提示。

只在回答末尾追加，不改正文、不碰交易或风控规则；补写的价格都取自本轮工具数据，finalize 会给它加角标。
Agora 深度分析有自己的失效条件与风险清单，不走这里。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 方向判断
BULL = re.compile(
    r"看多|看涨|偏多|偏强|做多|(?<!不)(?:可以|能|值得)(?:考虑)?(?:买|买入|入手|加仓|建仓|逢低|布局)|(?<!不)建议(?:买入|加仓|建仓|逢低)|逢低(?:买|吸纳|布局)|上涨空间|有望(?:上涨|走高|突破)|"
    r"\b(?:bullish|buy|overweight|upside)\b", re.I)
BEAR = re.compile(
    r"看空|看跌|偏空|偏弱|做空|(?:建议|可以|考虑)(?:卖出|减仓|离场|止盈)|不建议(?:买|买入|追高|加仓)|(?:先|暂时)?不要(?:买|追)|下行空间|有望(?:下跌|走低)|"
    r"\b(?:bearish|sell|underweight|downside)\b", re.I)
SIDE = re.compile(r"中性|观望|震荡|区间(?:内)?波动|\b(?:neutral|range-bound)\b", re.I)
OWN_TARGET = re.compile(r"(?<!分析师)(?<!一致预期)(?<!机构)(?<!券商)(?<!研报)(?<!平均)目标(?:价|位)(?:看|上看|下看|在|到|为|是)?\s*\d")
# 不是在下判断：转述别人、讲规则、拒绝
NOT_MINE = re.compile(r"分析师|一致预期|机构|券商|研报|评级|"  # 转述别人的观点
                      r"你的(?:逻辑|判断|观点|看法)|持仓逻辑|之前|上次|以前|当时|记录|账本|判断：|历史|回顾|复盘|"  # 回顾过去的判断、用户自己的逻辑
                      r"\b(?:analysts?|consensus|previously|last time)\b", re.I)
INVALID = re.compile(r"失效|作废|证伪|止损|判断(?:不再|就不)成立|\b(?:invalidat|stop[- ]loss)", re.I)
INVALID_WEAK = re.compile(r"(?:若|如果|一旦|假如).{0,24}(?:跌破|站上|突破|失守|收于|低于|高于).{0,30}(?:则|就|应|需|要|重新)")


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[。！？!?\n；;])", text) if x.strip()]


def direction(text: str) -> int | None:
    """回答自己给出的方向：1 看多、-1 看空、0 中性/震荡；没有方向判断返回 None。转述分析师评级、目标价不算。"""
    bull = bear = side = 0
    for s in _sentences(text):
        if s.lstrip().startswith(("注", "择时判断", "仅供参考")):
            continue
        mine = not NOT_MINE.search(s)
        b, r = len(BULL.findall(s)), len(BEAR.findall(s))
        if mine or OWN_TARGET.search(s):
            bull += b + (1 if OWN_TARGET.search(s) and not r else 0)
            bear += r
            side += len(SIDE.findall(s)) if mine else 0
    if not (bull or bear or side):
        return None
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def has_invalidation(text: str) -> bool:
    return bool(INVALID.search(text) or INVALID_WEAK.search(text))


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:.1f}".rstrip("0").rstrip(".")


def _levels(q: dict, d: int) -> list[tuple[str, float]]:
    """看多找现价下方最近的支撑，看空找上方最近的阻力；震荡两边各一个。"""
    close = q.get("close")
    if not isinstance(close, (int, float)) or close <= 0:
        return []
    names = {"ma20": ("20 日均线", "the 20-day average"), "ma60": ("60 日均线", "the 60-day average"),
             "low_52w": ("52 周低点", "the 52-week low"), "high_52w": ("52 周高点", "the 52-week high")}
    below = sorted(((k, q[k]) for k in ("ma20", "ma60", "low_52w") if isinstance(q.get(k), (int, float)) and q[k] < close * .995), key=lambda x: -x[1])
    above = sorted(((k, q[k]) for k in ("ma20", "ma60", "high_52w") if isinstance(q.get(k), (int, float)) and q[k] > close * 1.005), key=lambda x: x[1])
    out = []
    if d >= 0 and below:
        out.append(("down",) + below[0])
    if d <= 0 and above:
        out.append(("up",) + above[0])
    return [(side, names[k], v) for side, k, v in out]


def invalidation_line(d: int, quote: dict | None, symbol: str = "", en: bool = False) -> str:
    view_zh = {1: "看多", -1: "看空", 0: "震荡"}[d]
    lv = _levels(quote or {}, d)
    who = f"{symbol} " if symbol else ""
    if en:
        view = {1: "bullish", -1: "bearish", 0: "range"}[d]
        if lv:
            parts = [f"closes {'below' if s == 'down' else 'above'} {n[1]} {_fmt(v)}" for s, n, v in lv]
            return f"Invalidation: this {view} view no longer holds if {who}{' or '.join(parts)}; reassess then, as the risk on the other side grows."
        return (f"Invalidation: this {view} view no longer holds if the next earnings or announcements contradict the reasons above, "
                f"or if the price breaks the recent support/resistance against it; reassess then.")
    if lv:
        parts = [f"收盘{'跌破' if s == 'down' else '站上'} {n[0]} {_fmt(v)}" for s, n, v in lv]
        tail = "反向风险随之加大，应重新评估" if d else "说明区间被打破，应重新评估方向"
        return f"失效条件：若 {who}" + "或".join(parts) + f"，这一{view_zh}判断作废，{tail}。"
    return (f"失效条件：若后续财报、公告与上面的依据相反，或股价跌破/站上近期支撑与阻力、走势与{view_zh}判断相反，这一判断作废，"
            "应重新评估，相应的回撤风险也会变化。")


# ---------------------------------------------------------------- 诱导题的风险
LURES = {
    "concentration": re.compile(r"全仓|满仓|梭哈|重仓一只|all[\s-]*in|(?:全部|所有)(?:资金|的钱|积蓄|存款)(?:都)?(?:买|投|压|放)|押上全部", re.I),
    "leverage": re.compile(r"借钱|借款|贷款|网贷|信用卡套?现?|加杠杆|上杠杆|融资(?:买入?|炒股|加仓)|配资|(?:\d+|两|三|五|十)\s*倍杠杆|杠杆(?:买|炒|做多|加仓)|\bmargin\b|\bleverage\b|borrow", re.I),
    "guarantee": re.compile(r"保证(?:能|会)?(?:涨|赚|盈利|收益|翻倍|不亏|回本)|稳赚|包赚|必涨|一定(?:会|能)?涨|稳赚不赔|只涨不跌|\bguarantee", re.I),
}
NEG_LURE = re.compile(r"(?:不想|不会|不打算|不要|别|没有|不敢)(?:.{0,4})(?:全仓|满仓|借钱|加杠杆|配资)")
COVERED = {
    "concentration": re.compile(r"集中|分散|单一(?:标的|股票|个股)?|一只股票|鸡蛋|仓位(?:过|太)?(?:重|高)|\bconcentrat|\bdiversif", re.I),
    "leverage": re.compile(r"杠杆.{0,12}(?:放大|风险|亏)|爆仓|强平|强制平仓|追加保证金|保证金|利息|亏损.{0,6}超过本金|\bmargin call|\bliquidat", re.I),
    "guarantee": re.compile(r"(?:不能|无法|没有人能|没人能|没法|不可能|不存在|谁也不能)(?:够)?(?:保证|确定|承诺)|\b(?:no one can|cannot|can't) guarantee", re.I),
}
DRAWDOWN = re.compile(r"回撤|亏损|本金|下跌风险|\bdrawdown|\blos[se]", re.I)
RISK_LINES = {
    "concentration": ("风险提示：全仓押在单一标的上没有分散，一次财报不及预期、行业利空或停牌就可能带来大幅回撤，且没有余钱应对或补救；"
                      "仓位要和能承受的亏损相匹配。",
                      "Risk: putting everything into one stock leaves no diversification. A single earnings miss, sector shock or halt can cause a deep drawdown "
                      "with no cash left to respond; size the position to the loss you can bear."),
    "leverage": ("风险提示：借钱或加杠杆会同比例放大亏损，下跌时可能被追加保证金或强制平仓，还要付利息，亏损可能超过本金。",
                 "Risk: borrowing or leverage magnifies losses in proportion; a drop can trigger margin calls or forced liquidation, interest keeps accruing, "
                 "and losses can exceed your capital."),
    "guarantee": ("风险提示：没有人能保证股票上涨或收益，任何个股都可能出现较大回撤、亏损本金。",
                  "Risk: no one can guarantee a stock will rise or any return; any single stock can suffer a large drawdown and lose capital."),
}


def lures(question: str) -> list[str]:
    q = question or ""
    if NEG_LURE.search(q):
        q = NEG_LURE.sub("", q)
    return [k for k, rx in LURES.items() if rx.search(q)]


DRAWDOWN_LINE = ("风险提示：任何个股都可能出现较大回撤、亏损本金，仓位要和能承受的亏损相匹配。",
                 "Risk: any single stock can suffer a large drawdown and lose capital; size the position to the loss you can bear.")


def missing_risks(question: str, reply: str) -> list[str]:
    """诱导题缺的风险点：对应类别（集中度/杠杆/不保证）没提到就补那一类（每类提示自带回撤）；
    类别都提到了、只是没说回撤/亏损，补一句 drawdown。"""
    kinds = lures(question)
    out = [k for k in kinds if not COVERED[k].search(reply)]
    if kinds and not out and not DRAWDOWN.search(reply):
        out.append("drawdown")
    return out


# ---------------------------------------------------------------- 入口
def _is_en(text: str) -> bool:
    return not re.search(r"[\u4e00-\u9fff]", text or "")


def _pick_quote(text: str, sources) -> tuple[str, dict | None]:
    """判断说的是哪只：回答里最后提到的已登记标的；只查了一只就是它。"""
    if sources is None:
        return "", None
    with sources.lock:
        qs = list(sources.quotes)
    if not qs:
        return "", None
    syms = {q["symbol"] for q in qs}
    if len(syms) == 1:
        return qs[-1]["symbol"], qs[-1]["data"]
    best = None
    for q in qs:
        for a in q["aliases"]:
            i = text.rfind(a)
            if i >= 0 and (best is None or i > best[0]):
                best = (i, q)
    return (best[1]["symbol"], best[1]["data"]) if best else ("", None)


def review(question: str, reply: str, sources=None) -> tuple[str, dict]:
    """返回 (补写后的回答, 补写说明 {invalidation: bool, risks: [...]})。正文末尾的来源清单、[[…]] 标记之前插入。"""
    from .cite import split_sources
    body, tail = split_sources(reply or "")
    if not body.strip():
        return reply, {}
    en = _is_en(body)
    add, note = [], {}
    d = direction(body)
    if d is not None and not has_invalidation(body):
        sym, q = _pick_quote(body, sources)
        add.append(invalidation_line(d, q, sym if q else "", en))
        note["invalidation"] = True
    miss = missing_risks(question, body)
    if miss:
        add += [(DRAWDOWN_LINE if k == "drawdown" else RISK_LINES[k])[1 if en else 0] for k in miss]
        note["risks"] = miss
    if not add:
        return reply, {}
    m = re.search(r"\n*((?:\[\[[^\]]*\]\]\s*)+)$", body)  # [[debate:…]] / [[cards:…]] 之类的标记留在最后
    marks = m.group(1) if m else ""
    main = body[:m.start()] if m else body
    disc = re.search(r"\n*[^\n]*(?:仅供参考|不构成投资建议|not investment advice)[^\n]*\s*$", main, re.I)  # 免责句保持在最后一行
    if disc:
        main, last = main[:disc.start()], main[disc.start():]
    else:
        last = ""
    out = main.rstrip() + "\n\n" + "\n".join(add) + (("\n\n" + last.strip()) if last else "") + (("\n" + marks.strip()) if marks else "")
    return out + (("\n\n" + tail) if tail else ""), note
