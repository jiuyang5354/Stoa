"""工具面收敛（v0.37）：每轮只把「常驻 12 个 + 按提问意图预选的域工具组」的 schema 发给模型，其余挂在 tool_search 后面。

- 第一轮之前就按提问选好工具组（规则，0 次额外模型请求）；系统提示只列域工具名（约 400 字），模型知道有哪些。
- 模型直接按名字调用没下发 schema 的工具也照常执行（执行不看 schema）；tool_search 找到的工具本轮之后各步都带上 schema。
- 上一轮用过的工具本轮继续带上（追问不丢工具）。用户接的 MCP 工具全部带上（用户自己选的）。
- 域工具的使用说明（原来全塞在系统提示里）跟着工具组走：选中哪组，哪组的说明才进本轮系统提示。
"""
from __future__ import annotations

import re

CORE_TOOLS = ("get_quotes", "get_news", "get_announcements", "get_financials", "web_search", "fetch_url",
              "calc", "memory", "write_file", "read_filing", "compare_filings", "record_call")

# 组名：(工具, 触发正则, 选中时进系统提示的使用说明)
GROUPS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "market": (("market_overview", "watchlist"),
               r"大盘|三大|指数|整体|市场怎么样|自选|watchlist|关注列表|加进|移出|market overview",
               "大盘与自选一览 → market_overview；增删自选 → watchlist。"),
    "flow": (("get_fund_flow", "get_northbound", "get_dragon_tiger", "get_chips", "get_market_mood"),
             r"资金|主力|超大单|北向|沪深港通|龙虎榜|游资|席位|两融|融资|融券|股东户数|筹码|情绪|涨停|跌停|炸板|连板|打板|板块.{0,4}(?:涨|资金)",
             "资金流/主力/超大单 → get_fund_flow（仅 A股；港股/美股资金流直接说明没有可靠免费逐日数据，不编）；北向 → get_northbound（2024-08 起不披露每日净买入）；"
             "龙虎榜 → get_dragon_tiger；两融、股东户数、筹码 → get_chips；A股情绪、涨停炸板、板块资金 → get_market_mood。"),
    "street": (("get_consensus", "get_intraday", "get_options", "get_corporate_events", "get_insiders", "get_short_volume"),
               r"目标价|评级|一致预期|盈利预测|EPS|分析师|consensus|分时|盘中|几点|分钟|intraday|期权|隐含波动|\bIV\b|put|call|最大痛点|解禁|减持|增持|回购|质押|业绩预告|调研|内部人|高管.{0,4}(?:买|卖)|insider|form 4|做空|空头|short",
               "分析师目标价/评级/盈利预测 → get_consensus；盘中/分时 → get_intraday；美股期权 IV、Put/Call、最大痛点 → get_options；"
               "解禁、减持、增持、回购、质押、业绩预告、调研、美股高管买卖 → get_corporate_events / get_insiders；美股做空 → get_short_volume。"),
    "quant": (("price_levels", "financial_bridge", "option_payoff", "trade_replay", "volume_profile"),
              r"支撑|阻力|关键价位|压力位|利润结构|瀑布|钱花在哪|收入到净利|期权组合|备兑|价差|跨式|收益曲线|止损.{0,20}目标|目标.{0,20}止损|胜算|概率.{0,6}(?:碰|到)|成交密集|成交分布|筹码峰|\bPOC\b|价值区|震荡还是趋势",
              "支撑阻力 → price_levels；利润结构 → financial_bridge；期权组合盈亏 → option_payoff；给了入场/止损/目标想知道胜算 → trade_replay（压力测试不是预测）；"
              "成交密集区/震荡还是趋势 → volume_profile（结构描述，不当买卖信号）。这些工具会在回答下方画图。"),
    "fundamental": (("get_statements", "earnings_quality", "reverse_dcf", "dcf_scenarios", "regulatory_letters"),
                    r"三表|报表|科目|资产负债表|利润表|现金流量表|现金流|资本开支|盈利质量|含金量|造假|勾稽|M-?score|F-?score|Altman|估值|DCF|折现|隐含.{0,4}增长|情景|问询函|关注函|监管|处罚|立案|SEC 意见|comment letter|valuation|intrinsic",
                    "原始三表科目 → get_statements；利润含金量/造假风险 → earnings_quality；现价隐含增长 → reverse_dcf；三情景估值 → dcf_scenarios（情景假设写清楚）；问询函/监管/处罚 → regulatory_letters。"),
    "sec": (("sec_filings", "sec_document", "sec_xbrl", "sec_search", "earnings_release", "earnings_transcript", "price_history"),
            r"\bSEC\b|EDGAR|10-?K|10-?Q|8-?K|20-?F|DEF 14A|proxy|S-4|filing|XBRL|press release|新闻稿|earnings call|transcript|电话会|guidance|指引|fiscal|FY ?20\d\d|Q[1-4] ?20\d\d|"
            r"\b(?:revenue|EBITDA|EBIT|margin|net income|operating income|free cash flow|diluted|shares outstanding|backlog|same[- ]store|segment|acquisition|merger|tender offer|buyback|dividend|capex|guidance|basis points)\b",
            "美股 SEC 原文（点名某份 10-K/10-Q/8-K/新闻稿/封面股数，或要精确科目时优先这组，不拿网页转述或 Yahoo 汇总数代替）：sec_filings 找申报；sec_document 读原文（cover 封面股数、mdna、risk、financial，query 检索表格）；"
            "sec_xbrl 取精确科目值；sec_search 全文检索；earnings_release 读业绩新闻稿与指引；earnings_transcript 读电话会。退市/改名公司 symbol 填公司名或 CIK。"),
    "history": (("price_history", "cn_data", "macro_series", "run_python"),
                r"历史|当时|那天|某日|收盘价|开盘价|最高价|最低价|区间|期间|之间|以来|最大(?:单周|单日|回撤)|单周|周涨跌|连续.{0,6}(?:涨|跌)|多少天|几天|20[0-2]\d ?年|\b(?:19|20)\d\d[-/年.]\d{1,2}|"
                r"统计|年鉴|国家统计局|统计局|宏观|CPI|PPI|GDP|PMI|M2|社零|工业增加值|固投|LPR|利率|汇率|外储|经常账户|就业|工资|房价|金价|上海金|黄金|申万|行业指数|中证|交易所|上市公司.{0,6}(?:家|总|数)|总市值|退市|ETF|基金净值|期权|"
                r"\bFRED\b|vintage|federal funds|bank credit|seasonally|index\b|historical|closing price|opening price|all[- ]time|between .{3,30} and|since 20\d\d|in 20\d\d|as of",
                "历史/统计查数：某日收盘、区间涨跌、公告后 N 天股价 → price_history（非交易日按题意取前/后一交易日并说明；不要因为 get_quotes 只有近期数据就说查不到；full=true 给完整逐日序列 + csv）；中国宏观、A股财务筛选、中证/申万指数、交易所统计、ETF 期权、国家统计局、上海金 → cn_data；"
                "海外宏观 → macro_series（指定数据版本给 vintage）；序列计算（连涨天数、回撤、计数）→ run_python 读返回的 csv。查数题要落到一个具体答案，"
                "结构化工具 → web_search 换 3 种问法 → fetch_url 打开原页核对；仍无精确值给有出处的最佳估计并写口径。"),
    "memory": (("memory_links", "session_search", "search_memory", "call_stats"),
               r"上次|之前|以前|记得|说过|定的|我的(?:持仓|成本|风格|偏好)|记忆|画像|离场价|账本|战绩|准确率|胜率|复盘|判断.{0,4}(?:对|错)",
               "「上次/之前说过」→ session_search；找某条记忆、过去的判断理由或辩论结论 → search_memory；分析某只股票前把相关记忆一次取齐 → memory_links；判断账本统计 → call_stats。"),
    "schedule": (("task_create", "task_list", "task_delete", "alert_set", "alert_list", "alert_delete", "pending_actions", "confirm_action"),
                 r"提醒|每天|每周|每个工作日|每隔|定时|到点|明天.{0,6}点|alert|跌破|涨到|突破.{0,6}告诉|通知我|推送|任务|待确认|确认|批准|同意|拒绝",
                 "价格/涨跌/公告提醒 → alert_set（说明触发后推到哪里）；定时执行任意指令 → task_create（prompt 自包含、北京时间）；查看/取消 → task_list / alert_list / task_delete / alert_delete；"
                 "待确认动作 → pending_actions，用户本轮明确同意/拒绝才 confirm_action。"),
    "deep": (("deep_analysis", "earnings_calendar", "earnings_report"),
             r"深度分析|该不该(?:买|卖)|能不能买|值不值得买|多空|辩论|Agora|财报日|什么时候出财报|财报季|财报.{0,6}(?:复盘|研报|出来了)|业绩.{0,4}(?:超|不及)预期",
             "深度分析/该不该买卖 → deep_analysis（如实转述，先查证要点和风险，方向放后面）；财报日历 → earnings_calendar；财报出来后对照持仓逻辑 → earnings_report。"),
    "research": (("start_inquiry", "inquiry_status", "inquiry_resume"),
                 r"长期研究|持续研究|课题|研究.{0,4}(?:小时|半小时|久一点)|继续研究|接着研究|怎么停了|inquiry",
                 "长程研究 → start_inquiry（后台跑，告诉用户课题号和预算）；进度 → inquiry_status；「继续/怎么停了」→ 先 inquiry_status 再 inquiry_resume。普通对话一轮只能查几分钟，研究半小时以上直接开课题。"),
    "skills": (("skill_view", "skill_save", "skill_delete"),
               r"技能|skill|流程|步骤清单|存成.{0,4}(?:方法|做法)",
               "技能：同类任务先 skill_view；多步骤做法用 skill_save 存下来。"),
    "code": (("run_python",),
             r"代码|python|pandas|回归|相关系数|统计检验|蒙特卡洛|模拟|批量计算|整表|附件|csv|xlsx|excel|<<<附件",
             "表格/序列计算 → run_python（结果开头注明本机受限模式时如实告诉用户）。"),
    "deck": (("sweep_filings", "financial_bridge"),
             r"(?<![A-Za-z])pptx?(?![A-Za-z])|幻灯|演示文稿|投研(?:报告|简报|材料)|路演|(?<![A-Za-z])(?:slides?|deck)(?![A-Za-z])|keynote|做成.{0,4}(?:报告|简报)",
             "投研 PPT → write_file(format=\"pptx\", deck={symbol,name,date,title, conclusion:{view,confidence,horizon_days,reasons[3]}, "
             "price:{metrics:[[名称,值,注]],history:[[日期,收盘]],source}, financials:{columns,rows,source}（三年关键项+增速）, "
             "waterfall:{items:[[科目,金额]]（首项营收、中间成本/费用为负、末项净利，用 financial_bridge 的数）,period}, debate:{bull[],bear[],verdict}（deep_analysis 的产物）, "
             "risks:[{level,text}], peers:{columns,rows,coverage}（sweep_filings 矩阵原样放）, invalidation:{conditions[],review_date}, sources[]})。"
             "数字只填本轮工具取到或 calc 算出的；结论/辩论/风险/失效条件/复盘日期没给时会从本机该标的最近的 Agora 纪要与账本补，仍缺的页写「未提供」，不要编。"),
    "batch": (("sweep_filings",),
              r"(?:多家|几家|\d+ ?家|各家|这些公司|同行|横向|批量).{0,30}(?:年报|季报|定期报告|MD&A|管理层讨论|风险因素|10-?K)|(?:年报|MD&A|10-?K).{0,20}(?:多家|几家|横向|矩阵|对比表)|sweep",
              "多家公司 × 多年 × 同一章节的横向对比 → sweep_filings（一轮出矩阵，可直接 write_file 成 xlsx），不要逐份 read_filing。"),
}

