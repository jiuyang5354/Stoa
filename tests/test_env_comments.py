import importlib, os
from pathlib import Path


def test_desktop_env_strips_inline_comment(tmp_path, monkeypatch):
    from stock_agent.desktop import load_env
    monkeypatch.delenv("QUOTE_DIFF_PCT", raising=False)
    p = tmp_path / ".env"; p.write_text("QUOTE_DIFF_PCT=0.5         # 两源报价偏差超过多少 % 写提醒\n", "utf-8")
    load_env(p)
    assert os.environ["QUOTE_DIFF_PCT"] == "0.5"


def test_marketdata_survives_bad_number(monkeypatch):
    monkeypatch.setenv("QUOTE_DIFF_PCT", "0.5 # comment")
    import stock_agent.marketdata as m
    m = importlib.reload(m)
    assert m.QUOTE_DIFF_PCT == 0.5
    monkeypatch.setenv("QUOTE_DIFF_PCT", "abc")
    assert importlib.reload(m).QUOTE_DIFF_PCT == 0.5
