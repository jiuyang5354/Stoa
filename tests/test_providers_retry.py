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
