"""v0.34 长任务机制：断点续跑、推送幂等、补跑策略、确认跨重启、硬约束。全部离线 mock。"""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

CN = timezone(timedelta(hours=8))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    for k in ("SERVERCHAN_KEY", "WECOM_WEBHOOK", "FEISHU_WEBHOOK", "TELEGRAM_BOT_TOKEN", "DINGTALK_WEBHOOK", "PUSHPLUS_TOKEN", "BARK_KEY",
              "NTFY_TOPIC", "DISCORD_WEBHOOK", "SLACK_WEBHOOK", "WEBHOOK_URL", "PUSH_CONFIRM_NEW", "REPORT_TIMES", "REPORT_MAX_LATE", "REPORT_CATCHUP"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


# ------------------------------------------------------------------ (a) 断点续跑
def test_task_file_save_waits_for_active_reader(home, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from stock_agent import tasks
    tasks.save([{"id": "before"}])
    reading, release, replacing = threading.Event(), threading.Event(), threading.Event()
    original_read, original_replace = Path.read_text, Path.replace

    def read(path, *args, **kwargs):
        if path == tasks._path():
            reading.set()
            assert release.wait(2)
        return original_read(path, *args, **kwargs)

    def replace(path, target):
        busy = reading.is_set() and not release.is_set()
        replacing.set()
        if busy:
            raise PermissionError("task file is open for reading")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(tasks.load)
        assert reading.wait(2)
        writer = pool.submit(tasks.save, [{"id": "after"}])
        replacing.wait(1)
        release.set()
        assert reader.result() == [{"id": "before"}]
        writer.result()
    assert tasks.load() == [{"id": "after"}]


class ScriptLLM:
    """第 1 次：调 get_quotes + record_call；第 2 次：回答。crash_at=n 时第 n 次调用抛异常（模拟进程中途挂掉）。"""

    def __init__(self, crash_at=None):
        self.calls, self.crash_at = 0, crash_at
        self.ready = True

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        if self.crash_at == self.calls:
            raise RuntimeError("进程被杀（模拟）")
        if not any(m["role"] == "tool" for m in messages[-4:]):
            return {"role": "assistant", "content": "先查行情。", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_quotes", "arguments": json.dumps({"symbols": ["NVDA"]})}},
                {"id": "c2", "type": "function", "function": {"name": "record_call", "arguments": json.dumps(
                    {"symbol": "NVDA", "view": "看涨", "horizon_days": 5, "reason": "测试用判断理由"})}}]}
        return {"role": "assistant", "content": "NVDA 收在 100.5，已记一条看涨判断。"}


def _agent(llm, counter, session=None):
    from stock_agent.agent import Agent

    class A(Agent):
        def run_tool(self, name, args):
            counter[name] = counter.get(name, 0) + 1
            if name == "get_quotes":
                return {"NVDA": {"close": 100.5, "date": "2026-10-30", "source": "测试"}}
            if name == "record_call":
                return {"ok": True, "id": counter[name]}
            return {}
    a = A(channel="web", session=session)
    a.llm = llm
    a.tools = [t for t in a.tools if t["name"] in ("get_quotes", "record_call")]
    return a


def test_checkpoint_resume_skips_done_steps(home, monkeypatch):
    from stock_agent import agent as AG, ledger
    counter = {}
    a = _agent(ScriptLLM(crash_at=2), counter, session="web-ck1")
    with pytest.raises(RuntimeError):
        a.ask("英伟达现在多少？记一笔判断")
    runs = ledger.interrupted()
    assert len(runs) == 1 and runs[0]["status"] == "failed" and runs[0]["n_steps"] == 3  # 模型 1 步 + 两个工具
    assert counter == {"get_quotes": 1, "record_call": 1}
    # 重开：新进程里续跑。模型第 1 步从库里取，两个工具都不重跑（尤其是有副作用的 record_call）
    llm2 = ScriptLLM()
    monkeypatch.setattr(AG.Agent, "__init__", _patched_init(AG.Agent.__init__, llm2, counter))
    ag, reply = AG.Agent.resume(runs[0]["run_id"])
    assert "100.5" in reply
    assert counter == {"get_quotes": 1, "record_call": 1}
    assert llm2.calls == 1  # 只调了收尾那一次
    assert ledger.run_get(runs[0]["run_id"])["status"] == "done"
    hist = ag.mem.history("web-ck1")
    assert [h["role"] for h in hist] == ["user", "assistant"]  # 提问没有被重复记一遍
    with pytest.raises(ValueError):
        AG.Agent.resume(runs[0]["run_id"])  # 跑完了不能再续


def _patched_init(orig, llm, counter):
    def init(self, mem=None, channel="cli", session=None):
        orig(self, mem, channel, session)
        self.llm = llm
        self.tools = [t for t in self.tools if t["name"] in ("get_quotes", "record_call")]
        real = self.run_tool

        def rt(name, args):
            counter[name] = counter.get(name, 0) + 1
            if name == "get_quotes":
                return {"NVDA": {"close": 100.5, "date": "2026-10-30", "source": "测试"}}
            return {"ok": True}
        self.run_tool = rt
    return init


def test_normal_run_is_marked_done_and_cancel(home):
    from stock_agent import ledger
    from stock_agent.agent import Agent
    counter = {}
    a = _agent(ScriptLLM(), counter)
    a.ask("英伟达现在多少？")
    rs = ledger.runs_list()
    assert rs[0]["status"] == "done" and rs[0]["n_steps"] == 4
    assert ledger.interrupted() == []
    b = _agent(ScriptLLM(), counter)
    b._stop = threading.Event()
    b._stop.set()
    assert b.ask("再查一次") == "已停止。"
    assert ledger.runs_list()[0]["status"] == "cancelled"
    assert issubclass(Agent, object)


def test_checkpoint_off(home, monkeypatch):
    from stock_agent import ledger
    monkeypatch.setenv("AGENT_CHECKPOINT", "off")
    _agent(ScriptLLM(), {}).ask("英伟达现在多少？")
    assert ledger.runs_list() == []


def test_checkpoint_read_failure_stops_before_tool_execution(home, monkeypatch):
    """An unreadable checkpoint is unknown, never evidence that an effect did not run."""
    import sqlite3
    from stock_agent import ledger

    counter = {}
    a = _agent(ScriptLLM(), counter)
    original = ledger._q

    def unreadable(sql, params=(), one=False):
        if "FROM run_steps WHERE" in sql and params[1] > 0:
            raise sqlite3.OperationalError("injected checkpoint read failure")
        return original(sql, params, one)

    monkeypatch.setattr(ledger, "_q", unreadable)
    with pytest.raises(RuntimeError, match="断点"):
        a.ask("英伟达现在多少？记一笔判断")
    assert counter == {}
    assert ledger.run_get(a._run_id)["status"] == "unknown"
    assert ledger.interrupted() == []


def test_effect_checkpoint_write_failure_remains_unknown(home):
    from stock_agent import ledger
    from stock_agent.agent import Agent

    ledger.db().execute("""CREATE TRIGGER fail_effect_checkpoint BEFORE INSERT ON run_steps
        WHEN NEW.kind='tool' AND NEW.name='record_call'
        BEGIN SELECT RAISE(FAIL, 'injected result persistence failure'); END""")
    counter = {}
    a = _agent(ScriptLLM(), counter)
    with pytest.raises(RuntimeError, match="断点"):
        a.ask("英伟达现在多少？记一笔判断")
    assert counter == {"get_quotes": 1, "record_call": 1}
    rid = a._run_id
    assert rid is not None
    record = ledger.run_get(rid)
    assert record is not None and record["status"] == "unknown"
    pending = ledger.step_get(rid, 2)
    assert pending is not None and pending["kind"] == "tool_pending"
    assert ledger.interrupted() == []
    with pytest.raises(ValueError, match="unknown"):
        Agent.resume(rid)
    b = _agent(ScriptLLM(), counter)
    b._run_id = rid
    with pytest.raises(RuntimeError, match="断点"):
        b._tool("record_call", pending["payload"]["args"], step=2)
    assert counter["record_call"] == 1


@pytest.mark.parametrize("kind,name,payload", [
    ("tool", "get_quotes", {"args": {"symbols": ["AAPL"]}, "result": {"ok": True}}),
    ("tool", "get_news", {"args": {"symbols": ["NVDA"]}, "result": {"ok": True}}),
    ("model", "", {"role": "assistant", "content": "cached model"}),
    ("tool", "get_quotes", {"args": {"symbols": ["NVDA"]}}),
])
def test_checkpoint_replay_rejects_different_call(home, kind, name, payload):
    from stock_agent import ledger
    counter = {}
    a = _agent(ScriptLLM(), counter)
    a._begin_sources("行情")
    a._run_id = ledger.run_start(a.session, "web", "行情", None)
    ledger.step_put(a._run_id, 0, kind, name, payload)
    with pytest.raises(RuntimeError, match="断点"):
        a._tool("get_quotes", {"symbols": ["NVDA"]}, step=0)
    assert counter == {}
    assert ledger.step_get(a._run_id, 0)["payload"] == payload


@pytest.mark.parametrize("fault", ["read", "write", "kind"])
def test_model_checkpoint_failure_is_not_a_cache_miss(home, monkeypatch, fault):
    from stock_agent import ledger
    a = _agent(ScriptLLM(), {})
    a._run_id = ledger.run_start(a.session, "web", "行情", None)
    calls = []
    monkeypatch.setattr(a, "_chat", lambda *a: calls.append(1) or {"role": "assistant", "content": "answer"})
    if fault == "read":
        def broken(*args, **kwargs):
            raise OSError("unreadable")
        monkeypatch.setattr(ledger, "_q", broken)
    elif fault == "write":
        ledger.db().execute("""CREATE TRIGGER fail_model_checkpoint BEFORE INSERT ON run_steps
            WHEN NEW.kind='model' BEGIN SELECT RAISE(FAIL, 'injected model persistence failure'); END""")
    else:
        ledger.step_put(a._run_id, 0, "tool", "record_call", {"args": {}, "result": {"ok": True}})
    with pytest.raises(RuntimeError, match="断点"):
        a._model_step([], "default")
    assert calls == ([1] if fault == "write" else [])


def test_effect_exception_does_not_continue_as_normal_tool_error(home):
    from stock_agent import ledger
    counter = {}
    a = _agent(ScriptLLM(), counter)
    original = a.run_tool

    def lost_response(name, args):
        result = original(name, args)
        if name == "record_call":
            raise ConnectionError("effect succeeded but response lost")
        return result

    a.run_tool = lost_response
    with pytest.raises(RuntimeError, match="断点"):
        a.ask("英伟达现在多少？记一笔判断")
    assert counter["record_call"] == 1
    assert ledger.run_get(a._run_id)["status"] == "unknown"


def test_checkpoint_start_failure_does_not_disable_durability(home, monkeypatch):
    from stock_agent import ledger
    counter = {}
    a = _agent(ScriptLLM(), counter)

    def unavailable(*args):
        raise OSError("database unavailable")

    monkeypatch.setattr(ledger, "run_start", unavailable)
    with pytest.raises(RuntimeError, match="断点"):
        a.ask("英伟达现在多少？记一笔判断")
    assert a.llm.calls == 0 and counter == {}


@pytest.mark.parametrize("stage", ["before_effect", "after_effect"])
def test_process_exit_leaves_effect_unknown_without_replay(home, stage):
    """Real child process exit; only the effect itself is a local synthetic append."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    from stock_agent import ledger
    from stock_agent.agent import Agent

    script = r'''
import os, runpy
from pathlib import Path
from stock_agent import ledger
ns = runpy.run_path("tests/test_longrun.py")
a = ns["_agent"](ns["ScriptLLM"](), {}, session="web-process-exit")
root = Path(os.environ["STOCK_AGENT_HOME"])
start, claim, tool = ledger.run_start, ledger.step_claim, a.run_tool
def start_run(*args):
    rid = start(*args)
    (root / "rid.txt").write_text(rid, encoding="utf-8")
    return rid
def claim_step(*args):
    claim(*args)
    if os.environ["CRASH_STAGE"] == "before_effect":
        os._exit(71)
def run_tool(name, args):
    if name == "record_call":
        with (root / "effect.txt").open("a", encoding="utf-8") as f:
            f.write("effect\n")
            f.flush()
            os.fsync(f.fileno())
        os._exit(72)
    return tool(name, args)
ledger.run_start, ledger.step_claim, a.run_tool = start_run, claim_step, run_tool
a.ask("英伟达现在多少？记一笔判断")
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", script], cwd=root,
                            env={**os.environ, "CRASH_STAGE": stage}, capture_output=True, timeout=30)
    assert result.returncode == (71 if stage == "before_effect" else 72), result.stderr.decode(errors="replace")
    rid = (home / "rid.txt").read_text(encoding="utf-8")
    pending = ledger.step_get(rid, 2)
    assert pending is not None and pending["kind"] == "tool_pending"
    before = (home / "effect.txt").read_text(encoding="utf-8") if (home / "effect.txt").exists() else ""
    assert before == ("effect\n" if stage == "after_effect" else "")
    with pytest.raises(RuntimeError, match="断点"):
        Agent.resume(rid)
    record = ledger.run_get(rid)
    assert record is not None and record["status"] == "unknown"
    after = (home / "effect.txt").read_text(encoding="utf-8") if (home / "effect.txt").exists() else ""
    assert after == before


def test_step_claim_has_one_winner_and_rolls_back_on_failure(home):
    from concurrent.futures import ThreadPoolExecutor
    from stock_agent import ledger
    rid = ledger.run_start("web-race", "web", "test", None)

    def claim(_):
        try:
            ledger.step_claim(rid, 1, "tool", "record_call", {"symbol": "NVDA"})
            return True
        except ledger.CheckpointError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    ledger.db().execute("""CREATE TRIGGER fail_run_touch BEFORE UPDATE ON runs
        BEGIN SELECT RAISE(FAIL, 'injected run timestamp failure'); END""")
    with pytest.raises(ledger.CheckpointError):
        ledger.step_claim(rid, 2, "tool", "record_call", {})
    assert ledger.step_get(rid, 2) is None
    with pytest.raises(ledger.CheckpointError):
        ledger.step_put(rid, 1, "tool", "record_call", {"args": {}, "result": {"ok": True}})
    pending = ledger.step_get(rid, 1)
    assert pending is not None and pending["kind"] == "tool_pending"


@pytest.mark.parametrize("fault", ["read", "write", "pending", "lost_response"])
def test_direct_debate_checkpoint_fault_never_falls_back(home, monkeypatch, fault):
    from stock_agent import debate, ledger
    a = _agent(ScriptLLM(), {})
    dispatched = []
    monkeypatch.setattr(debate, "wants_debate", lambda text: True)
    monkeypatch.setattr(debate, "resolve_symbol", lambda *args: "NVDA")

    def run(*args):
        dispatched.append(1)
        if fault == "lost_response":
            raise ConnectionError("debate output unknown")
        return {"id": "synthetic-debate", "answer": "结论完成"}

    monkeypatch.setattr(debate, "run_for_agent", run)
    if fault == "read":
        original = ledger._q
        def unreadable(sql, params=(), one=False):
            if "FROM run_steps WHERE" in sql:
                raise OSError("unreadable")
            return original(sql, params, one)
        monkeypatch.setattr(ledger, "_q", unreadable)
    elif fault == "write":
        ledger.db().execute("""CREATE TRIGGER fail_debate_checkpoint BEFORE INSERT ON run_steps
            WHEN NEW.kind='debate' BEGIN SELECT RAISE(FAIL, 'injected debate persistence failure'); END""")
    elif fault == "pending":
        original_start = ledger.run_start
        def started(*args):
            rid = original_start(*args)
            ledger.step_claim(rid, 0, "debate", "deep_analysis", {"symbol": "NVDA", "question": args[2]})
            return rid
        monkeypatch.setattr(ledger, "run_start", started)
    with pytest.raises(RuntimeError, match="断点"):
        a.ask("深度分析英伟达")
    assert dispatched == ([1] if fault in ("write", "lost_response") else [])
    assert a.llm.calls == 0
    assert ledger.run_get(a._run_id)["status"] == "unknown"


def _nested_debate_agent(counter):
    from stock_agent.agent import Agent
    from stock_agent.debate import MockRoleLLM

    class NestedLLM(ScriptLLM):
        def chat(self, messages, tools=None, **kw):
            if "[ROLE:" in messages[0]["content"]:
                return MockRoleLLM().chat(messages, tools)
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": "", "tool_calls": [{"id": "deep", "type": "function",
                        "function": {"name": "deep_analysis", "arguments": '{"symbol":"NVDA"}'}}]}
            return {"role": "assistant", "content": "研究完成。"}

    a = _agent(NestedLLM(), counter)
    a._no_debate = True
    original = a.run_tool

    def tool(name, args):
        if name == "deep_analysis":
            return Agent.run_tool(a, name, args)
        return original(name, args)

    a.run_tool = tool
    return a


def test_tool_debate_nested_record_belongs_to_outer_checkpoint(home):
    from stock_agent import ledger
    counter = {}
    a = _nested_debate_agent(counter)
    a.ask("研究一下")
    assert counter.get("record_call") == 1
    rows = ledger.steps(a._run_id)
    assert [(r["kind"], r["name"]) for r in rows] == [("model", ""), ("tool", "deep_analysis"), ("model", "")]
    assert ledger.run_get(a._run_id)["status"] == "done"


@pytest.mark.parametrize("tool", ["record_call", "get_quotes", "compare_filings"])
def test_nested_debate_checkpoint_error_is_fatal(home, tool):
    from stock_agent import ledger
    a = _nested_debate_agent({})
    original = a.run_tool

    def broken(name, args):
        if name == tool:
            raise ledger.CheckpointError("断点内部故障")
        return original(name, args)

    a.run_tool = broken
    with pytest.raises(ledger.CheckpointError):
        a.ask("研究一下")
    assert ledger.run_get(a._run_id)["status"] == "unknown"
    assert ledger.step_get(a._run_id, 1)["kind"] == "tool_pending"


def test_nested_debate_lost_effect_ack_keeps_outer_pending(home):
    from stock_agent import ledger
    counter = {}
    a = _nested_debate_agent(counter)
    original = a.run_tool

    def lost(name, args):
        result = original(name, args)
        if name == "record_call":
            raise ConnectionError("effect committed but ack lost")
        return result

    a.run_tool = lost
    with pytest.raises(ledger.CheckpointError):
        a.ask("研究一下")
    assert counter["record_call"] == 1
    assert ledger.run_get(a._run_id)["status"] == "unknown"
    assert ledger.step_get(a._run_id, 1)["kind"] == "tool_pending"


@pytest.mark.parametrize("path", ["tool", "direct"])
def test_debate_user_cancel_is_not_unknown(home, monkeypatch, path):
    from stock_agent import debate, ledger
    from stock_agent.agent import Cancelled
    a = _nested_debate_agent({})
    if path == "direct":
        a._no_debate = False
        monkeypatch.setattr(debate, "wants_debate", lambda text: True)
        monkeypatch.setattr(debate, "resolve_symbol", lambda *args: "NVDA")
    original = a.run_tool

    def stopped(name, args):
        if name == "record_call":
            raise Cancelled()
        return original(name, args)

    a.run_tool = stopped
    assert a.ask("研究一下") == "已停止。"
    assert ledger.run_get(a._run_id)["status"] == "cancelled"


@pytest.mark.parametrize("entry", ["ask", "resume"])
def test_run_owner_excludes_other_process_and_crash_releases(home, monkeypatch, entry):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time
    from stock_agent import agent as AG, ledger

    rid = ledger.run_start("web-owner", "web", "行情", None) if entry == "resume" else ""
    script = r'''
import os, runpy, time
from pathlib import Path
from stock_agent import agent as AG, ledger
ns = runpy.run_path("tests/test_longrun.py")
class HoldingLLM(ns["ScriptLLM"]):
    def chat(self, *args, **kwargs):
        rid = ledger.runs_list()[0]["run_id"]
        (Path(os.environ["STOCK_AGENT_HOME"]) / "owner-ready").write_text(rid)
        time.sleep(60)
        raise RuntimeError("test owner was not killed")
AG.Agent.__init__ = ns["_patched_init"](AG.Agent.__init__, HoldingLLM(), {})
if os.environ["TEST_RID"]:
    AG.Agent.resume(os.environ["TEST_RID"])
else:
    AG.Agent(channel="web", session="web-owner").ask("行情")
'''
    p = subprocess.Popen([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                         env={**os.environ, "TEST_RID": rid}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        ready = home / "owner-ready"
        while not ready.exists() and p.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists(), f"owner failed: {p.poll()}"
        rid = ready.read_text()
        before = ledger.run_get(rid)
        counter, llm = {}, ScriptLLM()
        monkeypatch.setattr(AG.Agent, "__init__", _patched_init(AG.Agent.__init__, llm, counter))
        with pytest.raises(ledger.CheckpointError, match="执行|占用"):
            AG.Agent.resume(rid)
        assert llm.calls == 0 and counter == {}
        assert ledger.run_get(rid) == before
        p.kill()
        p.wait(timeout=10)
        _, reply = AG.Agent.resume(rid)
        assert reply and counter["record_call"] == 1
        assert ledger.run_get(rid)["status"] == "done"
    finally:
        if p.poll() is None:
            p.kill()
        p.communicate(timeout=10)


def test_late_readonly_cannot_replace_pending_then_repeat_effect(home):
    from stock_agent import ledger
    counter = {}
    a = _agent(ScriptLLM(), counter)
    a._begin_sources("行情")
    a._run_id = ledger.run_start(a.session, "web", "行情", None)
    a._run_step = 0
    args = {"symbol": "NVDA", "view": "看涨", "horizon_days": 5, "reason": "测试用判断理由"}
    original = a.run_tool

    def late(name, params):
        if name == "get_quotes":
            # B has already read a miss. A now claims the same step, commits
            # its effect and loses the ack before B's readonly result arrives.
            ledger.step_claim(a._run_id, 0, "tool", "record_call", args)
            original("record_call", args)
        return original(name, params)

    a.run_tool = late
    calls = [{"id": "read", "function": {"name": "get_quotes", "arguments": '{"symbols":["NVDA"]}'}},
             {"id": "effect", "function": {"name": "record_call", "arguments": json.dumps(args)}}]
    with pytest.raises(ledger.CheckpointError):
        a._run_calls(calls)
    assert counter["record_call"] == 1
    assert ledger.step_get(a._run_id, 0) == {"kind": "tool_pending", "name": "record_call", "payload": {"args": args}}
    assert ledger.step_get(a._run_id, 1) is None


@pytest.mark.parametrize("existing", ["model", "pending"])
def test_late_model_plan_cannot_replace_existing_step(home, monkeypatch, existing):
    from stock_agent import ledger
    a = _agent(ScriptLLM(), {})
    a._run_id = ledger.run_start(a.session, "web", "行情", None)
    a._run_step = 0
    old = {"role": "assistant", "content": "old plan"}

    def late(*args):
        if existing == "model":
            ledger.step_put(a._run_id, 0, "model", "", old)
        else:
            ledger.step_claim(a._run_id, 0, "tool", "record_call", {})
        return {"role": "assistant", "content": "different plan"}

    monkeypatch.setattr(a, "_chat", late)
    with pytest.raises(ledger.CheckpointError):
        a._model_step([], [])
    saved = ledger.step_get(a._run_id, 0)
    assert saved["payload"] == (old if existing == "model" else {"args": {}})


def test_nested_debate_stop_event_prevents_later_effect(home):
    from stock_agent import ledger
    counter = {}
    a = _nested_debate_agent(counter)
    a._stop = threading.Event()
    original = a.run_tool

    def stop_during_read(name, args):
        result = original(name, args)
        if name == "get_quotes":
            a._stop.set()
        return result

    a.run_tool = stop_during_read
    assert a.ask("研究一下") == "已停止。"
    assert counter.get("record_call", 0) == 0
    assert ledger.run_get(a._run_id)["status"] == "cancelled"


# ------------------------------------------------------------------ (b) 推送幂等 + (d) 新渠道确认
class Sent:
    def __init__(self):
        self.items = []

    def post(self, url, **kw):
        self.items.append(url)

        class R:
            status_code = 200
        return R()


def test_push_idempotent_by_key(home, monkeypatch):
    from stock_agent import notify
    s = Sent()
    monkeypatch.setattr(notify.requests, "post", s.post)
    monkeypatch.setenv("BARK_KEY", "k")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    assert notify.push("早报", "内容", key="report:morning:2026-10-30 08:30") == ["Bark", "ntfy"]
    assert notify.push("早报", "内容", key="report:morning:2026-10-30 08:30") == []  # 重试 / 补跑：不重复推
    assert len(s.items) == 2
    assert notify.push("早报", "内容", key="report:morning:2026-11-02 08:30") == ["Bark", "ntfy"]
    assert notify.push("无键", "x") == ["Bark", "ntfy"] and notify.push("无键", "x") == ["Bark", "ntfy"]  # 不带键不去重


def test_push_partial_failure_retries_only_failed_channel(home, monkeypatch):
    from stock_agent import notify
    calls = []

    def post(url, **kw):
        calls.append(url)
        if "ntfy" in url and len([c for c in calls if "ntfy" in c]) == 1:
            raise ConnectionError("ntfy 挂了")

        class R:
            status_code = 200
        return R()
    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setenv("BARK_KEY", "k")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    assert notify.push("晚报", "x", key="k1") == ["Bark"]
    assert notify.push("晚报", "x", key="k1") == ["ntfy"]  # 第二次只补失败的渠道
    assert sum("api.day.app" in c for c in calls) == 1


def test_new_channel_needs_approval_and_survives_restart(home, monkeypatch):
    from stock_agent import ledger, notify
    s = Sent()
    monkeypatch.setattr(notify.requests, "post", s.post)
    monkeypatch.setenv("BARK_KEY", "k")
    assert notify.push("早报", "a", key="r1") == ["Bark"]  # 升级时已配置的渠道直接信任
    monkeypatch.setenv("DISCORD_WEBHOOK", "https://discord.example/hook")  # 用户新开一个渠道
    assert notify.push("早报", "b", key="r2") == ["Bark"]
    pend = ledger.approvals("pending")
    assert len(pend) == 1 and pend[0]["kind"] == "push_channel" and pend[0]["payload"]["channel"] == "Discord"
    notify.push("晚报", "c", key="r3")
    assert len(ledger.approvals("pending")) == 1  # 同一渠道只挂一条
    ledger._DB.clear()  # 模拟重启：连接全丢，重新打开数据库
    pend = ledger.approvals("pending")
    assert len(pend) == 1
    r = ledger.approval_decide(pend[0]["id"], True)
    assert r["ok"] and r["result"]["sent"] == ["Discord"] and ledger.trusted("Discord")
    assert any("discord" in u for u in s.items)
    assert notify.push("早报", "d", key="r4") == ["Bark", "Discord"]
    assert "error" in ledger.approval_decide(pend[0]["id"], True)  # 不能重复执行


def test_gate_can_be_disabled(home, monkeypatch):
    from stock_agent import ledger, notify
    monkeypatch.setattr(notify.requests, "post", Sent().post)
    ledger.meta("channels_seeded", "x")
    monkeypatch.setenv("PUSH_CONFIRM_NEW", "off")
    monkeypatch.setenv("SLACK_WEBHOOK", "https://slack.example")
    assert notify.push("t", "m") == ["Slack"]


# ------------------------------------------------------------------ (d) 覆盖文件要确认
def test_overwrite_file_pending_then_approved(home):
    from stock_agent import exports, ledger
    r1 = exports.write("持仓表", "csv", columns=["a"], rows=[[1]], replace=True)
    assert r1["ok"] and r1["file"] == "持仓表.csv"
    r2 = exports.write("持仓表", "csv", columns=["a"], rows=[[2]], replace=True)
    assert "pending_approval" in r2 and "1" in (exports.root() / "持仓表.csv").read_text(encoding="utf-8-sig")
    ledger._DB.clear()  # 重启
    a = ledger.approvals("pending")[0]
    assert a["kind"] == "file_overwrite"
    assert ledger.approval_decide(a["id"], True)["ok"]
    assert "2" in (exports.root() / "持仓表.csv").read_text(encoding="utf-8-sig")
    r3 = exports.write("持仓表", "csv", columns=["a"], rows=[[3]])  # 默认不覆盖：新文件
    assert r3["ok"] and r3["file"] != "持仓表.csv"
    r4 = exports.write("持仓表", "csv", columns=["a"], rows=[[4]], replace=True)
    assert ledger.approval_decide(r4["pending_approval"], False)["status"] == "rejected"
    assert "2" in (exports.root() / "持仓表.csv").read_text(encoding="utf-8-sig")


# ------------------------------------------------------------------ (e) 硬约束
def test_guards():
    from stock_agent import guards
    ok = {"symbol": "NVDA", "view": "看涨", "horizon_days": 5, "reason": "均线多头排列"}
    assert guards.check("record_call", ok, "记一笔", {}) is None
    assert guards.check("record_call", {**ok, "view": "看多"}, "", {})["blocked_by"] == "guard:ledger"
    assert guards.check("record_call", {**ok, "horizon_days": 999}, "", {})["blocked_by"] == "guard:ledger"
    assert guards.check("record_call", {**ok, "confidence": 150}, "", {})["blocked_by"] == "guard:ledger"
    assert guards.check("record_call", {**ok, "reason": ""}, "", {})["blocked_by"] == "guard:ledger"
    assert guards.check("confirm_action", {"id": "x", "approve": True}, "帮我看看有什么待确认的", {})["blocked_by"] == "guard:consent"
    assert guards.check("confirm_action", {"id": "x", "approve": True}, "确认，覆盖吧", {}) is None
    assert guards.check("confirm_action", {"id": "x", "approve": True}, "不要覆盖", {})["blocked_by"] == "guard:consent"
    assert guards.check("confirm_action", {"id": "x", "approve": False}, "不要覆盖", {}) is None
    assert guards.check("confirm_action", {"id": "x", "approve": True}, "[定时任务「x」] 确认", {})["blocked_by"] == "guard:unattended"
    assert guards.check("task_create", {}, "[定时任务「x」] 每天", {})["blocked_by"] == "guard:unattended"
    assert guards.check("memory", {}, "", {"memory": 8})["blocked_by"] == "guard:turn_limit"
    assert guards.check("memory", {}, "", {"memory": 7}) is None


def test_guard_applied_inside_agent(home):
    counter = {}

    class Bad(ScriptLLM):
        def chat(self, messages, tools=None, **kw):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "record_call", "arguments": json.dumps({"symbol": "NVDA", "view": "大涨", "horizon_days": 5, "reason": "xxxx"})}}]}
            last = [m for m in messages if m["role"] == "tool"][-1]["content"]
            return {"role": "assistant", "content": "被拦了：" + ("guard" if "guard:ledger" in last else "没拦")}
    a = _agent(Bad(), counter)
    assert "guard" in a.ask("记一笔") and counter == {}


# ------------------------------------------------------------------ (c) 补跑策略
def _t(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CN)


def test_report_schedule_on_time_late_and_stale(home, monkeypatch):
    from stock_agent import ledger, schedule
    monkeypatch.setenv("REPORT_TIMES", "morning=08:30")
    monkeypatch.setenv("REPORT_MAX_LATE", "morning=3,evening=12")
    ran = []
    runner = lambda session, planned, late, key: ran.append((session, planned, late, key)) or 0  # noqa: E731
    schedule.tick_reports(runner, now=_t("2026-10-30 08:30"))  # 周五准点
    assert ran == [("morning", "2026-10-30 08:30", None, "report:morning:2026-10-30 08:30")]
    schedule.tick_reports(runner, now=_t("2026-10-30 08:31"))  # 同一次不再跑
    assert len(ran) == 1
    # 关机到 10:10 才开：早报补发并标注；晚报还没到点
    ran.clear()
    ledger._DB.clear()
    schedule.tick_reports(runner, now=_t("2026-11-02 10:10"))  # 周一
    assert len(ran) == 1 and ran[0][0] == "morning" and "补发" in ran[0][2] and "08:30" in ran[0][2]
    # 过期太久（周一早报到 12:00 之后才开机）→ 跳过并记账
    ran.clear()
    schedule.tick_reports(runner, now=_t("2026-11-03 12:30"))
    assert ran == [] and ledger.job_get("report:morning:2026-11-03 08:30")["status"] == "skipped"
    # 周五晚报错过，周六 08:00 开机：12 小时内 → 补发；周末没有早报
    monkeypatch.setenv("REPORT_TIMES", "morning=08:30,evening=21:00")
    ran.clear()
    schedule.tick_reports(runner, now=_t("2026-11-07 08:00"))
    assert [(r[0], r[1]) for r in ran] == [("evening", "2026-11-06 21:00")] and "补发" in ran[0][2]
    ran.clear()
    schedule.tick_reports(runner, now=_t("2026-11-07 08:05"))
    assert ran == []


def test_report_catchup_off(home, monkeypatch):
    from stock_agent import schedule
    monkeypatch.setenv("REPORT_TIMES", "morning=08:30")
    monkeypatch.setenv("REPORT_CATCHUP", "off")
    ran = []
    schedule.tick_reports(lambda *a: ran.append(a) or 0, now=_t("2026-11-02 10:10"))
    assert ran == []


def test_report_failure_marks_error_and_retries(home, monkeypatch):
    from stock_agent import ledger, schedule
    monkeypatch.setenv("REPORT_TIMES", "morning=08:30")
    monkeypatch.setattr(ledger, "RETRY_AFTER", timedelta(0))
    rc = iter([1, 0])
    ran = []
    schedule.tick_reports(lambda *a: ran.append(a) or next(rc), now=_t("2026-11-02 08:30"))
    assert ledger.job_get("report:morning:2026-11-02 08:30")["status"] == "error"
    schedule.tick_reports(lambda *a: ran.append(a) or next(rc), now=_t("2026-11-02 08:40"))
    assert ledger.job_get("report:morning:2026-11-02 08:30")["status"] == "done" and len(ran) == 2
    assert ran[1][3] == ran[0][3]  # 同一个推送键：前一次若已推过某渠道，这次不会重复


def test_task_idempotent_and_late_tag(home, monkeypatch):
    from stock_agent import ledger, tasks
    t = tasks.add("总结一下大盘", "daily", "09:00")
    pushed = []
    push = lambda title, md, key=None: pushed.append((title, key)) or ["x"]  # noqa: E731
    ts = tasks.load()
    ts[0]["next_run"] = "2026-11-02 09:00"
    tasks.save(ts)
    n = tasks.tick(lambda sid, text: "好的，大盘平稳。", push=push, at=_t("2026-11-02 11:00"))  # 晚了 2 小时：补跑并标「补发」
    assert n == 1 and "补发" in pushed[0][0] and pushed[0][1] == f"task:{t['id']}:2026-11-02 09:00"
    assert ledger.job_get(pushed[0][1])["status"] == "done"
    # 同一个计划时间再来一次（例如重试）：不重复跑、不重复推
    assert tasks.run_one({**tasks.load()[0], "next_run": "2026-11-02 09:00"}, lambda *a: "x", push=push, at=_t("2026-11-02 11:01")).get("skipped")
    assert len(pushed) == 1


def test_task_stale_skipped_by_policy(home, monkeypatch):
    from stock_agent import tasks
    monkeypatch.setenv("TASK_CATCHUP_HOURS", "1")
    tasks.add("x", "daily", "09:00")
    ts = tasks.load()
    ts[0]["next_run"] = "2026-11-02 09:00"
    tasks.save(ts)
    assert tasks.tick(lambda *a: "y", push=lambda *a, **k: [], at=_t("2026-11-02 11:00")) == 0


def test_task_recover_after_crash(home):
    from stock_agent import ledger, tasks
    t = tasks.add("x", "daily", "09:00")
    key = f"task:{t['id']}:2026-11-02 09:00"
    ledger.job_claim(key, "task", "2026-11-02 09:00")  # 上个进程跑到一半挂了：job 停在 running
    ledger._DB.clear()
    ran = []
    n = tasks.recover(lambda sid, text: ran.append(sid) or "补上了", push=lambda *a, **k: [], at=_t("2026-11-02 09:20"))
    assert n == 1 and ran == ["task-" + t["id"]] and ledger.job_get(key)["status"] == "done"
    assert tasks.recover(lambda *a: "again", push=lambda *a, **k: [], at=_t("2026-11-02 09:25")) == 0


# ------------------------------------------------------------------ serve：启动自动续跑 / 定时任务续跑
def test_serve_auto_resume_and_task_runner(home, monkeypatch):
    from stock_agent import ledger, serve
    calls = []

    class FakeAg:
        session = "x"

    def fake_resume(rid, mem=None, on_event=None, stop=None):
        calls.append(rid)
        ledger.run_finish(rid, "done", "续上了")
        return FakeAg(), "续上了"
    monkeypatch.setattr(serve.Agent, "resume", staticmethod(fake_resume))
    monkeypatch.setattr(serve.time, "sleep", lambda s: None)
    r1 = ledger.run_start("web-abc", "web", "问题一", 1)
    r2 = ledger.run_start("web-task-t1", "web", "[定时任务…", 2)  # 定时任务的不自动续（由任务补跑接管）
    r3 = ledger.run_start("telegram-1", "telegram", "tg", 3)
    old = (datetime.now(CN) - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")  # 上个进程 2 分钟前就没动静了
    for r in (r1, r2, r3):
        ledger._x("UPDATE runs SET updated=? WHERE run_id=?", (old, r))
    serve.auto_resume()
    assert calls == [r1]
    assert serve.SESSIONS["web:abc"]["agent"].session == "x"
    # 定时任务：有中断的运行就续跑，不重新开一轮
    monkeypatch.setattr(serve, "chat", lambda ch, text: calls.append(("chat", ch)) or "新跑")
    assert serve.task_runner("task-t1", "[定时任务…") == "续上了" and calls[-1] == r2
    assert serve.task_runner("task-t1", "[定时任务…") == "新跑"


@pytest.mark.parametrize("terminal", ["done", "cancelled", "partial"])
def test_task_runner_stale_terminal_snapshot_never_restarts(home, monkeypatch, terminal):
    from stock_agent import agent as AG, ledger, serve
    counter = {}
    a = _agent(ScriptLLM(crash_at=2), counter, session="web-task-terminal")
    with pytest.raises(RuntimeError):
        a.ask("查行情并记判断")
    rid = a._run_id
    monkeypatch.setattr(serve, "MEM", a.mem)
    monkeypatch.setattr(serve, "SESSIONS", {})
    monkeypatch.setattr(AG.Agent, "__init__", _patched_init(AG.Agent.__init__, ScriptLLM(), counter))
    original = ledger.interrupted
    settled = {}

    def stale_snapshot(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        # Deterministic second executor: real resume completes and releases its lock
        # after discovery, before task_runner's own real resume acquires that lock.
        settled["reply"] = serve.resume_run(rid)
        if terminal != "done":
            ledger.run_finish(rid, terminal, "已停止的部分结果")
        settled["run"] = ledger.run_get(rid)
        return snapshot

    monkeypatch.setattr(ledger, "interrupted", stale_snapshot)
    reply = serve.task_runner("task-terminal", "查行情并记判断")
    assert counter == {"get_quotes": 1, "record_call": 1}
    assert ledger.run_get(rid) == settled["run"]
    assert len(ledger.runs_list()) == 1
    if terminal == "done":
        assert reply == settled["reply"]
    else:
        assert terminal in reply and "已停止的部分结果" in reply
    # A subsequent scheduled invocation must not be blocked by the prior terminal.
    monkeypatch.setattr(ledger, "interrupted", original)
    serve.task_runner("task-terminal", "下一计划：查行情并记判断")
    assert len(ledger.runs_list()) == 2
    assert ledger.run_get(rid) == settled["run"]
    assert counter == {"get_quotes": 2, "record_call": 2}


@pytest.mark.parametrize("busy", [False, True])
def test_task_runner_resume_error_does_not_restart(home, monkeypatch, busy):
    from stock_agent import agent as AG, ledger, serve
    counter = {}
    a = _agent(ScriptLLM(crash_at=2), counter, session="web-task-error")
    with pytest.raises(RuntimeError):
        a.ask("查行情并记判断")
    rid = a._run_id
    monkeypatch.setattr(serve, "MEM", a.mem)
    monkeypatch.setattr(serve, "SESSIONS", {})
    llm = ScriptLLM(crash_at=1)
    monkeypatch.setattr(AG.Agent, "__init__", _patched_init(AG.Agent.__init__, llm, counter))
    chats = []
    monkeypatch.setattr(serve, "chat", lambda *args: chats.append(args) or "unsafe restart")
    if busy:
        # Simulate a different executor owning the actual ledger lock.
        from concurrent.futures import ThreadPoolExecutor
        with ledger.run_lock(rid), ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(ledger.RunBusy):
                pool.submit(serve.task_runner, "task-error", "retry").result()
    else:
        for _ in range(2):
            llm.calls = 0
            with pytest.raises(RuntimeError, match="进程被杀"):
                serve.task_runner("task-error", "retry")
    assert ledger.run_get(rid)["status"] == "failed"
    assert len(ledger.runs_list()) == 1
    assert chats == []
    assert counter == {"get_quotes": 1, "record_call": 1}


def test_task_runner_checkpoint_failure_stays_paused_on_later_calls(home, monkeypatch):
    from stock_agent import agent as AG, ledger, serve
    counter = {}
    a = _agent(ScriptLLM(crash_at=2), counter, session="web-task-paused")
    with pytest.raises(RuntimeError):
        a.ask("查行情并记判断")
    rid = a._run_id
    monkeypatch.setattr(serve, "MEM", a.mem)
    monkeypatch.setattr(AG.Agent, "__init__", _patched_init(AG.Agent.__init__, ScriptLLM(), counter))
    chats = []
    monkeypatch.setattr(serve, "chat", lambda *args: chats.append(args) or "unsafe replay")
    original = ledger.step_get

    def unreadable(run, step):
        if run == rid:
            raise ledger.CheckpointError("断点不可读")
        return original(run, step)

    monkeypatch.setattr(ledger, "step_get", unreadable)
    for _ in range(2):
        with pytest.raises(ledger.CheckpointError):
            serve.task_runner("task-paused", "查行情并记判断")
        assert ledger.run_get(rid)["status"] == "unknown"
        assert chats == []
    assert counter == {"get_quotes": 1, "record_call": 1}


@pytest.mark.parametrize("status", ["running", "failed"])
def test_task_runner_old_pending_is_not_superseded(home, monkeypatch, status):
    from stock_agent import ledger, serve
    rid = ledger.run_start("web-task-old", "web", "old", None)
    ledger.step_claim(rid, 0, "tool", "record_call", {})
    ledger.run_finish(rid, status)
    ledger._x("UPDATE runs SET started='2000-01-01 00:00:00' WHERE run_id=?", (rid,))
    newer = ledger.run_start("web-task-old", "web", "new", None)
    chats = []
    monkeypatch.setattr(serve, "chat", lambda *args: chats.append(args))
    monkeypatch.setattr(serve, "resume_run", lambda *args: chats.append(args))
    for _ in range(2):
        with pytest.raises(ledger.CheckpointError):
            serve.task_runner("task-old", "old")
    assert chats == []
    assert ledger.run_get(rid)["status"] == status
    assert ledger.run_get(newer)["status"] == "running"


def test_task_runner_status_changes_to_unknown_before_resume(home, monkeypatch):
    from stock_agent import ledger, serve
    rid = ledger.run_start("web-task-race", "web", "test", None)
    original = serve.resume_run

    def changed(run):
        ledger.run_finish(run, "unknown")
        return original(run)

    monkeypatch.setattr(serve, "resume_run", changed)
    chats = []
    monkeypatch.setattr(serve, "chat", lambda *args: chats.append(args))
    with pytest.raises(ledger.CheckpointError):
        serve.task_runner("task-race", "test")
    assert chats == []
    assert ledger.run_get(rid)["status"] == "unknown"


@pytest.mark.parametrize("status", ["unknown", "running"])
def test_startup_prune_does_not_forget_unresolved_task(home, monkeypatch, status):
    from stock_agent import ledger, serve
    rid = ledger.run_start("web-task-retained", "web", "test", None)
    if status == "running":
        ledger.step_claim(rid, 0, "tool", "record_call", {})
    ledger.run_finish(rid, status)
    ledger._x("UPDATE runs SET started='2000-01-01 00:00:00', updated='2000-01-01 00:00:00' WHERE run_id=?", (rid,))
    monkeypatch.setattr(serve.time, "sleep", lambda _: None)
    monkeypatch.setenv("RUN_RESUME", "off")
    serve.auto_resume()
    chats = []
    monkeypatch.setattr(serve, "chat", lambda *args: chats.append(args))
    with pytest.raises(ledger.CheckpointError):
        serve.task_runner("task-retained", "test")
    assert chats == []
    assert ledger.run_get(rid)["status"] == status


def test_agent_confirm_tools_respect_consent(home):
    from stock_agent import exports, ledger
    exports.write("报表", "md", content="v1", replace=True)
    pend = exports.write("报表", "md", content="v2", replace=True)["pending_approval"]
    counter = {}

    class Approver(ScriptLLM):
        def chat(self, messages, tools=None, **kw):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
                    "name": "confirm_action", "arguments": json.dumps({"id": pend, "approve": True})}}]}
            return {"role": "assistant", "content": [m for m in messages if m["role"] == "tool"][-1]["content"][:200]}

    from stock_agent.agent import Agent
    a = Agent(channel="web")
    a.llm = Approver()
    a.tools = [t for t in a.tools if t["name"] in ("confirm_action", "pending_actions")]
    out = a.ask("报表那个怎么样了")  # 用户没表态：模型想批准也被拦下
    assert "guard:consent" in out and ledger.approval_get(pend)["status"] == "pending"
    b = Agent(channel="web")
    b.llm = Approver()
    b.tools = a.tools
    b.ask("确认，覆盖吧")
    assert ledger.approval_get(pend)["status"] == "approved"
    assert (exports.root() / "报表.md").read_text(encoding="utf-8") == "v2"
