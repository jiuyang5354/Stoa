"""Regression tests for attribution, redirect validation and code isolation."""
import io
import shutil
import subprocess

import pytest

from stock_agent import cite, research, sandbox


def quote_sources():
    s = cite.Sources()
    s.add("get_quotes", {}, {
        "AAA": {"name": "甲公司", "date": "2026-10-28", "close": 512.4, "chg_pct": -2.35, "ma20": 498.21},
        "BBB": {"name": "乙公司", "date": "2026-10-27", "close": 21.3, "chg_pct": 2.35},
    })
    return s


@pytest.mark.parametrize("text", [
    "AAA 收盘 21.3 元，BBB 收盘 512.4 元。",
    "甲公司收盘 21.3 元，乙公司收盘 512.4 元。",
    "AAA 收盘 498.21 元。",
    "AAA 上涨 2.35%。",
    "AAA 10 月 27 日收盘 512.4 元。",
    "AAA close 21.3, BBB close 512.4.",
])
def test_rejects_misattributed_quotes(text):
    assert not cite.verify(text, quote_sources())["ok"]


@pytest.mark.parametrize("text", [
    "AAA 10 月 28 日收盘 512.4 元，下跌 2.35%；BBB 收盘 21.3 元。",
    "甲公司收盘 512.40 元，20 日均线 498.21 元。",
    "AAA close 512.4, BBB close 21.3.",
    "AAA 较 20 日均线高 2.85%。",
])
def test_correct_quotes_and_calculations_still_pass(text):
    result = cite.verify(text, quote_sources())
    assert result["ok"], result["why"]


def test_finalize_does_not_cite_wrong_stock_or_metric():
    text, _ = cite.finalize("AAA 收盘 21.3 元。", quote_sources())
    assert "[1]" not in text


def test_finalize_replaces_a_valid_but_unrelated_source_marker():
    text, used = cite.finalize("AAA 收盘 512.4 元[2]。", quote_sources())
    assert len(used) == 1 and "AAA" in used[0]["name"]


def test_symbol_with_dot_keeps_its_scope():
    s = cite.Sources()
    s.add("get_quotes", {}, {"BRK.B": {"close": 512.4}, "BBB": {"close": 21.3}})
    assert not cite.verify("BRK.B close 21.3.", s)["ok"]


class Response:
    def __init__(self, url, status=200, location=None):
        self.url, self.status_code = url, status
        self.headers = {"content-type": "text/html"}
        if location is not None:
            self.headers["Location"] = location
        self.raw = self
        self.body = io.BytesIO(b"<title>Research</title><p>Public research content.</p>")
        self.encoding = "utf-8"
        self.closed = False

    def close(self):
        self.closed = True

    def read(self, size, decode_content=False):
        return self.body.read(size)


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "http://private.test/secret", "file:///secret"])
def test_redirect_is_checked_before_request(monkeypatch, target):
    monkeypatch.setattr(research, "_public", lambda host: host == "public.test")
    calls, responses = [], []

    def get(url, **kwargs):
        calls.append((url, kwargs["allow_redirects"]))
        response = Response(url, 302, target)
        responses.append(response)
        return response

    monkeypatch.setattr(research.requests, "get", get)
    assert "error" in research.fetch_url("https://public.test/start")
    assert calls == [("https://public.test/start", False)]
    assert all(r.closed for r in responses)


def test_relative_public_redirect_and_response_cleanup(monkeypatch):
    monkeypatch.setattr(research, "_public", lambda host: host == "public.test")
    calls, responses = [], []

    def get(url, **kwargs):
        calls.append(url)
        response = Response(url, 302, "/article") if url.endswith("/start") else Response(url)
        responses.append(response)
        return response

    monkeypatch.setattr(research.requests, "get", get)
    result = research.fetch_url("https://public.test/start")
    assert result["title"] == "Research" and "Public research" in result["text"]
    assert calls == ["https://public.test/start", "https://public.test/article"]
    assert all(r.closed for r in responses)


def test_redirect_loop_is_bounded(monkeypatch):
    monkeypatch.setattr(research, "_public", lambda host: True)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(url, 302, "/loop")

    monkeypatch.setattr(research.requests, "get", get)
    assert "error" in research.fetch_url("https://public.test/loop")
    assert len(calls) <= 6


def test_code_cannot_fall_back_to_host_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "shutil", shutil, raising=False)
    monkeypatch.setenv("ALLOW_CODE", "1")
    monkeypatch.setattr(sandbox, "home", lambda: tmp_path)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: pytest.fail("host code must not run"))
    assert "Docker" in sandbox.run_python("print('hello')")


def test_container_mounts_only_workspace_and_inherits_no_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "shutil", shutil, raising=False)
    monkeypatch.setenv("ALLOW_CODE", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(sandbox, "home", lambda: tmp_path)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "docker")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "42\n", "")

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    assert sandbox.run_python("print(42)") == "42"
    argv, kwargs = calls[0]
    assert argv[:2] == ["docker", "run"]
    assert "--network=none" in argv and "--read-only" in argv and "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    mount = argv[argv.index("--mount") + 1]
    assert str(tmp_path / "workspace") in mount and "target=/workspace" in mount
    assert "LLM_API_KEY" not in kwargs["env"]
    assert "--privileged" not in argv and "-v" not in argv


def test_timeout_removes_container(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "shutil", shutil, raising=False)
    monkeypatch.setenv("ALLOW_CODE", "1")
    monkeypatch.setattr(sandbox, "home", lambda: tmp_path)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "docker")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    assert "超时" in sandbox.run_python("while True: pass", timeout=1)
    assert calls[1][:3] == ["docker", "rm", "-f"]
    assert calls[1][3] == calls[0][calls[0].index("--name") + 1]
