"""Cancellation must stop the real HTTP worker, not only hide the answer."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stock_agent import providers


@pytest.mark.parametrize("error,status", [(providers.RequestCancelled(), "cancelled"), (TimeoutError("request deadline"), "done")])  # v0.36：超时用已拿到的结果收尾，不再空返回
def test_request_interruption_updates_checkpoint(tmp_path, monkeypatch, error, status):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    from stock_agent import ledger
    from stock_agent.agent import Agent
    from stock_agent.memory import Memory
    mem = Memory(tmp_path)
    agent = Agent(mem, session="web-interrupted")

    def interrupt(text, on_tool=None):
        agent._cur_id = mem.log(agent.session, "user", text)
        agent._run_id = ledger.run_start(agent.session, "web", text, agent._cur_id)
        raise error

    monkeypatch.setattr(agent, "_ask", interrupt)
    agent.ask("checkpoint interruption")
    assert ledger.run_get(agent._run_id)["status"] == status
    assert agent.llm.stop is None and agent.llm.on_event is None


@pytest.fixture
def slow_model(monkeypatch):
    started, disconnected = threading.Event(), threading.Event()
    mode = {"retry": False, "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            mode["calls"] += 1
            started.set()
            if mode.get("fast"):
                body = json.dumps({"choices": [{"message": {"content": "中文回答，持仓待确认。"}}]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            if mode["retry"]:
                self.send_response(429); self.send_header("Retry-After", "20"); self.end_headers()
                return
            # Streaming a response lets the server observe that cancellation closed the socket.
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b" "); self.wfile.flush(); time.sleep(.05)
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "late answer"}}]}).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                disconnected.set()
        def log_message(self, *a): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-key")
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LLM_FALLBACK", "")
    yield started, disconnected, mode
    server.shutdown(); server.server_close()


def test_cancel_closes_active_connection(slow_model):
    started, disconnected, _ = slow_model
    stop, errors = threading.Event(), []
    def call():
        try:
            providers.Router().chat([{"role": "user", "content": "slow"}], stop=stop)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=call, daemon=True); thread.start()
    assert started.wait(5)
    t0 = time.monotonic(); stop.set(); thread.join(2)
    assert not thread.is_alive() and time.monotonic() - t0 < 2
    assert len(errors) == 1 and isinstance(errors[0], providers.RequestCancelled)
    assert disconnected.wait(2)


def test_cancel_interrupts_retry_without_another_request(slow_model):
    _, _, mode = slow_model
    mode["retry"] = True
    stop, retrying, errors = threading.Event(), threading.Event(), []
    def event(ev):
        if ev.get("status") == "retry": retrying.set()
    def call():
        try:
            providers.Router().chat([{"role": "user", "content": "retry"}], stop=stop, on_event=event)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=call, daemon=True); thread.start()
    assert retrying.wait(5)
    stop.set(); thread.join(2)
    assert not thread.is_alive() and isinstance(errors[0], providers.RequestCancelled)
    assert mode["calls"] == 1


def test_deadline_covers_slow_dripping_response(slow_model):
    stop = threading.Event()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        providers.Router().chat([{"role": "user", "content": "timeout"}], timeout=.8, stop=stop)
    assert time.monotonic() - t0 < 2


def test_deadline_also_covers_slow_process_creation(monkeypatch):
    import io
    terminated = threading.Event()
    class Process:
        stdin, stdout = io.StringIO(), io.StringIO()
        def terminate(self): terminated.set()
        def wait(self, timeout=None): return 0
    def spawn(*a, **k):
        time.sleep(.5)
        return Process()
    monkeypatch.setattr(providers.subprocess, "Popen", spawn)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        providers.start_request_worker([], {}, {}, threading.Event(), started + .1)
    assert time.monotonic() - started < .4
    assert terminated.wait(2)


def test_worker_preserves_chinese_request_and_reply(slow_model):
    _, _, mode = slow_model
    mode["fast"] = True
    reply = providers.Router().chat([{"role": "user", "content": "中文问题"}], stop=threading.Event())
    assert reply["content"] == "中文回答，持仓待确认。"


def test_cancel_during_inquiry_planning_is_paused(slow_model, tmp_path, monkeypatch):
    from stock_agent import inquiry
    from stock_agent.memory import Memory
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    item = inquiry.create("test slow planning", 5)
    stop, result = threading.Event(), []
    thread = threading.Thread(target=lambda: result.append(inquiry.run(item["id"], Memory(), stop)), daemon=True)
    thread.start()
    assert slow_model[0].wait(5)
    stop.set(); thread.join(2)
    assert not thread.is_alive() and result[0]["status"] == "paused"
