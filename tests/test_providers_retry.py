"""providers：429/5xx 退避重试、真实 usage 统计。"""
import requests

from stock_agent import providers


class Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body or {}, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._body


OK = {"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


def _llm(monkeypatch, seq):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return seq.pop(0)

    monkeypatch.setattr(providers.requests, "post", post)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    return providers.LLM("openai", "m"), calls


def test_retry_on_429_then_ok(monkeypatch):
    llm, calls = _llm(monkeypatch, [Resp(429, headers={"Retry-After": "1"}), Resp(502), Resp(200, OK)])
    u0 = providers.usage_since()
    msg = llm.chat([{"role": "user", "content": "hi"}])
    u = providers.usage_since(u0)
    assert msg["content"] == "好" and len(calls) == 3
    assert u["retries"] == 2 and u["calls"] == 1 and u["errors"] == 0
    assert u["prompt_tokens"] == 10 and u["completion_tokens"] == 2 and u["total_tokens"] == 12


def test_no_retry_on_400_and_gives_up(monkeypatch):
    llm, calls = _llm(monkeypatch, [Resp(400)])
    u0 = providers.usage_since()
    try:
        llm.chat([{"role": "user", "content": "hi"}])
        assert False
    except requests.HTTPError:
        pass
    assert len(calls) == 1 and providers.usage_since(u0)["errors"] == 1
    monkeypatch.setenv("LLM_RETRIES", "1")
    llm, calls = _llm(monkeypatch, [Resp(429), Resp(429)])
    try:
        llm.chat([{"role": "user", "content": "hi"}])
        assert False
    except requests.HTTPError:
        pass
    assert len(calls) == 2


def test_missing_usage_counted(monkeypatch):
    llm, _ = _llm(monkeypatch, [Resp(200, {"choices": [{"message": {"content": "x"}}]})])
    u0 = providers.usage_since()
    llm.chat([{"role": "user", "content": "hi"}])
    assert providers.usage_since(u0)["calls_no_usage"] == 1


# ---- 传输层中断（不是 HTTP 状态码）：连接被重置、读超时、分块截断 ----
# 真实现场：2026-11-16 评测 refuse-03 在 proxy 抖动时拿到
# `RuntimeError: ConnectionError：模型请求失败`，一次请求失败就整题作废，
# 而 429/5xx 是会重试的。传输错误同样该退避重试。
def test_retry_on_connection_reset_then_ok(monkeypatch):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        if len(calls) < 3:
            raise requests.ConnectionError(
                "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
        return Resp(200, OK)

    monkeypatch.setattr(providers.requests, "post", post)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    llm = providers.LLM("openai", "m")
    u0 = providers.usage_since()
    msg = llm.chat([{"role": "user", "content": "hi"}])
    u = providers.usage_since(u0)
    assert msg["content"] == "好" and len(calls) == 3
    assert u["retries"] == 2 and u["errors"] == 0 and u["calls"] == 1


def test_retry_on_read_timeout_then_ok(monkeypatch):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.Timeout("read timed out")
        return Resp(200, OK)

    monkeypatch.setattr(providers.requests, "post", post)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    llm = providers.LLM("openai", "m")
    msg = llm.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "好" and len(calls) == 2


def test_connection_error_gives_up_after_retries(monkeypatch):
    monkeypatch.setenv("LLM_RETRIES", "1")
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        raise requests.ConnectionError("Connection reset by peer")

    monkeypatch.setattr(providers.requests, "post", post)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    llm = providers.LLM("openai", "m")
    u0 = providers.usage_since()
    try:
        llm.chat([{"role": "user", "content": "hi"}])
        assert False, "应该抛出连接错误"
    except requests.ConnectionError:
        pass
    u = providers.usage_since(u0)
    assert len(calls) == 2 and u["errors"] == 1 and u["retries"] == 1


def test_describe_error_keeps_short_cause():
    e = requests.ConnectionError(
        "HTTPSConnectionPool(host='inference-api.example', port=443): Max retries exceeded with url: /v1/chat/completions")
    text = providers.describe_error(e)
    assert text.startswith("ConnectionError：")
    assert "inference-api.example" in text
    assert len(text) <= 200 and "\n" not in text


def test_describe_error_without_message_says_request_failed():
    assert providers.describe_error(RuntimeError()) == "RuntimeError：模型请求失败"


def test_describe_error_keeps_explained_http_text_unprefixed():
    """403 已经被 explain_http 翻成「账户余额不足…」，不要再套一层 HTTPError：。"""
    e = requests.HTTPError("模型账户余额不足：请到服务商控制台充值")
    assert providers.describe_error(e) == "模型账户余额不足：请到服务商控制台充值"
