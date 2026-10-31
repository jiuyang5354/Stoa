"""v0.37 sweep_filings：N 家 × M 年 × 同一章节一轮出矩阵（用 tests/fixtures/annual_2024.pdf / annual_2025.pdf 造两公司两年，离线）。"""
import json
import time
from pathlib import Path

import pytest

from stock_agent import context, sweep
from stock_agent import filings as fl

FX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    monkeypatch.setenv("SWEEP_PROCESSES", "0")


def offline(monkeypatch, fail=(), delay=0.0, counter=None):
    """两家公司（600519 / 000858）× 2024、2025 用同两份 PDF fixture；fail 里的 (代码, 年) 下载失败。"""
    def locate(symbol, year, kind="annual"):
        code = symbol[:6]
        if (code, year) in fail or year not in (2024, 2025):
            return {"error": f"找不到 {code} {year} 年报"}
        name = {"600519": "示例茅台", "000858": "示例五粮"}[code]
        return {"title": f"{name}{year}年年度报告", "time": f"{year + 1}-04-01", "url": f"https://example.com/{code}_{year}.pdf", "source": "巨潮资讯", "format": "pdf"}

    def download(url, fmt):
        if counter is not None:
            counter.append(url)
        time.sleep(delay)
        year = int(url.rsplit("_", 1)[1].split(".")[0])
        return (FX / f"annual_{year}.pdf").read_bytes()
    monkeypatch.setattr(fl, "locate", locate)
    monkeypatch.setattr(fl, "_download", download)


SYMS = ["600519.SS", "000858.SZ"]


def test_matrix_columns_rows_and_numbers(monkeypatch):
    offline(monkeypatch)
    r = sweep.sweep_filings(SYMS, [2024, 2025], "mdna", ["营业收入", "归母净利润", "毛利率", "渠道"])
    assert r["columns"] == ["代码", "名称", "年份", "营业收入", "归母净利润", "毛利率", "渠道", "页码(按列)", "缺失"]
    assert len(r["rows"]) == 4 and r["coverage"] == {"requested": 4, "fetched": 4, "failed": [], "skipped": []}
    row = next(x for x in r["rows"] if x[0] == "600519.SS" and x[2] == 2025)
    assert row[1] == "示例茅台"
    assert row[3] == "1234.56亿元" and row[4] == "456.78亿元" and row[5] == "93.50%"
    assert row[6].startswith("「") and "直销" in row[6]  # 规则摘句，原文
    assert row[7] == "p.2/2/3/3"


def test_revenue_caliber_is_literal(monkeypatch):
    offline(monkeypatch)
    r = sweep.sweep_filings(["600519.SS"], [2025], "financial", ["营业总收入", "营收"])
    row = r["rows"][0]
    assert row[3] == "" and "营业总收入" in row[-1]  # 年报里只有「营业收入」：不拿它冒充营业总收入，宁少勿乱
    assert row[4] == "1234.56亿元" and r["caliber"] == ["营收取「营业收入」"]


def test_single_failure_goes_to_coverage(monkeypatch):
    offline(monkeypatch, fail={("000858", 2024)})
    r = sweep.sweep_filings(SYMS, [2024, 2025], "risk", ["风险"])
    assert len(r["rows"]) == 3 and r["coverage"]["fetched"] == 3
    assert r["coverage"]["failed"] and r["coverage"]["failed"][0].startswith("000858.SZ 2024")


def test_max_docs_caps_and_reports_skipped(monkeypatch):
    offline(monkeypatch)
    r = sweep.sweep_filings(SYMS, [2024, 2025], "mdna", ["营业收入"], max_docs=3)
    assert len(r["rows"]) == 3 and r["coverage"]["skipped"] == ["000858.SZ 2025"] and "max_docs" in r["warnings"][0]


def test_parallel_and_cache(monkeypatch):
    calls = []
    offline(monkeypatch, delay=0.3, counter=calls)
    t0 = time.time()
    sweep.sweep_filings(SYMS, [2024, 2025], "mdna", ["营业收入"])
    assert time.time() - t0 < 0.3 * 4 * 0.75  # 并行：明显快于串行 4 × 0.3 s
    n = len(calls)
    sweep.sweep_filings(SYMS, [2024, 2025], "mdna", ["营业收入"])
    assert len(calls) == n  # 第二次全部走缓存


