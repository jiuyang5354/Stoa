"""v0.38 · 盲评差距（缺数据换源、时效事实联网核对、可复算出处、代码先跑通、长文档完整抽取）+ 可比公司/财务建模 + 退市代码历史价。
每条都按 v0.37 的失败写：在 v0.37 代码上这些测试会失败（模块不存在、闸门不触发、角标插进公式、旧口径）。"""
import json

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    monkeypatch.delenv("TOOL_SELECT", raising=False)
    monkeypatch.delenv("ALLOW_CODE", raising=False)
    monkeypatch.setenv("QUOTE_CROSSCHECK", "off")
    return tmp_path


# ------------------------------------------------------------------ 1. 缺数据换源
TX_A = 'v_sh600519="1~贵州茅台~600519~1392.05~1354.00~1360.00~59927~30000~29927~' + "~" * 21 + '20261030161455~38.05~2.81~1395~1350~1392.05/59927/8257381778~59927~825738~0.48~20.1~"'


def _tx_line(fields: dict, n=60):
    f = [""] * n
    for k, v in fields.items():
        f[k] = v
    return "~".join(f)


def test_turnover_a_share_and_hk():
    from stock_agent import hkdata
    a = hkdata.parse_turnover(_tx_line({1: "贵州茅台", 3: "1392.05", 30: "20261030161455", 35: "1392.05/59927/8257381778", 36: "59927", 37: "825738"}), "A", "sh600519")
    assert a["amount"] == 8257381778 and a["amount_yi"] == 82.57 and a["date"] == "2026-10-30" and a["url"] == "https://gu.qq.com/sh600519"
    h = hkdata.parse_turnover(_tx_line({1: "腾讯控股", 3: "383.6", 30: "2026/10/30 16:08:14", 36: "22269954", 37: "8540915574.008"}), "HK", "hk00700")
    assert h["currency"] == "HKD" and h["amount_yi"] == 85.41 and h["date"] == "2026-10-30"


def test_get_quotes_carries_turnover(home, monkeypatch):
    """盲评 Q1：v0.37 的 get_quotes 没有成交额，模型只能答「暂未查到」。"""
    import pandas as pd
    from stock_agent import agent as AG, data, hkdata
    ser = pd.Series([1300.0 + i for i in range(80)], index=pd.date_range("2026-07-01", periods=80, freq="B"))
    ser.attrs["source"] = "腾讯证券"
    monkeypatch.setattr(data, "fetch_closes", lambda syms, period="1y": {"600519.SS": ser})
    monkeypatch.setattr(hkdata, "turnover", lambda s: {"date": ser.index[-1].strftime("%Y-%m-%d"), "amount": 8.26e9, "amount_yi": 82.57, "currency": "CNY",
                                                        "source": "腾讯证券实时行情", "url": "https://gu.qq.com/sh600519"})
    q = AG.quotes(["600519.SS"])
    assert q["600519.SS"]["turnover"]["amount_yi"] == 82.57


def test_southbound_market_and_stock(monkeypatch):
    """盲评 Q2：南向资金每日净买入仍披露，v0.37 只有北向工具。"""
    from stock_agent import hkdata

    def fake(url, params=None, **kw):
        rn = params["reportName"]
        if rn == "RPT_MUTUAL_DEAL_HISTORY":
            mt = params["filter"].split('"')[1]
            net = {"006": -3559.58, "002": -2261.43, "004": -1298.15}[mt]
            return {"result": {"data": [{"TRADE_DATE": "2026-10-30 00:00:00", "NET_DEAL_AMT": net, "BUY_AMT": 35315.51, "SELL_AMT": 38875.09},
                                        {"TRADE_DATE": "2026-10-29 00:00:00", "NET_DEAL_AMT": net / 2, "BUY_AMT": 1, "SELL_AMT": 1}]}}
        return {"result": {"data": [{"MUTUAL_TYPE": "002", "TRADE_DATE": "2026-10-30 00:00:00", "NET_BUY_AMT": -1.5e8, "BUY_AMT": 6e8, "SELL_AMT": 7.5e8, "CLOSE_PRICE": 383.6},
                                    {"MUTUAL_TYPE": "004", "TRADE_DATE": "2026-10-30 00:00:00", "NET_BUY_AMT": -0.57e8, "BUY_AMT": 7.46e8, "SELL_AMT": 8.03e8, "CLOSE_PRICE": 383.6},
                                    {"MUTUAL_TYPE": "002", "TRADE_DATE": "2026-10-29 00:00:00", "NET_BUY_AMT": 1.46e8, "BUY_AMT": 9e8, "SELL_AMT": 7.54e8, "CLOSE_PRICE": 377.6}]}}
    monkeypatch.setattr(hkdata, "_fetch", fake)
    r = hkdata.southbound("0700.HK", 5)
    assert r["market"][0]["total_net_buy_yi"] == -35.6 and r["market_sum_net_buy_yi"] == -53.4
    assert r["stock"][0]["net_buy_yi"] == -2.07 and r["stock_sum_net_buy_yi"] == -0.61 and r["stock_verdict"] == "净卖出"
    assert "00700" in r["url"] and "北向" in r["rule_note"]


