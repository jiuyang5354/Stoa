"""SEC EDGAR 工具 / 历史收盘价 / 电话会文字稿：全部用假的 _fetch 离线跑。"""
import json

import pytest

from stock_agent import agent as ag
from stock_agent import cite
from stock_agent import edgar as E

TICKERS = {"0": {"cik_str": 912593, "ticker": "SUI", "title": "SUN COMMUNITIES INC"},
           "1": {"cik_str": 1637459, "ticker": "KHC", "title": "Kraft Heinz Co"}}

SUB = {"name": "SUN COMMUNITIES INC", "tickers": ["SUI"], "fiscalYearEnd": "1231",
       "filings": {"recent": {
           "form": ["10-Q", "8-K", "8-K", "8-K", "10-K"],
           "filingDate": ["2025-08-05", "2025-07-31", "2025-07-30", "2025-07-23", "2025-02-20"],
           "reportDate": ["2025-06-30", "2025-07-29", "2025-07-30", "2025-07-20", "2024-12-31"],
           "accessionNumber": ["0000912593-25-000230", "0000912593-25-000220", "0000912593-25-000213", "0000912593-25-000199", "0000912593-25-000010"],
           "items": ["", "5.02", "2.02,9.01", "5.02,7.01", ""],
           "primaryDocument": ["sui-20250630.htm", "sui-20250729.htm", "sui-20250730.htm", "sui-20250720.htm", "sui-20241231.htm"],
           "primaryDocDescription": ["10-Q", "8-K", "8-K", "8-K", "10-K"]},
           "files": []}}

INDEX = """<html><div id="formName"><strong>Form 8-K</strong> - Current report</div>
<div class="infoHead">Filing Date</div>
<div class="info">2025-07-30</div>
<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/912593/000091259325000213/sui-20250730.htm">sui-20250730.htm</a></td><td>8-K</td><td>30000</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/912593/000091259325000213/ex991.htm">ex991.htm</a></td><td>EX-99.1</td><td>90000</td></tr>
<tr><td>3</td><td>GRAPHIC</td><td><a href="/Archives/edgar/data/912593/000091259325000213/g1.jpg">g1.jpg</a></td><td>GRAPHIC</td><td>100</td></tr>
</table></html>"""

PRESS = """<html><body><p>Sun Communities Reports Second Quarter Results</p>
<p>2025 Guidance: the Company expects Core FFO per share of $6.51 to $6.67.</p>
<table><tr><td>Reconciliation</td><td></td><td>2025</td><td>2024</td></tr>
<tr><td>Net income</td><td>$</td><td>1,234</td><td>$</td><td>(85</td><td>)</td></tr>
<tr><td>Interest expense</td><td>$</td><td>912</td><td>$</td><td>900</td></tr>
<tr><td>Adjusted EBITDA</td><td>$</td><td>6,363</td><td>$</td><td>6,500</td></tr></table>
</body></html>"""

TENK = """<html><body><p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p><p>FORM 10-K</p>
<p>As of February 10, 2023, there were 186,789,367 shares of the registrant's Common Stock outstanding.</p>
<p>Table of Contents</p><p>Item 1. Business 4</p><p>Item 1A. Risk Factors 12</p><p>Item 7. Management's Discussion and Analysis 40</p><p>Item 8. Financial Statements 60</p>
<p>Item 1. Business</p><p>We develop antibody drug conjugates.</p>
<p>Item 1A. Risk Factors</p><p>Our products face competition. Regulators may delay approval.</p>
<p>Item 7. Management's Discussion and Analysis of Financial Condition</p><p>Total revenues were $1,962.4 million in 2022, driven by Adcetris. Cost of sales rose.</p><p>Liquidity remains strong.</p>
<p>Item 8. Financial Statements</p><p>Balance sheet follows.</p></body></html>"""

