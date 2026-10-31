from datetime import datetime

from stock_agent import poster
from stock_agent.report import CN_TZ

SNAP = {"indices": {"A股": [{"symbol": "000001.SS", "name": "上证", "data": {"close": 3000, "chg_pct": 0.5, "date": "2026-10-30"}},
                            {"symbol": "000300.SS", "name": "沪深300", "data": {"close": 4800, "chg_pct": 0.8, "date": "2026-10-30"}}],
                    "港股": [{"symbol": "^HSI", "name": "恒生指数", "data": {"close": 27000, "chg_pct": 1.3, "date": "2026-10-30"}}],
                    "美股": [{"symbol": "^GSPC", "name": "标普500", "data": {"close": 7000, "chg_pct": 0.3, "date": "2026-10-29"}}]},
        "watchlist": [{"symbol": "0700.HK", "name": "腾讯<b>", "data": {"close": 600, "chg_pct": -3.5, "chg_20d": -6, "signals": ["单日大跌", "跌破 20 日线"]}},
                      {"symbol": "NVDA", "name": "英伟达", "data": {"close": 200, "chg_pct": 0.5, "signals": []}}]}


def test_data_and_html():
    d = poster.data(SNAP, "evening", None, {"open": 2, "closed": 0}, datetime(2026, 10, 30, 21, tzinfo=CN_TZ))
    assert d["roman"] == "XXX · X · MMXXVI" and "星期五" in d["date"]
    assert [i["name"] for i in d["indices"]] == ["沪深300", "恒生指数", "标普500"]  # A股优先沪深300
    assert d["headline"].startswith("三地齐涨，港股领涨。先看风险：腾讯")
    assert len(d["risks"]) == 1 and d["asof"] == "2026-10-30"
    h = poster.html(d)
    assert "腾讯&lt;b&gt;" in h and "<em>先看风险</em>" in h and "还没有到期结算" in h and "存为图片" in h
    assert "{{" not in h and "存为图片" not in poster.html(d, save=False)


def test_headline_from_ai_and_calm():
    d = poster.data({"indices": SNAP["indices"], "watchlist": []}, "morning", "**港股**科技反弹[1]，量能一般。其余略。", {})
    assert d["headline"] == "港股科技反弹，量能一般。" and d["risks"] == []
    assert "没有触发" in poster.html(d)