DI_HTML = """<table><tr><td>表格序號</td></tr>
<tr><td class="tbCell"><a href="NSForm2.aspx?fn=CS20260625E00375&amp;sa2=an&amp;sc=981">CS20260625E00375</a></td><td class="tbCell">国家集成电路产业投资基金股份有限公司</td>
<td class="tbCell"><a href="javascript:openWin('NSStdCode_C.htm?ft=CS#1213');">1213</a>(L)<br></td><td class="tbCell">9,000,000(L)<br></td><td class="tbCell">HKD 79.7000</td>
<td class="tbCell">582,332,707(L)<br></td><td class="tbCell">6.80(L)<br></td><td class="tbCell"><a href="x">23/06/2026</a></td><td>&nbsp;</td></tr>
<tr><td class="tbCell"><a href="NSForm2.aspx?fn=CS20260527E00385&amp;sc=981">CS20260527E00385</a></td><td class="tbCell">国家集成电路产业投资基金股份有限公司</td>
<td class="tbCell"><a href="#">1201</a>(L)<br></td><td class="tbCell">7,000,000(L)<br></td><td class="tbCell">HKD 86.6900</td>
<td class="tbCell">640,311,906(L)<br></td><td class="tbCell">7.99(L)<br></td><td class="tbCell"><a href="x">26/05/2026</a></td></tr></table>"""


def test_hk_interests_parses_disclosures(monkeypatch):
    """盲评 Q12：v0.37 只查 A股公告，漏了 2026-05/06 大基金在 H 股的减持（披露易权益披露）。"""
    from stock_agent import hkdata
    pages = {hkdata.DI_LIST: '<a href="NSAllFormList.aspx?sa2=an&amp;sid=6552&amp;sc=981">x</a>'}
    monkeypatch.setattr(hkdata, "_fetch", lambda url, params=None, **kw: pages.get(url, DI_HTML))
    r = hkdata.hk_interests("0981.HK", 365)
    n0, n1 = r["notices"][0], r["notices"][1]
    assert n0["event_date"] == "2026-06-23" and n0["shares_traded"] == "9,000,000(L)" and n0["avg_price"] == "HKD 79.7000" and n0["pct_of_issued"] == "6.80(L)"
    assert n0["reason_code"] == "1213" and n0["url"].startswith("https://di.hkex.com.hk/di/NSForm2.aspx?fn=CS20260625E00375")
    assert n0["change_vs_prev_notice"] == "减少" and n1["reason_code"] == "1201"
    assert "di.hkex.com.hk" in r["url"]


def test_hstech_index_ytd_via_tencent(monkeypatch):
    """盲评 Q8：Yahoo 没有 ^HSTECH（404），v0.37 只能拿 ETF 近似；现在走腾讯指数日线，年初至今从上年末收盘算。"""
    from stock_agent import edgar, hkdata

    def boom(*a, **k):
        raise LookupError("404")
    monkeypatch.setattr(edgar, "_yahoo_daily", boom)
    days = [["2025-12-30", "5503.61", "5578.38", "5597.87", "5503.61", "1"], ["2025-12-31", "5566.24", "5515.98", "5581.97", "5493.15", "1"],
            ["2026-01-02", "5547.85", "5736.44", "5753.28", "5530.99", "1"], ["2026-10-30", "3720.49", "3754.90", "3809.71", "3706.30", "1"]]
    monkeypatch.setattr(hkdata, "_fetch", lambda url, params=None, **kw: {"data": {"hkHSTECH": {"day": days}}})
    r = edgar.price_history("^HSTECH", None, "2026-01-01", "2026-10-30")
    assert r["range"]["change_pct_from_prev_close"] == round((3754.90 / 5515.98 - 1) * 100, 4)
    assert "腾讯" in r["source"] and r["url"] == "https://gu.qq.com/hkHSTECH"