FACTS = {"entityName": "Seagen Inc.", "facts": {
    "dei": {"EntityCommonStockSharesOutstanding": {"label": "Entity Common Stock, Shares Outstanding", "units": {"shares": [
        {"end": "2023-02-10", "val": 186789367, "fy": 2022, "fp": "FY", "form": "10-K", "filed": "2023-02-15", "accn": "a1"}]}}},
    "us-gaap": {
        "Revenues": {"label": "Revenues", "units": {"USD": [
            {"start": "2021-01-01", "end": "2021-12-31", "val": 1574000000, "fy": 2021, "fp": "FY", "form": "10-K", "filed": "2022-02-10", "accn": "a0"},
            {"start": "2021-01-01", "end": "2021-12-31", "val": 1574000000, "fy": 2022, "fp": "FY", "form": "10-K", "filed": "2023-02-15", "accn": "a1"},
            {"start": "2022-01-01", "end": "2022-12-31", "val": 1962410000, "fy": 2022, "fp": "FY", "form": "10-K", "filed": "2023-02-15", "accn": "a1"},
            {"start": "2022-10-01", "end": "2022-12-31", "val": 530000000, "fy": 2022, "fp": "FY", "form": "10-K", "filed": "2023-02-15", "accn": "a1"}]}},
        "CashAndCashEquivalentsAtCarryingValue": {"label": "Cash and Cash Equivalents", "units": {"USD": [
            {"end": "2022-12-31", "val": 319940000, "fy": 2022, "fp": "FY", "form": "10-K", "filed": "2023-02-15", "accn": "a1"}]}}}}}

T0 = 1752984000  # 2025-07-20 04:00 UTC 附近
CHART = {"chart": {"result": [{"meta": {"currency": "USD", "symbol": "SUI", "exchangeTimezoneName": "America/New_York", "fullExchangeName": "NYSE"},
                               "timestamp": [1752845400, 1753104600, 1753191000],  # 07-18 / 07-21 / 07-22 09:30 ET
                               "indicators": {"quote": [{"close": [124.0, 125.06, 126.29], "volume": [1, 2, 3]}],
                                              "adjclose": [{"adjclose": [118.6, 119.6, 120.77]}]}}]}}

FOOL_QUOTE = 'x "transcripts/2025/02/12/dutch-bros-bros-q4-2024-earnings-call-transcript/\\" y "transcripts/2025/11/06/dutch-bros-bros-q3-2025-earnings-call-transcript/\\"'
FOOL_T = "<html><body><div class=\"article-body\"><p>Operator: welcome.</p><p>CFO: we expect capital expenditures of $240 million to $260 million in 2025.</p></div><div class=\"article-disclosure\">x</div></body></html>"


@pytest.fixture
def web(monkeypatch):
    calls = []

    def fake(url, kind="json", ttl=3600, params=None):
        calls.append((url, params))
        if url.endswith("company_tickers.json"):
            return TICKERS
        if "search-index" in url and params and "keysTyped" in params:
            return {"hits": {"hits": [{"_id": "1060736", "_source": {"entity": "Seagen Inc."}}]}}
        if "search-index" in url:
            return {"hits": {"total": {"value": 1}, "hits": [{"_id": "0001701605-25-000001:dex991.htm", "_source": {
                "ciks": ["0001701605"], "display_names": ["Baker Hughes Co (BKR)"], "form": "8-K", "file_type": "EX-99.1",
                "file_date": "2025-07-29", "period_ending": "2025-07-28", "items": ["1.01"], "file_description": "PRESS RELEASE"}}]}}
        if "submissions/CIK" in url:
            return SUB
        if url.endswith("-index.htm"):
            return INDEX
        if url.endswith("ex991.htm"):
            return PRESS
        if url.endswith("10k.htm"):
            return TENK
        if "companyfacts" in url:
            return FACTS
        if "frames" in url:
            return {"data": [{"entityName": "A", "cik": 1, "val": 5, "end": "2024-12-31"}, {"entityName": "B", "cik": 2, "val": 50, "end": "2024-12-31"}]}
        if "finance/chart" in url:
            return CHART
        if "fool.com/quote/nasdaq/bros" in url:
            raise RuntimeError("404")
        if "fool.com/quote/nyse/bros" in url:
            return FOOL_QUOTE
        if "fool.com/earnings" in url:
            return FOOL_T
        raise AssertionError(url)

    monkeypatch.setattr(E, "_fetch", fake)
    return calls


def test_cik_lookup_ticker_name_and_cik(web):
    assert E.cik_lookup("NYSE: SUI")["cik"] == "0000912593"
    r = E.cik_lookup("Seagen")
    assert r["cik"] == "0001060736" and r["matches"] and "note" in r
    assert E.cik_lookup("320193")["cik"] == "0000320193"


