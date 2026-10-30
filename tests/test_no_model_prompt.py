import json
import threading
import urllib.request

from http.server import ThreadingHTTPServer


def test_chat_without_model_asks_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    for k in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "STOA_MOCK_LLM", "WEB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    from stock_agent import serve
    monkeypatch.setattr(serve, "load_cfg", lambda: {"llm": {}})
    assert serve.llm_ready() is False
    monkeypatch.setenv("STOA_MOCK_LLM", "1")
    assert serve.llm_ready() is True


def test_desktop_has_no_key_popup():
    src = open("stock_agent/desktop.py", encoding="utf-8").read()
    assert "还没填大模型密钥" not in src and "open_file(env)" not in src


def test_titlebar_tint_colors():
    from stock_agent import desktop
    assert desktop._colorref((0xEC, 0xEA, 0xE5)) == 0xE5EAEC
    assert desktop.tint_titlebar() in (False, True)  # 非 Windows 直接跳过


def test_custom_titlebar_wired():
    from stock_agent import desktop
    c = desktop.WinChrome()
    assert c.is_max() is False and c.toggle_max() is False  # 非 Windows 无窗口时安全返回
    html = open("stock_agent/web.html", encoding="utf-8").read()
    assert 'id="wbar"' in html and "frame') === 'custom'" in html and "call('drag')" in html
