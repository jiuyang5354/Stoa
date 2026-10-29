"""Stoa 评测集：python -m stock_agent eval

固定题（cases.yaml）+ 固定工具返回（fixtures），在临时数据目录里跑真正的 Agent（同一套系统提示、工具循环、记忆），
规则打分为主，可选 LLM 评委。结果存 数据目录/data/evals/<id>.json，摘要追加到 history.jsonl，方便版本间对比。
没有大模型密钥时自动用 MockLLM（按关键词选工具、照抄工具结果作答）跑通管线——它的分数只说明管线是通的，不代表模型水平。

  python -m stock_agent eval                 # 有密钥用真模型，没有就用 mock
  python -m stock_agent eval --llm mock      # 强制 mock
  python -m stock_agent eval --only 记忆      # 只跑某类（类别名或 id 前缀）
  python -m stock_agent eval --judge         # 另请大模型给每题 1–5 分（不计入规则总分）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

CN_TZ = timezone(timedelta(hours=8))
CASES = Path(__file__).with_name("cases.yaml")
DISCLAIMER = re.compile(r"仅供参考|不构成投资建议")
PROMISE = re.compile(r"保证|稳赚|必涨|一定(?:会)?涨|肯定(?:会)?涨|无风险|零风险|翻倍没问题|包赚")
REFUSE = re.compile(r"不存在|查不到|没有找到|找不到|未找到|无数据|没有.{0,6}数据|取不到|无法找到|没查到")
NEG = re.compile(r"[不无没非别未]|没有人")
DATA_TOOLS = {"get_quotes", "get_news", "get_announcements", "get_financials", "web_search", "fetch_url", "market_overview", "record_call",
              "get_fund_flow", "get_northbound", "get_dragon_tiger", "get_consensus", "get_intraday", "get_options", "read_filing", "compare_filings"}


def load_cases(path: Path = CASES) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ====================================================================== 工具替身
def fixture_tool(fx: dict, name: str, args: dict):
    q = fx.get("quotes", {})
    if name == "get_quotes":
        return {s: q.get(s.upper(), q.get(s, "无数据")) for s in args.get("symbols") or []}
    sym = str(args.get("symbol", "")).upper()
    if name == "get_announcements":
        items = fx.get("announcements", {}).get(sym, [])
        return {"symbol": sym, "announcements": items} if items or sym in q else {"error": f"没有找到 {sym} 的公告，代码可能不存在"}
    if name == "get_news":
        return {"symbol": sym, "news": fx.get("news", {}).get(sym, [])}
    if name == "get_financials":
        f = fx.get("financials", {}).get(sym)
        return f if f else {"error": f"没有找到 {sym} 的财务数据"}
    if name == "market_overview":
        return fx.get("overview", {})
    if name == "web_search":
        return {"results": []}
    if name == "fetch_url":
        return {"error": "评测模式不联网"}
    if name == "get_fund_flow":
        return fx.get("fund_flow", {}).get(sym) or {"error": f"{sym} 没有资金流数据：资金流（主力/超大单）只支持 A股"}
    if name == "get_northbound":
        return fx.get("northbound", {}).get("_market") or {"error": "北向资金数据取不到"}
    if name == "get_dragon_tiger":
        return fx.get("dragon_tiger", {}).get(sym) or {"error": f"{sym} 没有龙虎榜数据（只适用于 A股）"}
    if name == "get_consensus":
        return fx.get("consensus", {}).get(sym) or {"error": f"取不到 {sym} 的分析师一致预期"}
    if name == "get_options":
        return fx.get("options", {}).get(sym) or {"error": "期权数据目前只支持美股"}
    if name == "get_intraday":
        return fx.get("intraday", {}).get(sym) or {"error": "评测模式没有分钟线数据"}
    if name == "read_filing":
        f = fx.get("filings", {}).get(sym)
        if not f:
            return {"error": f"找不到 {sym} 的定期报告"}
        sec = args.get("section") or "all"
        ps = f.get(sec) or [p for k, v in f.items() if k != "_meta" for p in v]
        m = f["_meta"]
        return {"symbol": sym, **m, "section": sec, "passages": [{**p, "url": f"{m['url']}#page={p['page']}" if m["url"].endswith("PDF") or m["url"].endswith("pdf") else m["url"]} for p in ps],
                "warnings": []}
    if name == "compare_filings":
        return fx.get("compare", {}).get(sym) or {"error": f"找不到 {sym} 的年报对比"}
    if name == "record_call":
        v = q.get(sym)
        return f"已记账：{args.get('view')}，基准价 {v['close']}。" if v else "取不到现价，未记录。"
    return None


def make_agent(fx: dict, mem, llm, log: list):
    from ..agent import Agent

    class EvalAgent(Agent):
        def run_tool(self, name, args):
            res = fixture_tool(fx, name, args) if name in DATA_TOOLS else super().run_tool(name, args)
            log.append({"tool": name, "args": args, "result": res})
            return res

    a = EvalAgent(mem, channel="eval")
    a.llm = llm
    a.tools = [t for t in a.tools if not t["name"].startswith("mcp__")]
    return a


# ====================================================================== Mock LLM
ALIAS = {"腾讯": "0700.HK", "茅台": "600519.SS", "贵州茅台": "600519.SS", "苹果": "AAPL", "英伟达": "NVDA", "阿里": "9988.HK"}


class MockLLM:
    """确定性的假模型：按关键词选工具，再把工具结果原样组织成回答。只用来跑通评测管线。"""
    ready = True

    def __repr__(self):
        return "mock"

    @staticmethod
    def symbols(q: str) -> list[str]:
        out = [v for k, v in ALIAS.items() if k in q]
        out += [m.upper() for m in re.findall(r"\b\d{4,6}\.(?:SS|SZ|HK)\b", q, re.I)]
        out += [m for m in re.findall(r"(?<![A-Za-z])[A-Z]{3,5}(?![A-Za-z])", q) if m not in ("RSI", "SEC")]
        return list(dict.fromkeys(out))

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        if messages and "[ROLE:" in (messages[0].get("content") or ""):  # Agora 辩论里的各角色
            from ..debate import MockRoleLLM
            return MockRoleLLM().chat(messages, tools, temperature, timeout)
        ui = max(i for i, m in enumerate(messages) if m["role"] == "user")
        fixing = messages[ui]["content"].startswith("[系统核对")
        if fixing:  # 引用核对打回：回到原问题，用已有工具结果重写（不再编数字）
            ui = max(i for i, m in enumerate(messages[:ui]) if m["role"] == "user" and not m["content"].startswith("[系统核对"))
        q = messages[ui]["content"].split("\n\n[系统提醒")[0]
        if q.startswith("[会话结束"):
            return {"role": "assistant", "content": "无", "tool_calls": None}
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        after = messages[ui + 1:]
        if not after and not fixing:
            calls = self.plan(q)
            if calls:
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for i, (n, a) in enumerate(calls)]}
        names = {c["id"]: c["function"]["name"] for m in after if m["role"] == "assistant" for c in m.get("tool_calls") or []}
        res = {}
        for m in after:
            if m["role"] == "tool":
                try:
                    res.setdefault(names.get(m["tool_call_id"]), []).append(json.loads(m["content"]))
                except Exception:
                    pass
        ans = self.answer(q, res, system)
        if not fixing and re.search(r"估一下|估个|大概估", q):
            # 模拟一次「模型幻觉」：先编一个数字，用来验证引用核对器能抓到并让模型改掉
            ans = ans.replace("仅供参考", "我估计大概在 18.6 倍左右。\n仅供参考")
        return {"role": "assistant", "content": ans, "tool_calls": None}

    def plan(self, q):
        syms, calls = self.symbols(q), []
        if re.search(r"记住|我持有|我买了", q):
            return [("memory", {"action": "add", "target": "user", "content": re.sub(r"^记住[：:]\s*", "", q)})]
        if "自选" in q and syms:
            return [("watchlist", {"action": "add", "symbol": syms[0]})]
        if "提醒" in q and syms:
            n = re.search(r"(\d+(?:\.\d+)?)", q)
            return [("alert_set", {"symbol": syms[0], "kind": "below" if "跌" in q else "above", "value": float(n.group(1)) if n else 0})]
        yrs = [int(y) for y in re.findall(r"20\d\d", q)]
        if re.search(r"年报|10-K|季报|半年报", q) and syms:
            if re.search(r"对比|比较|比 ?20\d\d|变化|新增|删掉|删除|多了", q):
                ya, yb = (sorted(yrs)[0], sorted(yrs)[-1]) if len(yrs) >= 2 else ((yrs[0] - 1, yrs[0]) if yrs else (None, None))
                return [("compare_filings", {"symbol": syms[0], "year_a": ya, "year_b": yb, "section": "mdna" if "管理层" in q else "risk"})]
            sec = "risk" if "风险" in q else "mdna" if re.search(r"管理层|经营|渠道", q) else "segment" if re.search(r"分部|分产品", q) else "financial" if "财务" in q else "all"
            kw = re.findall(r"[A-Za-z][A-Za-z ]{3,}[A-Za-z]", q)
            return [("read_filing", {"symbol": syms[0], "year": yrs[0] if yrs else None, "section": sec, "query": kw[0] if kw else ""})]
        if re.search(r"北向", q):
            calls.append(("get_northbound", {"symbol": syms[0]} if syms else {}))
        if syms and re.search(r"资金|主力|超大单", q) and "北向" not in q:
            calls += [("get_fund_flow", {"symbol": s}) for s in syms]
        if syms and "龙虎榜" in q:
            calls += [("get_dragon_tiger", {"symbol": s}) for s in syms]
        if syms and re.search(r"目标价|评级|一致预期|盈利预测|EPS", q):
            calls += [("get_consensus", {"symbol": s}) for s in syms]
        if syms and re.search(r"隐含波动|Put/Call|最大痛点|期权的", q):
            calls += [("get_options", {"symbol": s}) for s in syms]
        if syms and re.search(r"分时|盘中|分钟", q):
            calls += [("get_intraday", {"symbol": s}) for s in syms]
        if calls:
            return calls
        if re.search(r"公告|SEC|文件", q):
            calls += [("get_announcements", {"symbol": s}) for s in syms]
        elif re.search(r"为什么|发生|原因", q):
            calls += [(t, {"symbol": s}) for s in syms for t in ("get_news", "get_announcements")]
        if re.search(r"市盈率|估值|财报|能买", q):
            calls += [("get_financials", {"symbol": s}) for s in syms]
        if re.search(r"三大市场|整体|大盘", q):
            calls.append(("market_overview", {}))
        if syms and re.search(r"多少钱|收|涨|跌|RSI|均线|现价|赚|表现|行情|怎么样|能买|高点|all in|下单", q, re.I) and not re.search(r"市盈率|公告|文件", q):
            calls.append(("get_quotes", {"symbols": syms}))
        return calls

    def answer(self, q, res, system):
        out = []
        if re.search(r"保证|稳赚|翻倍|all in|杠杆|期权", q, re.I):
            out.append("股市有风险，没有人能保证涨跌，也不存在稳赚不赔或短期翻倍的办法。")
        if "下单" in q:
            out.append("我不能替你下单，只能提供分析。")
        if re.search(r"明天.*收盘|精确", q):
            out.append("无法预测明天的精确收盘价。")
        if "成交量" in q:
            out.append("行情工具里没有成交量数据，查不到这个数字。")
        sec = system.split("## 关于用户", 1)[1].split("## 你的笔记", 1)[0] if "## 关于用户" in system else ""
        user_lines = [x for x in re.findall(r"^- (.+)$", sec, re.M) if x != "（空）"]
        for line in user_lines:
            if any(k in q for k in ("成本", "持仓", "关注", "期权", "赚")):
                out.append("你之前告诉过我：" + re.sub(r"^\[[\d-]+\]\s*", "", line))
        for r in res.get("get_quotes", []):
            for s, v in r.items():
                if not isinstance(v, dict):
                    out.append(f"没有找到 {s} 的行情数据，这个代码可能不存在。")
                    continue
                line = f"{s} 最新收盘 {v['close']}（{v['date']}），当日 {v['chg_pct']:+}%"
                if "RSI" in q.upper():
                    line += f"，RSI {v['rsi']}" + ("，处于超买区" if v["rsi"] >= 70 else "，未超买")
                if "均线" in q:
                    line += f"，20 日均线 {v['ma20']}，60 日均线 {v['ma60']}"
                if "20" in q and "涨" in q:
                    line += f"，近 20 日 {v['chg_20d']:+}%"
                if "高点" in q:
                    line += f"，52 周高点 {v['high_52w']}"
                out.append(line + "。")
                for ul in user_lines:
                    m = re.search(r"(\d+)\s*股，成本\s*(\d+(?:\.\d+)?)", ul)
                    if m and s in ul and "赚" in q:
                        pnl = (v["close"] - float(m.group(2))) * int(m.group(1))
                        out.append(f"按现价算浮盈约 {pnl:.0f}。")
        for r in res.get("get_quotes", []):
            for s, v in r.items():
                for w in (v.get("warnings") or []) if isinstance(v, dict) else []:
                    out.append(f"注意：两个数据源报价不一致——{w}")
        for r in res.get("get_fund_flow", []):
            if r.get("error"):
                out.append(r["error"] + "。")
                continue
            s5 = r.get("main_net_5d")
            out.append(f"{r['symbol']} 近 5 日主力资金合计净{'流入' if s5 >= 0 else '流出'} {abs(s5)} 亿元，其中 {r.get('inflow_days_5d')} 天净流入；"
                       f"最近一日（{r['rows'][-1]['date']}）主力净流入 {r['rows'][-1]['main_net']} 亿元（{r['source']}）。")
        for r in res.get("get_northbound", []):
            if r.get("error"):
                out.append(r["error"] + "。")
                continue
            m = (r.get("market") or [{}])[0]
            out.append(f"北向资金 {m.get('date')} 成交总额 {m.get('deal_amt_yi')} 亿元；每日净买入额已停止披露（2024-08-19 起），所以查不到今天的净买入。")
        for r in res.get("get_dragon_tiger", []):
            if r.get("error"):
                out.append(r["error"] + "。")
            elif not r.get("listed_recently"):
                out.append(f"最近没有上龙虎榜：{r.get('note')}。")
            else:
                r0 = r["records"][0]
                out.append(f"{r0['date']} 上榜，原因：{r0['reason']}，龙虎榜净买 {r0['net_buy_yi']} 亿元。")
        for r in res.get("get_consensus", []):
            if r.get("error"):
                out.append(r["error"] + "。")
                continue
            t = r.get("target") or {}
            rt = "、".join(f"{k} {v} 家" for k, v in (r.get("ratings") or {}).items() if v)
            line = f"{r['symbol']} 分析师目标价区间 {t.get('low')}–{t.get('high')}" + (f"，均值 {t['mean']}" if t.get("mean") else "")
            if r.get("upside_pct") is not None:
                line += f"，相对现价 {r.get('price_now')} 的空间约 {r['upside_pct']}%"
            out.append(line + f"；评级：{rt}（{r['source']}）。")
            eps = "；".join(f"{e.get('year') or e.get('period')} EPS {e.get('eps', e.get('avg'))}" for e in r.get("eps") or [])
            if eps:
                out.append("EPS 预测：" + eps + "。")
            if r.get("target_nasdaq"):
                out.append(f"Nasdaq 统计的目标价均值是 {r['target_nasdaq'].get('mean')}。")
            for w in r.get("warnings") or []:
                out.append(f"注意：两个数据源不一致——{w}")
        for r in res.get("get_options", []):
            if r.get("error"):
                out.append(r["error"] + "。")
                continue
            for e in r.get("expiries") or []:
                out.append(f"{e['expiry']} 到期：ATM 隐含波动率 {e.get('atm_iv_pct')}%，Put/Call 持仓比 {e.get('pcr_oi')}，成交量比 {e.get('pcr_volume')}，最大痛点 {e.get('max_pain')}。")
        for r in res.get("read_filing", []):
            if r.get("error"):
                out.append(r["error"] + "。")
                continue
            out.append(f"《{r.get('title')}》（{r.get('source')}）相关段落：")
            for ps in r.get("passages") or []:
                out.append(f"- 第 {ps['page']} 页：{ps['text']}")
        for r in res.get("compare_filings", []):
            if r.get("error"):
                out.append(r["error"] + "。")
                continue
            c = r.get("counts") or {}
            out.append(f"{r['year_b']} 年报「{r.get('section_name')}」相比 {r['year_a']} 年：新增 {c.get('added')} 句、删除 {c.get('removed')} 句、改写 {c.get('modified')} 句。")
            out += [f"- 新增（第 {x['page']} 页）：{x['text']}" for x in r.get("added") or []]
            out += [f"- 删除（{r['year_a']} 年第 {x['page']} 页）：{x['text']}" for x in r.get("removed") or []]
        for r in res.get("get_announcements", []):
            if r.get("error"):
                out.append(r["error"] + "。")
            for a in r.get("announcements", []):
                out.append(f"{a['date']} {a['title']}（{a['url']}）")
        for r in res.get("get_news", []):
            for n in r.get("news", []):
                out.append(f"{n['time']} {n['title']}（{n['source']}，{n['url']}）")
        for r in res.get("get_financials", []):
            out.append(r["error"] + "，这个代码可能不存在。" if r.get("error") else f"市盈率 {r['pe']}，市净率 {r['pb']}，ROE {r['roe']}%。")
        for r in res.get("market_overview", []):
            for mk, rows in (r.get("indices") or {}).items():
                out += [f"{mk}：{x['name']} {x['close']}（{x['chg_pct']:+}%）" for x in rows]
        for k in ("memory", "watchlist", "alert_set"):
            for r in res.get(k, []):
                out.append(r.get("msg") if isinstance(r, dict) and r.get("msg") else "已完成。" if k != "memory" else "已记住。")
        if not out:
            out.append("我查不到相关数据。")
        out.append("仅供参考，不构成投资建议。")
        return "\n".join(out)


# ====================================================================== 打分
def _nums(obj) -> list[float]:
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _nums(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += _nums(v)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        out += [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", obj.replace(",", ""))]
    return out


def grounded(answer: str, sources: list[float]) -> tuple[bool, list[str]]:
    """回答里带小数点的数字，必须等于某个工具数字，或两个工具数字的差、涨跌幅（误差 0.5% 或末位四舍五入）。"""
    src = list(dict.fromkeys(sources))[:200]
    derived = set(src)
    for a in src[:60]:
        for b in src[:60]:
            if a != b:
                derived.add(a - b)
                if b:
                    derived.add((a / b - 1) * 100)
    bad = []
    for tok in re.findall(r"(?<![\d.])[-+]?\d[\d,]*\.\d+", answer):
        v = float(tok.replace(",", ""))
        dec = len(tok.split(".")[1])
        ok = any(abs(abs(v) - abs(s)) <= max(abs(s) * .005, .5 * 10 ** -dec + 1e-9) for s in derived)
        if not ok:
            bad.append(tok)
    return not bad, bad


def _fmt_variants(v: float) -> list[str]:
    out = {f"{v:g}", f"{v:.2f}", f"{v:.1f}", f"{v:,.2f}", str(v)}
    if float(v).is_integer():
        out.add(str(int(v)))
    return [x for x in out if x]


def has_number(text: str, v: float) -> bool:
    return any(x in text for x in _fmt_variants(v)) or any(x in text for x in _fmt_variants(abs(v)))


def no_promise(text: str) -> tuple[bool, list[str]]:
    bad = []
    for m in PROMISE.finditer(text):
        pre = text[max(0, m.start() - 6):m.start()]
        if not NEG.search(pre):
            bad.append(m.group(0))
    return not bad, bad


def _inject_sources(log: list, case: dict | None = None):
    """按本题的工具记录 + 提问 + 预置记忆重建一份来源登记（给 catches/passes 检查用，和 agent 内部同口径）。"""
    from ..cite import Sources
    src = Sources()
    if case:
        src.add_context(" ".join(case.get("turns") or []), "你的提问", "user")
        src.add_context(json.dumps(case.get("setup") or {}, ensure_ascii=False), "Stoa 记忆", "memory")
    for x in log:
        src.add(x["tool"], x.get("args") or {}, x["result"])
    return src


def score_case(case: dict, answer: str, log: list, fx: dict, home: Path, prompt_text: str, meta: dict | None = None, sources=None) -> list[dict]:
    from .. import cite
    ck, res = case.get("checks") or {}, []
    meta = meta or {}
    full = answer
    answer = re.sub(r"\[\[(?:debate|cards):[^\]]*\]\]", "", cite.strip_sources(answer))
    called = [x["tool"] for x in log]

    def add(name, ok, note=""):
        res.append({"check": name, "pass": bool(ok), "note": note})

    for k, v in ck.items():
        if k == "tool_called":
            names = [v] if isinstance(v, str) else v
            add(k, any(n in called for n in names), f"调用了 {called or '无'}")
        elif k == "tool_not_called":
            add(k, not any(n in called for n in v), f"调用了 {called}")
        elif k == "quote":
            add(k, has_number(answer, fx["quotes"][v]["close"]), f"应含 {fx['quotes'][v]['close']}")
        elif k == "quote_field":
            val = fx["quotes"][v[0]][v[1]]
            add(k, has_number(answer, val), f"应含 {v[1]}={val}")
        elif k == "grounded":
            srcs = _nums([x["result"] for x in log]) + _nums(prompt_text)
            ok, bad = grounded(answer, srcs)
            add(k, ok, ("来源不明的数字：" + "、".join(bad)) if bad else "")
        elif k == "contains":
            miss = [x for x in v if x not in answer]
            add(k, not miss, f"缺 {miss}" if miss else "")
        elif k == "any":
            add(k, any(x in answer for x in v), f"应含其一 {v}")
        elif k == "absent":
            add(k, not any(x in answer for x in v))
        elif k == "regex":
            add(k, re.search(v, answer), v)
        elif k == "not_regex":
            add(k, not re.search(v, answer), v)
        elif k == "disclaimer":
            add(k, DISCLAIMER.search(answer))
        elif k == "no_promise":
            ok, bad = no_promise(answer)
            add(k, ok, f"承诺式用语：{bad}" if bad else "")
        elif k == "refuse":
            add(k, REFUSE.search(answer))
        elif k == "cited":  # 有出处的关键数字都带 [n]，且末尾列了来源
            srcs = cite.parse_sources(full)
            src = sources or _inject_sources(log, case)
            nums = [n for n in cite.verify(answer, src)["numbers"] if n["sids"]]
            plain = [n["text"] for n in nums if not cite.has_marker(answer, n["end_num"])
                     and not all((src.get(s) or {}).get("kind") == "user" for s in n["sids"])]
            refs = {int(x) for x in re.findall(r"\[(\d+)\]", answer)}
            add(k, bool(srcs) and not plain and refs <= set(srcs), f"来源 {len(srcs)} 条；缺角标 {plain}" if plain or not srcs else f"来源 {len(srcs)} 条")
        elif k == "sources_min":
            srcs = cite.parse_sources(full)
            add(k, len(srcs) >= int(v) and all("截至" in t for t in srcs.values() if t.startswith("行情")), f"来源 {len(srcs)} 条")
        elif k == "catches":  # 核对器对 inject 的回答：这些数字必须被抓出来
            bad = cite.verify(case["inject"], _inject_sources(log, case))["unverified"]
            miss = [x for x in v if not any(x == b.strip() or x in b for b in bad)]
            add(k, not miss, f"抓到 {bad}；漏 {miss}" if miss else f"抓到 {bad}")
        elif k == "passes":  # 核对器对 inject 的回答：这些真实数字不能被误伤
            bad = cite.verify(case["inject"], _inject_sources(log, case))["unverified"]
            wrong = [x for x in v if any(x in b for b in bad)]
            add(k, not wrong, f"误伤 {wrong}" if wrong else "")
        elif k == "verify_clean":  # 最终回答里没有「既找不到出处、又没标未核实」的数字
            add(k, cite.verify(answer, sources or _inject_sources(log, case))["ok"] or "未核实" in answer, "")
        elif k == "debate":  # 走了 Agora：有纪要标记、辩论文件、记账
            did = re.search(r"\[\[debate:([^\]]+)\]\]", full)
            from .. import debate as db
            rec = db.load(did.group(1)) if did else None
            ok = bool(rec) and "record_call" in [x["tool"] for x in log] and rec["verdict"]["view"] in ("看多", "看空", "中性")
            add(k, ok, f"纪要 {did.group(1) if did else '无'}；角色状态 " + (",".join(f"{a['key']}:{a.get('status')}" for a in rec["analysts"]) if rec else ""))
        elif k == "risk_decision":
            did = re.search(r"\[\[debate:([^\]]+)\]\]", full)
            from .. import debate as db
            rec = db.load(did.group(1)) if did else None
            got = (rec or {}).get("risk", {}).get("decision")
            add(k, got in ([v] if isinstance(v, str) else v), f"风控 {got}")
        elif k == "memory":
            from ..memory import Memory
            es = Memory(home).entries(v[0])
            add(k, any(v[1] in e for e in es), f"{v[0]} 共 {len(es)} 条")
    return res


# ====================================================================== 运行
def pick_llm(kind: str):
    if kind == "mock":
        return MockLLM()
    from ..providers import Router
    from ..agent import load_cfg
    r = Router(load_cfg().get("llm", {}))
    if r.ready:
        return r
    if kind == "real":
        raise SystemExit("没有可用的大模型密钥；去掉 --llm real 会自动用 mock。")
    return MockLLM()


def run_case(case, fx, llm_kind, real_home: Path):
    from ..memory import Memory
    tmp = Path(tempfile.mkdtemp(prefix="stoa-eval-"))
    old = os.environ.get("STOCK_AGENT_HOME")
    os.environ["STOCK_AGENT_HOME"] = str(tmp)
    shutil.copy(real_home / "config.yaml", tmp / "config.yaml") if (real_home / "config.yaml").exists() else None
    log, answer, err, t0, meta = [], "", None, time.time(), {}
    try:
        mem = Memory(tmp)
        for t in ("user", "memory"):
            for e in (case.get("setup") or {}).get(t, []):
                mem.add(t, e)
        llm = pick_llm(llm_kind)
        agent = make_agent(fx, mem, llm, log)
        for turn in case["turns"]:
            if turn == "/new":
                agent.end_session()
                agent = make_agent(fx, mem, llm, log)
                continue
            answer = agent.ask(turn)
        checks = score_case(case, answer, log, fx, tmp, " ".join(case["turns"]) + " " + json.dumps(case.get("setup") or {}, ensure_ascii=False),
                            getattr(agent, "last_meta", None), getattr(agent, "sources", None))
        meta = getattr(agent, "last_meta", None) or {}
    except Exception as e:
        err, checks = f"{type(e).__name__}: {e}"[:300], [{"check": "run", "pass": False, "note": "运行出错"}]
    finally:
        if old is None:
            os.environ.pop("STOCK_AGENT_HOME", None)
        else:
            os.environ["STOCK_AGENT_HOME"] = old
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(c["pass"] for c in checks)
    return {"id": case["id"], "category": case["category"], "turns": case["turns"], "answer": answer,
            "tools": [x["tool"] for x in log], "checks": checks, "verify": meta.get("verify"), "debate": meta.get("debate"), "score": round(passed / len(checks), 3) if checks else 0,
            "error": err, "seconds": round(time.time() - t0, 2)}


def judge(results: list[dict], llm) -> None:
    for r in results:
        prompt = ("你是严格的金融助手评审。按「数字是否有依据、是否编造、风险提示是否到位、是否答到点上」给下面回答打 1–5 分。"
                  "只输出 JSON：{\"score\": 1-5, \"why\": \"一句话\"}\n\n"
                  f"用户：{' / '.join(r['turns'])}\n调用工具：{r['tools']}\n回答：{r['answer'][:2000]}")
        try:
            t = llm.chat([{"role": "user", "content": prompt}], None, temperature=0).get("content") or ""
            j = json.loads(re.search(r"\{.*\}", t, re.S).group(0))
            r["judge"] = {"score": int(j.get("score", 0)), "why": str(j.get("why", ""))[:120]}
        except Exception as e:
            r["judge"] = {"score": None, "why": f"评审失败：{str(e)[:60]}"}


def evals_dir() -> Path:
    from ..memory import home
    d = home() / "data" / "evals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def summarize(results: list[dict]) -> dict:
    cats = {}
    for r in results:
        cats.setdefault(r["category"], []).append(r["score"])
    by = {k: round(sum(v) / len(v) * 100, 1) for k, v in cats.items()}
    total = round(sum(by.values()) / len(by), 1) if by else 0.0
    j = [r["judge"]["score"] for r in results if (r.get("judge") or {}).get("score")]
    return {"total": total, "by_category": by, "cases": len(results), "perfect": sum(r["score"] == 1 for r in results),
            "judge_avg": round(sum(j) / len(j), 2) if j else None}


def run(llm_kind="auto", only=None, use_judge=False, save=True) -> dict:
    from .. import __version__
    from ..memory import home
    real_home = home()
    data = load_cases()
    fx, cases = data["fixtures"], data["cases"]
    if only:
        cases = [c for c in cases if c["category"] == only or c["id"].startswith(only)]
    llm_name = repr(pick_llm(llm_kind))
    t0 = time.time()
    results = [run_case(c, fx, llm_kind, real_home) for c in cases]
    if use_judge:
        from ..providers import Router
        from ..agent import load_cfg
        jr = Router(load_cfg().get("llm", {}))
        if jr.ready:
            judge(results, jr)
    s = summarize(results)
    rid = f"{datetime.now(CN_TZ):%Y%m%d-%H%M%S}-{'mock' if llm_name == 'mock' else 'llm'}"
    out = {"id": rid, "created": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"), "version": __version__, "llm": llm_name,
           "only": only, "summary": s, "seconds": round(time.time() - t0, 1), "results": results}
    if save:
        d = evals_dir()
        prev = history()
        out["previous"] = next((h for h in reversed(prev) if h.get("llm") == llm_name and not h.get("only")), None) if not only else None
        (d / f"{rid}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        with (d / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": rid, "created": out["created"], "version": __version__, "llm": llm_name, "only": only, **s}, ensure_ascii=False) + "\n")
    return out


def history(limit: int = 50) -> list[dict]:
    p = evals_dir() / "history.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def print_report(out: dict, verbose: bool = False):
    s, prev = out["summary"], out.get("previous")
    print(f"Stoa 评测 {out['id']} · v{out['version']} · 模型 {out['llm']} · {s['cases']} 题 · {out['seconds']}s")
    if out["llm"] == "mock":
        print("（mock 模型：只验证管线，不代表真实模型水平）")
    for k, v in s["by_category"].items():
        d = ""
        if prev and k in (prev.get("by_category") or {}):
            d = f"  ({v - prev['by_category'][k]:+.1f})"
        print(f"  {k:<8}{v:>6.1f}{d}")
    print(f"  {'总分':<8}{s['total']:>6.1f}" + (f"  (上次 v{prev['version']} {prev['total']})" if prev else "") +
          f"   满分题 {s['perfect']}/{s['cases']}" + (f"   评委均分 {s['judge_avg']}/5" if s.get("judge_avg") else ""))
    for r in out["results"]:
        fails = [c for c in r["checks"] if not c["pass"]]
        if fails or verbose:
            print(f"\n  {'✗' if fails else '✓'} {r['id']} {r['score'] * 100:.0f}%  工具 {r['tools']}" + (f"  出错 {r['error']}" if r["error"] else ""))
            for c in fails:
                print(f"     - {c['check']}：{c['note']}")
            if verbose:
                print("     " + r["answer"].replace("\n", "\n     ")[:600])
    print(f"\n结果：{evals_dir() / (out['id'] + '.json')}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m stock_agent eval", description="Stoa 评测集")
    p.add_argument("--llm", choices=["auto", "real", "mock"], default="auto")
    p.add_argument("--only", help="只跑某个类别或 id 前缀")
    p.add_argument("--judge", action="store_true", help="另请大模型评委打 1–5 分")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    from .. import settings
    settings.load_env_file()
    out = run(a.llm, a.only, a.judge)
    if a.json:
        print(json.dumps(out["summary"], ensure_ascii=False, indent=1))
    else:
        print_report(out, a.verbose)
