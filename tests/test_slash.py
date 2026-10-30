def test_slash_commands_wired():
    html = open("stock_agent/web.html", encoding="utf-8").read()
    assert 'id="slp"' in html and "function slashRun" in html
    for c in ("/deep", "/quick", "/news", "/earnings", "/compare", "/risk", "/review", "/brief", "/remember", "/alert",
              "/new", "/history", "/ledger", "/graph", "/backtest", "/model", "/settings", "/memory"):
        assert f"c: '{c}'" in html, c
