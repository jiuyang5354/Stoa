"""v0.38 回答前的三道闸（规则判断，触发时打回模型一次，让它用工具补，不额外调模型做判断）。

1. stale_facts   时效敏感事实（人事/职位/在任、「最新」公告）没在本轮联网核对就写了 → 要求先 web_search / 公告核对再写（盲评 Q31 巴菲特董事长过时）。
2. code_issues   回答里的 Python 代码先编译；开了代码执行（ALLOW_CODE=1）再在沙箱里真跑一遍（只跑定义 + 示例，60 秒内）；
                 语法错误 / 运行报错（缺数据文件、缺第三方包、联网不算）→ 带报错打回改正（盲评 Q32）。
3. gap_hints     回答停在「〔未知〕/暂未查到/无法确认」→ 按缺的是什么给具体的公开备用来源（盲评 Q1 成交额、Q2 南向资金、Q8 恒生科技指数、Q12 港股减持）。
"""
from __future__ import annotations

import ast
import re

WEB_TOOLS = {"web_search", "fetch_url", "doc_digest", "get_news", "get_announcements", "sec_filings", "sec_document", "sec_search", "earnings_release",
             "regulatory_letters", "hk_interests", "get_corporate_events", "get_insiders", "read_filing"}

ROLE = re.compile(r"(?:董事长|董事会主席|名誉董事长|副董事长|首席执行官|总裁|总经理|行长|首席财务官|CEO|CFO|COO|主席|掌门|接班|继任|退休|卸任|辞任|离任|"
                  r"chair(?:man|woman|person)?|chief executive|president|successor|step(?:s|ped)? down|retir(?:e|ed|es|ement))", re.I)
CLAIM = re.compile(r"(?:仍|继续|依然|现任|目前|已|将|担任|出任|接任|改任|转任|兼任|由.{1,12}(?:担任|接任)|is (?:still |now )?(?:the )?|remains?|became|will (?:become|remain)|"
                   r"serves? as|succeeded|took over|named)", re.I)
STALE_NUDGE = ("[系统：回答里写到了人事/职位/在任情况（{hit}），这类事实变化快，不能凭模型记忆或旧文件写。还有工具轮数：先用 web_search（带年份/「最新」/英文名）"
               "或公司公告、SEC 8-K item 5.02 核对当前情况，确认后再写；查不到最新信息就明确写「截至某日的公开信息」。然后给出完整的最终回答：保留原回答里仍然成立的全部内容（结构、表格、计算），只改需要核对的那几句；不要写成「修正版/补充说明」，不要提到之前的草稿、「上一条」或这条提示。]")


ROLE_CHECK_TOOLS = {"get_announcements", "sec_filings", "regulatory_letters", "hk_interests", "get_insiders"}


def stale_facts(question: str, reply: str, used: set, queries=()) -> str | None:
    """回答里有「X 继续担任董事长」这类时效敏感断言，但本轮没有专门核对过人事 → 返回打回提示。
    「核对过」= 查过公告/SEC 申报，或某次 web_search / 新闻检索的关键词里就有职位词（董事长/CEO/chairman…）。
    只读了一份旧文件（如 2 月的股东信）不算：信里写的「仍任董事长」到今天可能已经变了（盲评 Q31）。"""
    if used & ROLE_CHECK_TOOLS or any(ROLE.search(q or "") for q in queries):
        return None
    body = re.split(r"\n—— 来源 ——|\n-- Sources --", reply or "")[0]
    for sent in re.split(r"(?<=[。！？!?\n])|(?<=\.)\s", body):
        m = ROLE.search(sent)
        if m and CLAIM.search(sent):
            return STALE_NUDGE.format(hit=sent.strip()[:60])
    return None


CODE_RE = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.S | re.I)
CODE_ASK = re.compile(r"代码|python|写一段|写个|函数|脚本|程序|实现|script|function|code|snippet", re.I)
SOFT_ERR = re.compile(r"ModuleNotFoundError|ImportError|FileNotFoundError|No such file|ConnectionError|URLError|HTTPError|getaddrinfo|Temporary failure|"
                      r"代码执行未开启|未执行代码|workspace 不能", re.I)
CODE_NUDGE = ("[系统：回答里的 Python 代码{how}报错，不能这样交给用户：\n{err}\n请修正代码（保持原有功能，必要时加最小示例数据让它能直接运行），"
              "给出改好的完整代码；不要提到之前的版本或这条系统提示。]")


def code_blocks(reply: str) -> list[str]:
    return [b for b in CODE_RE.findall(reply or "") if b.strip()]


