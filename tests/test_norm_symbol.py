from stock_agent.agent import norm_symbol, _norm_args


def test_norm_symbol():
    assert norm_symbol("600519") == "600519.SS"
    assert norm_symbol("501011") == "501011.SS"
    assert norm_symbol("000858") == "000858.SZ"
    assert norm_symbol("159915") == "159915.SZ"
    assert norm_symbol("430047") == "430047.BJ"
    assert norm_symbol("600519.SH") == "600519.SS"
    assert norm_symbol("sh600519") == "600519.SS"
    assert norm_symbol("00700") == "0700.HK"
    assert norm_symbol("700.hk") == "0700.HK"
    assert norm_symbol("AAPL") == "AAPL"
    assert norm_symbol("^GSPC") == "^GSPC"
    assert norm_symbol("贵州茅台") == "600519.SS"


def test_norm_args():
    assert _norm_args({"symbols": "600519, 00700"})["symbols"] == ["600519.SS", "0700.HK"]
    assert _norm_args({"symbol": "000858", "x": 1}) == {"symbol": "000858.SZ", "x": 1}