def test_list_filings_filters_form_date_items(web):
    r = E.list_filings("SUI", forms=["8-K"], start="2025-07-01", end="2025-07-31", items="5.02")
    assert [f["filing_date"] for f in r["filings"]] == ["2025-07-31", "2025-07-23"]
    assert r["filings"][1]["url"].endswith("/912593/000091259325000199/sui-20250720.htm")
    assert r["filings"][1]["index_url"].endswith("0000912593-25-000199-index.htm")


def test_filing_documents_lists_exhibits(web):
    d = E.filing_documents("https://www.sec.gov/Archives/edgar/data/912593/000091259325000213/sui-20250730.htm")
    types = [x["type"] for x in d["documents"]]
    assert types == ["8-K", "EX-99.1", "GRAPHIC"]
    assert d["documents"][0]["url"] == "https://www.sec.gov/Archives/edgar/data/912593/000091259325000213/sui-20250730.htm"
    assert d["filing_date"] == "2025-07-30"


def test_html_tables_keep_rows_and_negatives():
    t = E.html_to_text(PRESS)
    assert "Net income | 1,234 | (85)" in t
    assert "Adjusted EBITDA | 6,363 | 6,500" in t


def test_read_document_cover_section_query_and_paging(web):
    u = "https://www.sec.gov/Archives/edgar/data/1060736/000106073623000009/10k.htm"
    c = E.read_document(u, section="cover")
    assert any("186,789,367" in s for s in c["shares_outstanding_statements"])
    m = E.read_document(u, section="mdna")
    assert m["section"] == "Item 7" and "1,962.4" in m["text"] and "Balance sheet" not in m["text"]  # 跳过目录，止于 Item 8
    r = E.read_document(u, section="risk")
    assert "competition" in r["text"] and "antibody" not in r["text"]
    q = E.read_document(u, query="total revenues Adcetris")
    assert any("Adcetris" in p["text"] for p in q["passages"])
    p1 = E.read_document(u, max_chars=100)
    p2 = E.read_document(u, max_chars=100, start=p1["next_start"])
    assert p1["text"] != p2["text"] and p1["next_start"] == 100
    assert "error" in E.read_document("https://example.com/x.htm")


def test_xbrl_facts_dedupes_and_filters(web):
    r = E.xbrl_facts("Seagen", ["us-gaap:Revenues", "dei:EntityCommonStockSharesOutstanding", "us-gaap:Nope"], annual_only=True)
    rev = r["facts"]["us-gaap:Revenues"]["values"]
    assert [v["val"] for v in rev] == [1962410000, 1574000000]  # 上年数重复报告只留一条，单季被 annual_only 去掉
    assert rev[0]["days"] == 364
    assert r["facts"]["dei:EntityCommonStockSharesOutstanding"]["values"][0]["val"] == 186789367
    assert "error" in r["facts"]["us-gaap:Nope"]
    assert "warning" in r["facts"]["us-gaap:Revenues"]  # 很久没再报告的科目提示换科目
    e = E.xbrl_facts("Seagen", ["Revenues"], end="2022")
    assert {v["end"][:4] for v in e["facts"]["us-gaap:Revenues"]["values"]} == {"2022"}
    s = E.xbrl_facts("Seagen", search="cash equivalents")
    assert s["matches"][0]["concept"] == "us-gaap:CashAndCashEquivalentsAtCarryingValue"


def test_frames_and_full_text_search(web):
    f = E.xbrl_frames("Revenues", "CY2024")
    assert f["data"][0]["company"] == "B" and f["concept"] == "us-gaap:Revenues"
    s = E.full_text_search('"Chart Industries"', forms=["8-K"], start="2025-07-20", end="2025-08-10")
    assert s["hits"][0]["url"] == "https://www.sec.gov/Archives/edgar/data/1701605/000170160525000001/dex991.htm"
    q = [p for u, p in web if "search-index" in u][-1]
    assert q["dateRange"] == "custom" and q["forms"] == "8-K"