def check_code(code: str, run=None) -> str | None:
    """编译 → （可选）运行。返回需要打回的错误文字，没问题返回 None。run(code)->输出文字。"""
    try:
        ast.parse(code)
    except SyntaxError as e:
        line = (e.text or "").rstrip()
        return f"SyntaxError: {e.msg}（第 {e.lineno} 行：{line[:80]}）"
    if run is None:
        return None
    try:
        out = run(code) or ""
    except Exception as e:  # 沙箱本身出问题不算代码错
        return None
    if "Traceback (most recent call last)" in out or re.search(r"^\w+Error: ", out, re.M):
        tail = out.strip().splitlines()[-6:]
        txt = "\n".join(tail)
        if SOFT_ERR.search(txt):
            return None
        return txt[:600]
    return None


def code_issues(question: str, reply: str, run=None) -> str | None:
    if not CODE_ASK.search(question or "") and "```python" not in (reply or "").lower():
        return None
    for b in code_blocks(reply):
        err = check_code(b, run)
        if err:
            return CODE_NUDGE.format(how="在试运行时" if run and not err.startswith("SyntaxError") else "编译时", err=err)
    return None


def sandbox_runner():
    """开了代码执行（ALLOW_CODE=1）才真跑；否则只做编译检查。"""
    from . import sandbox
    if not sandbox.enabled():
        return None
    return lambda code: sandbox.run_python(code, timeout=60)


# ---------------------------------------------------------------- 缺数据 → 具体备用来源
GAP = re.compile(r"〔未知〕|\[Unknown\]|暂未查到|未查到|没有查到|查不到|未能(?:取得|获取|查到|核实)|无法(?:确认|核实|给出|取得|获取)|暂无(?:可靠)?数据|缺少数据|"
                 r"not (?:available|found|disclosed)|could not (?:find|obtain)", re.I)
HINTS = [
    (r"成交额|成交金额|turnover|成交量", "成交额/成交量 → get_quotes 结果里的 turnover（腾讯行情：A股元、港股港元、美股美元）"),
    (r"南向|港股通|southbound", "南向资金 → get_southbound（每日净买入仍披露；给港股代码另有该股港股通净买入）"),
    (r"恒生科技|HSTECH|恒生指数|国企指数|HSCEI|指数.{0,6}(?:年初|今年|YTD)|年初至今|今年以来", "指数本身的区间涨跌 → price_history（^HSTECH / ^HSI / ^HSCE 走腾讯指数日线，start=上年末交易日）；不要用 ETF 价格近似"),
    (r"减持|增持|大股东|权益披露|董事.{0,4}(?:买|卖)|substantial shareholder", "港股大股东/董事增减持 → hk_interests（港交所披露易）；A股 → get_corporate_events(kinds=[\"holder\"]) + get_announcements"),
    (r"行业|板块", "行业板块涨幅/资金 → get_market_mood（东财 502 时自动换新浪）"),
    (r"市净率|市盈率|PB|PE|股息率", "估值指标 → get_financials（双源 unified），口径不一致时写明两源数值"),
    (r"股东信|致股东|letter|年报全文|研报全文", "长文档 → web_search 找官网 PDF，再 doc_digest 读全文"),
    (r"收盘价|股价|close", "历史收盘 → price_history；美股已退市/被收购代码会改查 SEC 合并委托书里的收盘价"),
]
GAP_NUDGE = ("[系统：回答里有没查到就停下的地方（{hit}）。还有工具轮数，先换公开来源补齐，并写明口径与出处：\n{hints}\n"
             "补到了就给具体数值；确实没有公开来源再写「未知」，并说明查过哪些来源。最后给出完整的最终回答：保留原回答里仍然成立的全部内容，只补上缺的数据；不要写成「修正版/补充核查」，不要写「上一条」「此前写成」这类提到草稿或这条提示的话。]")


def gap_hints(question: str, reply: str) -> str | None:
    body = re.split(r"\n—— 来源 ——|\n-- Sources --", reply or "")[0]
    m = GAP.search(body)
    if not m:
        return None
    # 只按「问题问到的数据」给备用来源：回答里顺带提到的缺口（如 DCF 题里说某个次要数没查到）不打回，免得把完整回答改成「补充核查版」
    hs = [h for pat, h in HINTS if re.search(pat, question or "", re.I)]
    if not hs:
        return None
    s = max(0, m.start() - 20)
    return GAP_NUDGE.format(hit=body[s:m.end() + 20].replace("\n", " ").strip(), hints="\n".join("- " + h for h in hs[:4]))
