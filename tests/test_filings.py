"""年报解析：小样本 PDF / HTML fixture 离线测试（抽字带页码、章节定位、BM25 选段、两年对比、依赖缺失降级、页码引用）。"""
from pathlib import Path

import pytest

from stock_agent import cite
from stock_agent import filings as fl

FX = Path(__file__).parent / "fixtures"

SEC_HTML = """<html><body>
<div>NVIDIA CORP FORM 10-K</div><div>Table of Contents</div>
<div>Item 1. Business ..... 4</div><div>Item 1A. Risk Factors ..... 12</div><div>Item 1B. Unresolved Staff Comments ..... 30</div>
<div>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations ..... 36</div><div>Item 8. Financial Statements ..... 45</div>
<hr style="page-break-after:always"/>
<div>Item 1. Business</div><p>We pioneered accelerated computing. Our data center platform serves cloud providers.</p>
<hr style="page-break-after:always"/>
<div>Item 1A. Risk Factors</div><p>Increasing use of economic sanctions and export controls has impacted and may in the future impact demand for our products.</p>
<p>Competition could adversely impact our market share and financial results.</p>
<hr style="page-break-after:always"/>
<p>Adverse economic conditions may harm our business. Supply constraints could limit our revenue growth.</p>
<div>Item 1B. Unresolved Staff Comments</div><p>None.</p>
<hr style="page-break-after:always"/>
<div>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</div>
<p>Revenue for fiscal year 2026 was $215.9 billion, up 65% from a year ago, driven by Data Center compute.</p>
<hr style="page-break-after:always"/>
<div>Item 8. Financial Statements and Supplementary Data</div><p>See consolidated statements of income.</p>
</body></html>"""


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


def offline(monkeypatch, files: dict):
    """locate/_download 换成本地 fixture：files = {(year): (path or html, fmt)}。"""
    def locate(symbol, year, kind="annual"):
        if year not in files:
            return {"error": f"找不到 {year}"}
        src, fmt = files[year]
        return {"title": f"示例公司{year}年年度报告", "time": f"{year + 1}-04-01", "url": f"https://example.com/{year}.{fmt}", "source": "测试源", "format": fmt}

    def download(url, fmt):
        year = int(url.rsplit("/", 1)[1].split(".")[0])
        src, _ = files[year]
        return src.encode() if fmt == "html" else Path(src).read_bytes()

    monkeypatch.setattr(fl, "locate", locate)
    monkeypatch.setattr(fl, "_download", download)


# ---------------------------------------------------------------- PDF
def test_pdf_pages_keep_page_numbers_and_chinese():
    pages = fl.pdf_pages((FX / "annual_2025.pdf").read_bytes())
    assert len(pages) == 4
    assert "可能面对的风险" in pages[3] and "舆情风险" in pages[3]
    assert not any(fl.garbled(p) for p in pages)


def test_sections_skip_toc_and_stop_at_next_chapter():
    pages = fl.pdf_pages((FX / "annual_2025.pdf").read_bytes())
    assert fl.is_toc(pages[0]) and not fl.is_toc(pages[2])
    sec = fl.find_sections(pages)
    assert sec["mdna"][0] == 3  # 目录页（第 1 页）里的「第三节 管理层讨论与分析」不算
    assert sec["risk"][:1] == [4] and sec["financial"][0] == 2 and sec["segment"][0] == 3
    txt = "".join(t for _, t in fl.span_pages(pages, *sec["risk"]))
    assert "舆情风险" in txt and "公司治理结构完善" not in txt and "(五)其他" not in txt


def test_read_filing_returns_pages_and_pdf_page_links(monkeypatch):
    offline(monkeypatch, {2025: (str(FX / "annual_2025.pdf"), "pdf")})
    r = fl.read_filing("600519.SS", 2025, "risk")
    assert not r.get("error"), r
    assert r["section"] == "risk" and r["section_pages"][0] == 4
    p = r["passages"][0]
    assert p["page"] == 4 and p["url"].endswith("#page=4") and "环境保护风险" in p["text"]
    r2 = fl.read_filing("600519.SS", 2025, "mdna", "直销")
    assert r2["passages"][0]["page"] == 3 and "直销" in r2["passages"][0]["text"]
    # 第二次读缓存，不再下载
    monkeypatch.setattr(fl, "_download", lambda *a: (_ for _ in ()).throw(AssertionError("不该再下载")))
    assert fl.read_filing("600519.SS", 2025, "financial")["passages"][0]["page"] == 2
    assert (fl.filings_dir() / "A_600519" / "2025_annual.json").exists()