def test_earnings_release_picks_item_202_exhibit(web):
    r = E.earnings_release("SUI", date="2025-07-29", query="guidance Core FFO")
    assert r["filing_date"] == "2025-07-30" and r["exhibit"] == "EX-99.1"
    assert any("6.51" in p["text"] for p in r["passages"])
    full = E.earnings_release("SUI", start="2025-07-01", end="2025-08-31")
    assert "Adjusted EBITDA | 6,363" in full["text"]
    none = E.earnings_release("SUI", start="2025-02-01", end="2025-02-28")
    assert "error" in none


def test_price_history_dates_and_non_trading_day(web):
    p = E.price_history("NYSE: SUI", dates=["2025-07-21", "2025-07-20"], start="2025-07-18", end="2025-07-22")
    d = {x["date"]: x for x in p["dates"]}
    assert d["2025-07-21"]["close"] == 125.06 and d["2025-07-21"]["trading_day"]
    assert not d["2025-07-20"]["trading_day"]
    assert d["2025-07-20"]["previous_trading_day"]["date"] == "2025-07-18" and d["2025-07-20"]["next_trading_day"]["close"] == 125.06
    assert p["range"]["change_pct"] == round((126.29 / 124.0 - 1) * 100, 4)
    assert "error" in E.price_history("SUI")


def test_transcripts_list_and_read(web):
    lst = E.transcripts("NYSE:BROS")
    assert [x["quarter"] for x in lst["available"]] == ["Q3 2025", "Q4 2024"]
    t = E.transcripts("BROS", "Q4 2024", query="capital expenditures")
    assert "240" in json.dumps(t["passages"])
    assert "error" in E.transcripts("BROS", "Q1 2019")


def test_agent_registers_tools_and_cites(web, tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    names = {t["name"] for t in ag.TOOLS}
    assert ag.SEC_TOOLS <= names and len(names) == len(ag.TOOLS)
    for t in ag.TOOLS:
        if t["name"] in ag.SEC_TOOLS:
            assert len(t["description"]) < 500
    res = E.run_tool("price_history", {"symbol": "SUI", "dates": ["2025-07-21"]})
    src = cite.Sources()
    out = src.add("price_history", {"symbol": "SUI"}, res)
    assert out["_cite"] and "历史行情" in src.items[0]["name"]


def test_guess_lang():
    assert ag._guess_lang("For NASDAQ: CRWD, what does management say about AI tailwinds?") == "en"
    assert ag._guess_lang("茅台今天怎么样") == "zh"
    assert ag._guess_lang("AAPL") == "zh"


def test_price_history_ohlc_stats_and_symbols(monkeypatch):
    assert E._yahoo_symbol("DXY") == "DX-Y.NYB" and E._yahoo_symbol("DX-Y.NYB") == "DX-Y.NYB"
    assert E._yahoo_symbol("BRK.B") == "BRK-B" and E._yahoo_symbol("600519") == "600519.SS" and E._yahoo_symbol("700.HK") == "0700.HK"
    import datetime as dt
    days = [dt.date(2020, 1, 6) + dt.timedelta(days=i) for i in range(14)]
    days = [d for d in days if d.weekday() < 5]
    closes = [100, 101, 102, 103, 104, 90, 91, 92, 93, 94]
    rows = [{"date": d.isoformat(), "open": c, "high": c + 1, "low": c - 1, "close": c, "adj_close": c, "volume": 1} for d, c in zip(days, closes)]
    monkeypatch.setattr(E, "_yahoo_daily", lambda s, a, b: ([dict(r) for r in rows], {"symbol": "X", "splits": [{"date": "2020-01-13", "ratio": 2.0}]}))
    monkeypatch.setattr(E, "_fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    r = E.price_history("X", start="2020-01-06", end="2020-01-17")
    assert r["range"]["max_weekly_drop"]["change_pct"] == round((94 / 104 - 1) * 100, 4)
    assert r["range"]["highest_high"]["raw_high"] == 210.0  # 拆股前实际价
    d = E.price_history("X", dates=["2020-01-14"])["dates"][0]
    assert d["open"] == 91 and d["change_pct"] == round((91 / 90 - 1) * 100, 4)


def test_macro_tool_registered():
    from stock_agent import macro
    assert "macro_series" in {t["name"] for t in ag.TOOLS}
    assert "error" in macro.run_tool("macro_series", {})