def test_delisted_close_from_sec_proxy(monkeypatch):
    """vals-12：GTLS 被 Baker Hughes 收购后 Yahoo 没有数据，v0.37 拿到 2021 年过期价 → 现在从合并委托书原文取 7/28 收盘 171.65。"""
    from stock_agent import edgar

    def boom(*a, **k):
        raise LookupError("404")
    monkeypatch.setattr(edgar, "_yahoo_daily", boom)
    monkeypatch.setattr(edgar, "_cik", lambda s: ("0000892553", "CHART INDUSTRIES INC"))
    monkeypatch.setattr(edgar, "full_text_search", lambda *a, **k: {"hits": [{"form": "8-K", "url": "u8k", "filing_date": "2025-07-29"},
                                                                         {"form": "DEFM14A", "url": "https://www.sec.gov/defm14a.htm", "filing_date": "2025-09-08"}]})
    txt = ("On July 28, 2025, the last trading day prior to the public announcement of the execution of the Merger Agreement, the\n"
           "closing price of Chart common stock on the NYSE was $171.65 per share.")
    monkeypatch.setattr(edgar, "read_document", lambda url, **k: {"passages": [{"text": txt}]} if "defm14a" in url else {"passages": [{"text": "nothing"}]})
    r = edgar.price_history("GTLS", ["2025-07-28"])
    assert r["dates"][0]["close"] == 171.65 and "DEFM14A" in r["source"] and r["url"].endswith("defm14a.htm")


def test_delisted_ticker_cik_from_display_names(monkeypatch):
    from stock_agent import edgar
    monkeypatch.setattr(edgar, "_ticker_map", lambda: {})

    def fake(url, params=None, **kw):
        if params and "keysTyped" in params:
            return {"hits": {"hits": []}}
        return {"hits": {"hits": [{"_source": {"display_names": ["GREIF, INC  (GEF, GEF-B)  (CIK 0000043920)"]}},
                                  {"_source": {"display_names": ["CHART INDUSTRIES INC  (GTLS, GTLS-PB)  (CIK 0000892553)"]}}]}}
    monkeypatch.setattr(edgar, "_fetch", fake)
    r = edgar.cik_lookup("GTLS")
    assert r["cik"] == "0000892553" and r["name"] == "CHART INDUSTRIES INC"


# ------------------------------------------------------------------ 2/4/1 闸门（规则）
def test_stale_fact_gate():
    from stock_agent import gates
    r = "格雷格·阿贝尔已接任首席执行官；巴菲特继续担任董事会主席。"
    assert gates.stale_facts("总结伯克希尔最近一封致股东信", r, set())
    assert gates.stale_facts("总结伯克希尔最近一封致股东信", r, {"web_search", "doc_digest"}, ["Berkshire 2025 shareholder letter pdf"])  # 只读了旧信件
    assert gates.stale_facts("总结伯克希尔最近一封致股东信", r, {"web_search"}, ["Berkshire Hathaway chairman 2026"]) is None
    assert gates.stale_facts("总结伯克希尔最近一封致股东信", r, {"get_announcements"}) is None
    assert gates.stale_facts("茅台股价", "茅台收盘 1392 元。", set()) is None


def test_code_gate_compile_and_run():
    from stock_agent import gates
    bad = "代码如下：\n```python\nimport numpy as np\nresult = stock_metrics(prices\nprint(result)\n```"
    assert "SyntaxError" in gates.code_issues("写一段 Python 计算最大回撤", bad)
    good = "```python\ndef f(x):\n    return x * 2\nprint(f(2))\n```"
    assert gates.code_issues("写一段 Python", good) is None
    err = lambda code: 'Traceback (most recent call last):\n  File "x", line 1\nTypeError: unsupported operand'
    assert "TypeError" in gates.code_issues("写一段 Python", good, run=err)
    soft = lambda code: "Traceback (most recent call last):\nModuleNotFoundError: No module named 'yfinance'"
    assert gates.code_issues("写一段 Python", good, run=soft) is None