def test_compare_filings_added_removed(monkeypatch):
    offline(monkeypatch, {2024: (str(FX / "annual_2024.pdf"), "pdf"), 2025: (str(FX / "annual_2025.pdf"), "pdf")})
    r = fl.compare_filings("600519.SS", 2024, 2025, "risk")
    assert not r.get("error"), r
    added = " ".join(x["text"] for x in r["added"])
    removed = " ".join(x["text"] for x in r["removed"])
    changed = " ".join(f"{x['before']}→{x['after']}" for x in r["modified"])
    # 「三是食品安全风险」→「三是舆情风险」句式相同，算改写；「四是环境保护风险」是全新的一句
    assert "环境保护风险" in added and "宏观经济风险" not in added + changed
    assert "食品安全风险" in changed and "舆情风险" in changed
    assert r["added"][0]["url"].startswith("https://example.com/2025") and "#page=4" in r["added"][0]["url"]
    assert r["modified"][0]["page_a"] == 4 and r["counts"]["added"] >= 1 and r["counts"]["modified"] >= 1


def test_diff_sentences_treats_number_updates_as_modified():
    a = [{"page": 1, "text": "营业收入 1,000.00 亿元，同比增长 8.80%。"}, {"page": 1, "text": "公司面临原材料价格波动风险。"}]
    b = [{"page": 2, "text": "营业收入 1,234.56 亿元，同比增长 9.10%。"}, {"page": 2, "text": "公司面临出口管制风险。"}]
    d = fl.diff_sentences(a, b)
    assert len(d["modified"]) == 1 and "1,234.56" in d["modified"][0]["after"]
    assert any("出口管制" in x["text"] for x in d["added"]) or any("出口管制" in x["after"] for x in d["modified"])


def test_missing_pdf_dependency_degrades(monkeypatch):
    offline(monkeypatch, {2025: (str(FX / "annual_2025.pdf"), "pdf")})
    monkeypatch.setattr(fl, "pdf_backend", lambda: None)
    r = fl.read_filing("600519.SS", 2025, "risk")
    assert r.get("error") and "pypdf" in r["error"] and r["url"].startswith("https://example.com/2025")
    assert r.get("missing_dependency") == "pypdf"


def test_garbled_detection():
    assert fl.garbled("ͦ\u137d\n2 ʮ̡༟ࣘ\n3 ৌਕ฿ࠅ\n4 ˴ࢩజѓ ၍ଣᄴীሞʿʱؓ \u0ee8ԫึజѓ Άุ၍طజѓ ዹͭࣨᅰࢪజѓ ၝΥϗूڌ ၝΥΌࠦϗूڌ")
    assert not fl.garbled("管理層討論及分析 收入。截至二零二五年十二月三十一日止年度的收入同比增長 14% 至人民幣 7,518 億元。" * 3)
    assert not fl.garbled("Revenue for fiscal year 2026 was $215.9 billion" * 5)


# ---------------------------------------------------------------- SEC HTML
def test_sec_html_pages_and_items(monkeypatch):
    pages = fl.html_pages(SEC_HTML)
    assert len(pages) == 6 and fl.is_toc(pages[0])
    sec = fl.find_sections(pages)
    assert sec["risk"][0] == 3 and sec["mdna"][0] == 5 and sec["financial"][0] == 6
    txt = "".join(t for _, t in fl.span_pages(pages, *sec["risk"]))
    assert "Supply constraints" in txt and "None." not in txt  # 到 Item 1B 为止
    offline(monkeypatch, {2026: (SEC_HTML, "html")})
    r = fl.read_filing("NVDA", 2026, "risk", "export controls")
    assert "export controls" in r["passages"][0]["text"] and "#:~:text=" in r["passages"][0]["url"]


def test_bm25_prefers_matching_chunk():
    docs = [fl.tokens("公司治理结构完善，董事会运作规范。"), fl.tokens("出口管制和关税变化可能影响海外收入。"), fl.tokens("营业收入同比增长。")]
    sc = fl.BM25(docs).score(fl.tokens("出口管制"))
    assert sc.index(max(sc)) == 1


def test_norm_section_aliases():
    assert fl.norm_section("风险因素") == "risk" and fl.norm_section("MD&A") == "mdna" and fl.norm_section("分产品") == "segment"
    assert fl.norm_section(None) == "all"


# ---------------------------------------------------------------- 引用：页码角标
def test_page_cites_link_to_pdf_page(monkeypatch):
    offline(monkeypatch, {2025: (str(FX / "annual_2025.pdf"), "pdf")})
    r = fl.read_filing("600519.SS", 2025, "risk")
    src = cite.Sources()
    res = src.add("read_filing", {"symbol": "600519.SS"}, r)
    assert res["passages"][0]["_cite"]
    text, used = cite.finalize("年报第 4 页列出四类风险：宏观经济、安全、舆情、环境保护。", src)
    assert "第 4 页[1]" in text and used[0]["url"].endswith("#page=4")