class QuoteLLM:
    def __init__(self, quote):
        self.quote, self.calls = quote, 0

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        return {"role": "assistant", "content": json.dumps({"渠道": self.quote}, ensure_ascii=False)}


def test_llm_quote_must_be_verbatim(monkeypatch):
    offline(monkeypatch)
    good = QuoteLLM("直销收入占比提升")
    r = sweep.sweep_filings(["600519.SS"], [2025], "mdna", ["渠道"], llm=good)
    assert r["rows"][0][3] == "「直销收入占比提升」" and good.calls == 1 and "模型逐字摘句" in r["extract_mode"]
    bad = QuoteLLM("公司渠道库存处于历史低位")  # 原文里没有：不采用，退回规则摘句
    r = sweep.sweep_filings(["600519.SS"], [2024], "mdna", ["渠道"], llm=bad)
    assert "历史低位" not in r["rows"][0][3] and "直销" in r["rows"][0][3]


def test_cite_binds_rows_and_keeps_context_small(monkeypatch):
    from stock_agent import cite
    offline(monkeypatch)
    r = sweep.sweep_filings(SYMS, [2024, 2025], "mdna", ["营业收入", "毛利率", "渠道"])
    src = cite.Sources()
    out = src.add("sweep_filings", {}, r)
    assert "docs" not in out and out["columns"][-1] == "出处" and all(str(x[-1]).startswith("[") for x in out["rows"])
    assert any("第 2 页" in it["name"] and it["url"].endswith("#page=2") for it in src.items)
    v = cite.verify("示例茅台 2025 年营业收入 1234.56 亿元，毛利率 93.50%。", src)
    assert v["ok"], v
    assert context.est_tokens(context.clip_tool(out, 9000)) < 1200  # 4 份；24 份按比例也远低于 3,000


def test_24_docs_one_call_context_budget(monkeypatch):
    """8 家 × 3 年一轮完成：24 行，主上下文增量 < 3,000 token。"""
    from stock_agent import cite
    offline(monkeypatch)
    codes = [f"{600000 + i}.SS" for i in range(8)]
    monkeypatch.setattr(fl, "locate", lambda s, y, kind="annual": {"title": f"公司{s[:6]}{y}年年度报告", "time": f"{y + 1}-04-01",
                                                                  "url": f"https://example.com/{s[:6]}_{2024 if y < 2025 else 2025}_{y}.pdf", "source": "巨潮资讯", "format": "pdf"})
    monkeypatch.setattr(fl, "_download", lambda url, fmt: (FX / f"annual_{url.rsplit('_', 2)[1]}.pdf").read_bytes())
    r = sweep.sweep_filings(codes, [2023, 2024, 2025], "mdna", ["营收", "毛利率", "渠道库存表述"], max_docs=24)
    assert len(r["rows"]) == 24 and r["coverage"]["fetched"] == 24
    out = cite.Sources().add("sweep_filings", {}, r)
    assert context.est_tokens(context.clip_tool(out, 9000)) < 3000


def test_export_xlsx(monkeypatch):
    offline(monkeypatch)
    r = sweep.sweep_filings(SYMS, [2025], "mdna", ["营业收入"], export=True)
    assert r["markdown_link"].startswith("[") and ".xlsx" in r["markdown_link"]


def test_agent_routes_and_groups(monkeypatch):
    from stock_agent import toolsel
    from stock_agent.agent import TOOLS
    assert "sweep_filings" in {t["name"] for t in TOOLS}
    assert "sweep_filings" in {t["name"] for t in toolsel.select("8家白酒公司过去三年年报MD&A里怎么描述渠道库存", TOOLS)[0]}


def test_process_warmup_falls_back_cleanly(monkeypatch):
    """多进程预热（首次解析）：子进程拿不到测试替身时（Windows spawn）照样退回线程，结果一致。"""
    offline(monkeypatch)
    monkeypatch.setenv("SWEEP_PROCESSES", "auto")
    r = sweep.sweep_filings(SYMS, [2024, 2025], "mdna", ["营业收入"])
    assert len(r["rows"]) == 4 and all(x[3] == "1234.56亿元" for x in r["rows"])
