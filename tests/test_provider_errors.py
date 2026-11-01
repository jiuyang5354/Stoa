"""模型上游 4xx 的说明：欠费、密钥失效、限流、地址错，用户要看懂并能照做。

真实案例（2026-11-01）：中转站余额不足返回 403 + {"code":"INSUFFICIENT_BALANCE"}，
界面上只有 `403 Client Error: Forbidden for url: ...`，用户以为程序坏了。
"""
import requests

from stock_agent import providers


class Resp:
    def __init__(self, code, body="", text=None):
        self.status_code = code
        self._body = body
        self.text = text if text is not None else (body if isinstance(body, str) else "")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)

    def json(self):
        return self._body


def _err(code, text):
    return requests.HTTPError(f"{code} Client Error", response=Resp(code, text=text))


def test_insufficient_balance_is_explained():
    msg = providers.explain_http(_err(403, '{"code":"INSUFFICIENT_BALANCE","message":"账户余额不足"}'))
    assert "余额不足" in msg and "充值" in msg


def test_bad_key_is_explained():
    assert "密钥" in providers.explain_http(_err(401, '{"error":{"message":"Incorrect API key provided"}}'))
    assert "密钥" in providers.explain_http(_err(403, "该密钥已停用，请到控制台重新复制"))


def test_rate_limit_and_bad_url_are_explained():
    assert "限流" in providers.explain_http(_err(429, ""))
    assert "接口地址" in providers.explain_http(_err(404, ""))


def test_unknown_error_keeps_original_text():
    assert "boom" in providers.explain_http(_err(400, "boom"))
    assert providers.explain_http(requests.HTTPError("no response")) == "no response"


def test_chat_raises_explained_error_without_retry(monkeypatch):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return Resp(403, text='{"code":"INSUFFICIENT_BALANCE"}')

    monkeypatch.setattr(providers.requests, "post", post)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    llm = providers.LLM("openai", "m")
    try:
        llm.chat([{"role": "user", "content": "hi"}])
        raise AssertionError("应当抛错")
    except requests.HTTPError as e:
        assert "余额不足" in str(e)
    assert len(calls) == 1          # 403 不重试，别把欠费当临时故障
