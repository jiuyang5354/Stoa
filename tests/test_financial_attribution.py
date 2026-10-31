import pytest

from stock_agent import cite


def sources():
    s = cite.Sources()
    s.add("get_financials", {"symbol": "AAA"}, {
        "symbol": "AAA", "name": "甲公司", "source": "test", "valuation": {"货币": "CNY", "市盈率TTM": 21.3},
        "main_indicators": [{"报告期": "2026三季报", "营业总收入": "123.4亿", "归母净利润": "12.34亿", "ROE%": 15.2},
                            {"报告期": "2025年报", "营业总收入": "200亿", "归母净利润": "20亿", "ROE%": 16.8}],
    })
    s.add("get_financials", {"symbol": "BBB"}, {
        "symbol": "BBB", "name": "乙公司", "valuation": {"货币": "USD", "市盈率TTM": 30.4},
        "quarterly": [{"period": "2026-09-30", "Total Revenue": "123.4亿", "Net Income": "12.34亿"}],
    })
    return s


@pytest.mark.parametrize("text", [
    "甲公司 2026 年前三季度营业收入 123.4 亿元人民币。",
    "AAA 2025 年全年净利润 20 亿元。",
    "AAA 市盈率 21.3 倍。",
    "BBB 2026 年第三季度营收 123.4 亿美元。",
])
def test_correct_financial_claims(text):
    result = cite.verify(text, sources())
    assert result["ok"], result["why"]


@pytest.mark.parametrize("text", [
    "AAA 市盈率 30.4 倍。",
    "AAA 2026 年全年净利润 12.34 亿元。",
    "甲公司 2026 年前三季度营业收入 123.4 亿美元。",
    "AAA 2026 年前三季度 ROE 16.8%。",
    "AAA 2026 年第三季度净利润 12.34 亿元。",
    "BBB 2026 年前三季度营收 123.4 亿美元。",
])
def test_rejects_cross_company_period_currency_and_basis(text):
    assert not cite.verify(text, sources())["ok"]
