"""数据目录 .env 里有行尾注释时，数值配置要能正确读出来。

回归背景：Stoa.exe 首次启动会把 .env.example 原样复制成 .env，而里面写着
    QUOTE_DIFF_PCT=0.5         # 两源报价偏差超过多少 % 写提醒
旧的 load_env 只 strip 空格和引号，值里带着整段中文注释，float() 直接抛
ValueError，导致查行情和市场情绪两个工具都不可用。
"""
import importlib
import os
import sys

import pytest


@pytest.fixture()
def clean_env(monkeypatch):
    for k in ("QUOTE_DIFF_PCT", "FLOW_DIFF_PCT", "EPS_DIFF_PCT"):
        monkeypatch.delenv(k, raising=False)


def test_desktop_load_env_strips_inline_comment(tmp_path, clean_env):
    from stock_agent import desktop

    env = tmp_path / ".env"
    env.write_text(
        "QUOTE_DIFF_PCT=0.5         # 两源报价偏差超过多少 % 写提醒\n"
        "FLOW_DIFF_PCT=35           # 资金流两源差异阈值\n"
        'EPS_DIFF_PCT="10"          # EPS 预测两源差异阈值\n',
        encoding="utf-8",
    )
    desktop.load_env(env)

    assert os.environ["QUOTE_DIFF_PCT"] == "0.5"
    assert os.environ["FLOW_DIFF_PCT"] == "35"
    assert os.environ["EPS_DIFF_PCT"] == "10"
    for k in ("QUOTE_DIFF_PCT", "FLOW_DIFF_PCT", "EPS_DIFF_PCT"):
        float(os.environ[k])          # 不能抛 ValueError


def test_load_env_keeps_plain_values(tmp_path, clean_env):
    from stock_agent import desktop

    env = tmp_path / ".env"
    env.write_text("QUOTE_DIFF_PCT=1.25\n", encoding="utf-8")
    desktop.load_env(env)
    assert os.environ["QUOTE_DIFF_PCT"] == "1.25"


def test_settings_loader_same_rule(tmp_path, monkeypatch, clean_env):
    from stock_agent import settings

    env = tmp_path / ".env"
    env.write_text("QUOTE_DIFF_PCT=0.5   # 注释\n", encoding="utf-8")
    monkeypatch.setattr(settings, "env_path", lambda: env)
    settings.load_env_file()
    assert os.environ["QUOTE_DIFF_PCT"] == "0.5"


def test_marketdata_thresholds_survive_dirty_env(monkeypatch):
    monkeypatch.setenv("QUOTE_DIFF_PCT", "0.5         # 两源报价偏差超过多少 % 写提醒")
    monkeypatch.setenv("FLOW_DIFF_PCT", "35   # 资金流")
    monkeypatch.setenv("EPS_DIFF_PCT", "写错了")
    from stock_agent import marketdata

    importlib.reload(marketdata)
    try:
        assert marketdata.QUOTE_DIFF_PCT == 0.5
        assert marketdata.FLOW_DIFF_PCT == 35
        assert marketdata.EPS_DIFF_PCT == 10        # 解析不出数字时退回默认，不抛异常
    finally:
        monkeypatch.undo()
        importlib.reload(marketdata)