def test_gap_gate_gives_specific_sources():
    from stock_agent import gates
    n = gates.gap_hints("贵州茅台今天收盘价、涨跌幅、成交额是多少？", "收盘 1392.05 元；成交额：暂未查到可靠数据。")
    assert n and "turnover" in n
    n = gates.gap_hints("港股恒生科技指数今年以来涨跌幅", "恒生科技指数年初至今无法确认，用 ETF 近似")
    assert n and "^HSTECH" in n
    n = gates.gap_hints("腾讯南向资金是净买还是净卖", "南向资金方面，同期是净买还是净卖仍无法确认。")
    assert n and "get_southbound" in n
    assert gates.gap_hints("茅台收盘价", "茅台收盘 1392.05 元。") is None


class ScriptLLM:
    ready = True

    def __init__(self, replies):
        self.replies, self.calls, self.seen = list(replies), 0, []

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        self.seen.append(messages[-1]["content"] if isinstance(messages[-1].get("content"), str) else "")
        r = self.replies.pop(0) if self.replies else "（完）"
        if isinstance(r, tuple):
            name, args = r
            return {"role": "assistant", "content": "查一下。", "tool_calls": [
                {"id": f"c{self.calls}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
        return {"role": "assistant", "content": r}


def _agent(llm, results=None):
    from stock_agent.agent import Agent
    from stock_agent.memory import Memory

    class A(Agent):
        def run_tool(self, name, args):
            return (results or {}).get(name, {"ok": True})
    a = A(Memory(), channel="web", session="web-v038")
    a.llm = llm
    a._no_debate = True
    return a


def test_agent_verifies_personnel_before_answering(home):
    """盲评 Q31：v0.37 直接写「巴菲特继续担任董事长」（过时）。现在没联网核对就会被打回，先 web_search。"""
    llm = ScriptLLM(["伯克希尔：阿贝尔接任首席执行官，巴菲特继续担任董事长。",
                     ("web_search", {"query": "Berkshire Hathaway chairman 2026"}),
                     "伯克希尔：阿贝尔任首席执行官；据 2026-09-18 公告，巴菲特已转任名誉董事长。"])
    a = _agent(llm, {"web_search": {"results": [{"title": "Berkshire names Abel chairman", "url": "https://example.com/brk", "snippet": "Buffett chairman emeritus"}]}})
    out = a.ask("伯克希尔现在谁是董事长？")
    assert "名誉董事长" in out and any("人事/职位" in s for s in llm.seen)
    assert a.last_meta.get("verify", {}).get("gates") == ["stale"] or "stale" in json.dumps(a.last_meta, ensure_ascii=False)


def test_agent_fixes_code_before_delivering(home):
    """盲评 Q32：代码有语法错误也原样交付。现在编译不过会带报错打回改正。"""
    llm = ScriptLLM(["```python\ndef mdd(p):\n    peak = p[0\n    return 0\n```",
                     "```python\ndef mdd(p):\n    peak, m = p[0], 0.0\n    for x in p:\n        peak = max(peak, x); m = min(m, x / peak - 1)\n    return m\n\nprint(mdd([1, 2, 1.5]))\n```"])
    a = _agent(llm)
    out = a.ask("写一段 Python 计算一只股票的最大回撤")
    assert "peak, m = p[0], 0.0" in out and any("SyntaxError" in s for s in llm.seen)


# ------------------------------------------------------------------ 5. 长文档完整抽取
def test_doc_digest_reads_every_part():
    """盲评 Q31：v0.37 fetch_url 只给前 6000 字且不读 PDF。doc_digest 分段读完全文，最后一段的要点也在。"""
    from stock_agent import docread
    paras = [f"Section {i}. Operating earnings rose to ${i * 10}.5 billion this year and we repurchased shares." + " filler text." * 40 for i in range(1, 40)]
    paras.append("Finally, Warren Buffett became chairman emeritus and Greg Abel was named chairman on September 18, 2026.")
    text = "\n\n".join(paras)

    class L:
        def __init__(self):
            self.n = 0

        def chat(self, msgs, tools=None, **kw):
            self.n += 1
            seg = msgs[0]["content"]
            pts = [ln.split(".")[0] for ln in seg.split("\n") if ln.startswith(("Section", "Finally"))]
            return {"content": "\n".join("- " + p for p in pts) or "- （本段无实质要点）"}
    llm = L()
    r = docread.digest(text=text, llm=llm, title="letter")
    assert r["coverage"]["complete"] and r["coverage"]["parts"] == llm.n > 3
    allp = " ".join(p for part in r["parts"] for p in part["points"])
    assert "Finally, Warren Buffett became chairman emeritus and Greg Abel was named chairman on September 18, 2026" in allp
    r2 = docread.digest(text=text, llm=None)  # 无模型：规则抽取也要覆盖最后一段
    assert r2["coverage"]["complete"] and "chairman emeritus" in " ".join(p for part in r2["parts"] for p in part["points"])


def test_fetch_url_paginates_and_reads_pdf(monkeypatch):
    from stock_agent import research

    class Resp:
        def __init__(self, body, ctype):
            self.status_code, self.headers, self.encoding = 200, {"content-type": ctype}, "utf-8"
            self.raw = type("R", (), {"read": lambda s, n, decode_content=True: body})()

        def close(self):
            pass
    html = ("<html><title>T</title><body>" + "".join(f"<p>{'段落%03d ' % i}{'内容' * 30}</p>" for i in range(300)) + "</body></html>").encode()
    monkeypatch.setattr(research, "_public", lambda h: True)
    monkeypatch.setattr(research.requests, "get", lambda *a, **k: Resp(html, "text/html"))
    r1 = research.fetch_url("https://example.com/a")
    assert r1["truncated"] and r1["next_start"] == 6000
    r2 = research.fetch_url("https://example.com/a", start=r1["next_start"])
    assert r2["text"] and r2["text"] != r1["text"]
    import stock_agent.filings as F
    monkeypatch.setattr(F, "pdf_pages", lambda data, max_pages=400: ["Dear shareholders, page one." * 5, "Page two: Greg Abel chairman." * 5])
    monkeypatch.setattr(research.requests, "get", lambda *a, **k: Resp(b"%PDF-1.4", "application/pdf"))
    p = research.fetch_url("https://www.berkshirehathaway.com/letters/2025ltr.pdf")
    assert p["kind"] == "pdf" and p["pages"] == 2 and "Greg Abel" in p["text"] and "error" not in p


# ------------------------------------------------------------------ 3. 可复算 + 角标不拆公式
def test_markers_not_inserted_into_math():
    """v0.37 外部评测：「120{,}637[5]{,}192」「93.18[1]\\%」——角标插进 LaTeX 把数字拆坏。"""
    from stock_agent import cite
    src = cite.Sources()
    src.add("calc", {"expressions": {"a": "196.85*102.572"}}, {"results": {"a": 20191.2982, "pb": 11.4321}, "inputs": {"p": 196.85, "s": 102.572},
                                                            "expressions": {"a": "196.85*102.572"}})
    txt = "市值 20,191.2982 百万美元，EV/EBITDA 11.4321× 偏高。\n\n\\[\n196.85 \\times 102.572 = 20{,}191.2982\n\\]\n"
    out, _ = cite.finalize(txt, src)
    body = cite.split_sources(out)[0]
    assert "20{,}191.2982\n" in body and "102.572 =" in body and "\\times 102.572[" not in body
    assert "11.4321×[1]" in body and "20,191.2982[1]" in body
    flagged, _ = cite.flag("算式 $x = 123.456^{2}$ 结果见上。", cite.Sources())
    assert "$x = 123.456^{2}$" in flagged


def test_reverse_dcf_schedule_reproducible():
    from stock_agent import fundamentals as F
    s = F.pv_schedule(100.0, 0.08, 10, 0.09, 0.025)
    assert abs(s["total"] - F._pv(100.0, 0.08, 10, 0.09, 0.025)) < 0.05 and len(s["rows"]) == 10 and "TV" in s["formula"]


# ------------------------------------------------------------------ 第二阶段：可比公司 / 建模
def test_lbo_matches_vals25_rubric():
    """vals-25（EPAM 简化 LBO）：v0.37 发起人股权 7,096（没把超出最低现金的账上现金作来源）、退出股权 15,183。"""
    from stock_agent import finmodel as f
    r = f.run_tool("fin_model", dict(mode="lbo", price=141.00, premium=0.30, shares=55.893, revenue=5457.056, operating_income=520.003, da=124.811,
                                     leverage=5, interest_rate=0.08, transaction_fees=50, existing_debt=25.034, cash=1296.077, min_cash=500,
                                     cash_interest_rate=0.02, growth=0.08, margin_step=0.01, da_pct=0.025, capex_pct=0.01, tax_rate=0.25, years=5, exit_multiple=10))
    assert round(r["sources"]["sponsor_equity"], 2) == 6300.07
    assert round(r["exit"]["exit_ev"], 2) == 13483.54 and round(r["exit"]["exit_equity"], 2) == 13976.13
    assert round(r["returns"]["moic"], 2) == 2.22 and 17.25 <= r["returns"]["irr_pct"] <= 17.29


def test_comps_peer_average_vals09():
    """vals-09：v0.37 三家的倍数都对，但没算 peer average（must_pass）→ 0 分。"""
    from stock_agent import finmodel as f
    r = f.run_tool("fin_model", {"mode": "comps", "metrics": ["pb", "debt_to_equity", "debt_to_capital"], "companies": [
        {"name": "FTAI", "price": 196.85, "shares": 102.572, "equity": 252.396, "debt": 3446.733, "cash": 0},
        {"name": "AER", "market_cap": 1280.0, "equity": 1000.0, "debt": 2380.0, "cash": 0},
        {"name": "WLFC", "price": 135.6399, "shares": 6.814154, "equity": 650.204, "debt": 2239.451, "cash": 0}]})
    assert round(r["stats"]["pb"]["mean"], 2) == 27.57 and round(r["stats"]["debt_to_equity"]["mean"], 2) == 6.49
    assert r["rank_desc"]["debt_to_capital"][0] == "FTAI" and r["stats"]["debt_to_capital"]["median_pct"] == 77.5


def test_comps_ev_multiples_and_implied():
    from stock_agent import finmodel as f
    r = f.comps([{"name": "A", "price": 10, "shares": 100, "debt": 200, "cash": 100, "operating_leases": 50, "ebitda": 110, "revenue": 1000, "net_income": 50, "equity": 400},
                 {"name": "B", "price": 20, "shares": 50, "debt": 0, "cash": 0, "operating_leases": 0, "ebitda": 100, "revenue": 500, "net_income": 80, "equity": 500}],
                include_leases=True, target={"name": "T", "ebitda": 50, "debt": 100, "cash": 20, "shares": 10, "price": 30})
    a = r["rows"][0]
    assert a["ev"] == 1150 and a["ev_ebitda"] == round(1150 / 110, 4) and a["pe"] == 20
    imp = r["implied"]["by_multiple"]["ev_ebitda"]
    assert imp["equity_median"] == round(r["stats"]["ev_ebitda"]["median"] * 50 - 80, 2)


def test_coverage_net_interest_vals10():
    """vals-10：Target 只报净利息费用；v0.37 照旧清单坚持毛利息 → 拒绝给出 Target 的 FCCR。"""
    from stock_agent import finmodel as f
    r = f.run_tool("fin_model", {"mode": "coverage", "companies": [
        {"name": "TGT", "operating_income": 5566, "da": 2981, "rent": 509, "interest": 411},
        {"name": "WMT", "operating_income": 29348, "da": 12973, "rent": 2347, "interest": 2245}]})
    t, w = r["companies"]
    assert (t["ebitdar"], t["fixed_charges"], round(t["fccr"], 2)) == (9056, 920, 9.84)
    assert (w["ebitdar"], w["fixed_charges"], round(w["fccr"], 2)) == (44668, 4592, 9.73)
    assert r["stronger"] == "TGT" and round(r["fccr_diff"], 2) == 0.12


def test_dcf_both_terminal_methods():
    from stock_agent import finmodel as f
    r = f.dcf(dict(base_revenue=75048, growth=75048 / 71043 - 1, ebitda_margin=(7731 + 1687) / 75048, da_pct=1687 / 75048, tax_rate=905 / 5922,
                   capex_pct=0.02, nwc_change_pct=0.0025, deferred_tax=372, other_cf=(-754 - 509 - 140) / 3, wacc=0.08,
                   terminal={"method": "both", "multiple": 10, "growth": 0.025}, debt=21700, cash=4121, shares=231.397796))
    ex, pg = r["results"]["exit_multiple"], r["results"]["perpetual_growth"]
    assert round(r["rows"][0]["ufcf"], 2) == 6821.44 and r["higher_per_share"]["method"] == "perpetual_growth"
    assert abs(ex["per_share"] - 419.24) < 0.01 and abs(pg["per_share"] - 521.75) < 0.01
    assert abs(ex["enterprise_value"] - (r["pv_fcf_sum"] + ex["pv_terminal"])) < 1e-6


def test_fin_model_registered_and_selected(home):
    from stock_agent import toolsel, cite
    from stock_agent.agent import TOOLS
    names = {t["name"] for t in TOOLS}
    assert {"fin_model", "doc_digest", "get_southbound", "hk_interests"} <= names
    assert "model" in toolsel.intent_groups("Create a simplified LBO model for EPAM; calculate IRR and MOIC")
    assert "model" in toolsel.intent_groups("which company is most leveraged relative to the peer set based on price to book value")
    assert "doc" in toolsel.intent_groups("总结伯克希尔最近一封致股东信的要点。")
    assert "flow" in toolsel.intent_groups("腾讯控股南向资金是净买还是净卖？")
    src = cite.Sources()
    out = src.add("fin_model", {"mode": "coverage"}, {"mode": "coverage", "fccr": 9.8435, "formulas": {"fccr": "EBITDAR ÷ fixed charges"}})
    assert out["_cite"] and "计算：FCCR" in src.get(out["_cite"])["name"]


def test_finalize_keeps_code_indexing():
    """盲评 Q32 的真实根因之一：finalize 把代码里的 p[0] 当成无效角标删掉，交付的代码因此出错。"""
    from stock_agent import cite
    src = cite.Sources()
    src.add("get_quotes", {"symbols": ["600519.SS"]}, {"600519.SS": {"date": "2026-10-30", "close": 1392.05, "source": "腾讯证券"}})
    txt = "茅台收盘 1392.05 元。\n\n```python\npeak = prices[0]\nx = arr[1] + arr[2]\n```"
    out, _ = cite.finalize(txt, src)
    assert "peak = prices[0]" in out and "x = arr[1] + arr[2]" in out and "1392.05[1]" in out


def test_gap_gate_only_for_asked_data_and_keeps_full_answer():
    """盲评 v2 预跑（快照 526b9b5）：DCF 题回答里顺带一句「某数没查到」也被打回，模型把完整 DCF 改成了「补充核查后的修正版」。
    现在只按问题问到的数据打回；所有闸门提示都要求保留原回答内容、不提草稿。"""
    from stock_agent import gates, cite, agent
    assert gates.gap_hints("用 DCF 给可口可乐估值，写出完整假设与敏感性表。", "汇率影响的具体收盘价暂未查到。") is None
    assert gates.gap_hints("茅台成交额多少", "成交额暂未查到。")
    for txt in (gates.STALE_NUDGE, gates.GAP_NUDGE, cite.correction_prompt(["1.0"]), agent.GIVE_UP_NUDGE):
        assert "草稿" in txt or "更正说明" in txt


def test_a_share_pb_uses_latest_bps():
    """盲评 Q10：招行市净率用了 Yahoo 的 1.00（真值约 0.93）。A股改为 现价 ÷ 最新一期每股净资产，两源差 >5% 出 warning。"""
    from stock_agent import research
    out = {"valuation": {"市净率": 1.0026}}
    research.pb_mrq(out, 43.55, 46.87, "2026三季报")
    assert out["valuation"]["市净率"] == 0.9292 and out["valuation"]["市净率_Yahoo"] == 1.0026 and "两源不一致" in out["warnings"][0]
