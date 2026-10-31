"""数据目录 .env 里的行尾注释不该被当成值。

背景：Stoa.exe 首次启动把 .env.example 原样复制成 .env，里面写着
    QUOTE_DIFF_PCT=0.5         # 两源报价偏差超过多少 % 写提醒
读取器只 strip 空格引号的话，float() 会拿到整段中文注释。两套读取逻辑
（desktop.load_env 与 settings.load_env_file）规则不一致，正是当初的根因。
"""
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


def test_plain_value_untouched(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOTE_DIFF_PCT", raising=False)
    from stock_agent.desktop import load_env
    p = tmp_path / ".env"; p.write_text("QUOTE_DIFF_PCT=1.25\n", "utf-8")
    load_env(p)
    assert os.environ["QUOTE_DIFF_PCT"] == "1.25"


def test_settings_loader_uses_same_rule(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOTE_DIFF_PCT", raising=False)
    from stock_agent import settings
    p = tmp_path / ".env"; p.write_text("QUOTE_DIFF_PCT=0.5   # 注释\n", "utf-8")
    monkeypatch.setattr(settings, "env_path", lambda: p)
    settings.load_env_file()
    assert os.environ["QUOTE_DIFF_PCT"] == "0.5"