# 系统提示里的域工具名清单（只列名字，不列 schema）：模型知道有这些，按名字调用或先 tool_search
DOMAIN_INDEX = (
    "## 域工具（本轮没下发 schema 的也可以直接按名字调用，或先 tool_search 看参数）\n"
    "大盘自选 market_overview watchlist｜资金筹码 get_fund_flow get_northbound get_dragon_tiger get_chips get_market_mood｜"
    "预期事件 get_consensus get_intraday get_options get_corporate_events get_insiders get_short_volume｜"
    "计算画图 price_levels financial_bridge option_payoff trade_replay volume_profile｜基本面 get_statements earnings_quality reverse_dcf dcf_scenarios regulatory_letters｜"
    "SEC sec_filings sec_document sec_xbrl sec_search earnings_release earnings_transcript｜历史宏观 price_history cn_data macro_series run_python｜"
    "批量文档 sweep_filings｜记忆 memory_links session_search search_memory call_stats｜定时提醒 task_create task_list task_delete alert_set alert_list alert_delete pending_actions confirm_action｜"
    "深度 deep_analysis earnings_calendar earnings_report｜研究课题 start_inquiry inquiry_status inquiry_resume｜技能 skill_view skill_save skill_delete"
)

TOOL_SEARCH = {"name": "tool_search",
               "description": "按关键词找域工具（龙虎榜/北向/期权/宏观/SEC/定时任务等），返回名字、说明、参数，本轮即可调用。",
               "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}

_COMPILED = {g: re.compile(rx, re.I) for g, (_, rx, _) in GROUPS.items()}


def intent_groups(text: str) -> list[str]:
    t = text or ""
    return [g for g, rx in _COMPILED.items() if rx.search(t)]


def mode() -> str:
    import os
    return (os.getenv("TOOL_SELECT") or "auto").lower()


def select(text: str, pool: list[dict], carry=(), unlocked=()) -> tuple[list[dict], list[str]]:
    """返回 (本轮下发的工具 schema, 命中的组)。pool 是本会话可用的全部工具（内置 + MCP）。
    TOOL_SELECT=all 时全量下发（旧行为，排查用）。"""
    names = {t["name"] for t in pool}
    if mode() in ("all", "off", "0", "false"):
        return list(pool), ["all"]
    groups = intent_groups(text)
    want = set(CORE_TOOLS)
    for g in groups:
        want |= set(GROUPS[g][0])
    low = (text or "").lower()
    want |= {n for n in names if len(n) > 5 and n.lower() in low}  # 提问里直接点名工具
    want |= set(carry) | set(unlocked)
    want |= {n for n in names if n.startswith("mcp__")}
    picked = [t for t in pool if t["name"] in want]
    if "tool_search" not in want:
        picked.append(TOOL_SEARCH)
    return picked, groups


def notes(groups: list[str]) -> str:
    lines = [GROUPS[g][2] for g in groups if g in GROUPS]
    return ("\n## 本轮相关工具的用法\n" + "\n".join(f"- {x}" for x in lines)) if lines else ""


def search(query: str, pool: list[dict], limit: int = 5, exclude=()) -> list[dict]:
    """关键词（名字/说明/组触发词，BM25）+ 本地向量（vectors.local_tf 余弦）融合排序。"""
    from . import vectors
    import numpy as np
    from collections import Counter
    cand = [t for t in pool if t["name"] not in set(exclude) and t["name"] != "tool_search"]
    if not cand or not (query or "").strip():
        return []
    group_of = {n: g for g, (tools, _, _) in GROUPS.items() for n in tools}
    docs = []
    for t in cand:
        g = group_of.get(t["name"])
        extra = (GROUPS[g][1].replace("|", " ").replace("\\b", " ") + " " + GROUPS[g][2]) if g else ""
        docs.append(f"{t['name'].replace('_', ' ')} {t['name']} {t.get('description', '')} {extra}")
    toks = [Counter(vectors.tokens(d, unigrams=False)) for d in docs]
    kw = vectors.bm25(query, toks)
    direct = np.array([3.0 if t["name"].lower() in query.lower() else 0.0 for t in cand])
    try:
        qv = vectors.local_tf(query)
        dv = np.stack([vectors.local_tf(d) for d in docs])
        cos = dv @ qv / ((np.linalg.norm(dv, axis=1) * (np.linalg.norm(qv) or 1)) + 1e-9)
    except Exception:
        cos = np.zeros(len(cand))
    rk = vectors.rrf([list(np.argsort(-kw)), list(np.argsort(-cos))])
    score = {i: rk.get(i, 0) + direct[i] + (0.02 if kw[i] > 0 else 0) for i in range(len(cand))}
    order = sorted(score, key=lambda i: -score[i])
    return [cand[i] for i in order if kw[i] > 0 or cos[i] > 0.05 or direct[i]][:max(1, min(10, limit))]
