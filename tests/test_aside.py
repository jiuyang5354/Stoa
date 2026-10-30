def test_headlines_filters_by_title(monkeypatch):
    from stock_agent import serve, research
    serve._HEAD.update(ts=0, data=None)

    def fake(kw, n):
        return [{"time": "2026-10-30 10:00", "title": t, "source": "s", "url": "u" + t} for t in
                (f"{kw}三大指数收涨", f"{kw}午评", f"{kw}热点", "化工ETF吸金", "某基金分红")]
    monkeypatch.setattr(research, "_em_news", fake)
    d = serve.headlines()
    assert set(d["items"]) == {"A", "HK", "US"}
    assert all("ETF" not in x["title"] for x in d["items"]["US"])


def test_aside_layout_wired():
    html = open("stock_agent/web.html", encoding="utf-8").read()
    for k in ('id="asz"', 'id="pul"', 'id="nwl"', 'id="ldg"', 'id="ldgb"', "/api/headlines", "stoa-aside"):
        assert k in html
    aside = html[html.index('<aside id="aside">'):html.index("</aside>")]
    assert "Ledger" not in aside and "Persona" not in aside  # 账本与记忆搬进抽屉
