"""Docker 不可用时的本机受限模式，以及 CODE_SANDBOX=docker 强制只用容器。"""
import subprocess

from stock_agent import sandbox


def _setup(tmp_path, monkeypatch, docker=None):
    monkeypatch.setenv("ALLOW_CODE", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("SOME_WEBHOOK", "https://hook.example")
    monkeypatch.setattr(sandbox, "home", lambda: tmp_path)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: docker)


def test_no_docker_falls_back_to_local_restricted_mode(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, docker=None)
    monkeypatch.delenv("CODE_SANDBOX", raising=False)
    code = """
import os
from pathlib import Path
assert 'LLM_API_KEY' not in os.environ and 'SOME_WEBHOOK' not in os.environ
assert Path.cwd().resolve() == Path('/workspace').resolve()
Path('/workspace/out.txt').write_text('ok')
print('中文输出', sum(range(4)))
"""
    out = sandbox.run_python(code, timeout=30)
    assert out.startswith(sandbox.LOCAL_NOTE), out
    assert "中文输出 6" in out
    assert (tmp_path / "workspace" / "out.txt").read_text() == "ok"


def test_docker_only_refuses_without_docker(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, docker=None)
    monkeypatch.setenv("CODE_SANDBOX", "docker")
    out = sandbox.run_python("open('x.txt','w').write('1')", timeout=30)
    assert "CODE_SANDBOX=docker" in out and "未在本机执行" in out
    assert not (tmp_path / "workspace" / "x.txt").exists()


def test_docker_daemon_error_falls_back(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, docker="/usr/bin/docker")
    monkeypatch.delenv("CODE_SANDBOX", raising=False)
    real = subprocess.run

    def fake(argv, *a, **kw):
        if argv[0] == "/usr/bin/docker":
            return subprocess.CompletedProcess(argv, 125, "", "Cannot connect to the Docker daemon")
        return real(argv, *a, **kw)

    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    out = sandbox.run_python("print(1+1)", timeout=30)
    assert out == sandbox.LOCAL_NOTE + "\n2"


def test_docker_preferred_when_available(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, docker="/usr/bin/docker")
    calls = []

    def fake(argv, *a, **kw):
        calls.append(argv)
        if argv[1] == "run":
            assert "--network=none" in argv and "LLM_API_KEY" not in kw["env"]
            return subprocess.CompletedProcess(argv, 0, "from-container\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake)
    assert sandbox.run_python("print(1)", timeout=30) == "from-container"
    assert calls[0][1] == "run" and calls[-1][1:3] == ["rm", "-f"]


def test_local_timeout_and_truncation(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, docker=None)
    assert "超时" in sandbox.run_python("import time; time.sleep(5)", timeout=1)
    out = sandbox.run_python("print('x' * 20000)", timeout=30)
    assert out.endswith("（已截断）") and len(out) < sandbox.MAX_OUT + 200


def test_workspace_paths_are_rewritten_for_local_mode(tmp_path):
    ws = tmp_path / "workspace"
    code = "pd.read_csv('/workspace/uploads/a/持仓.csv'); os.chdir(\"/workspace\"); x='/workspaces/no'"
    out = sandbox.local_code(code, ws)
    assert f"'{ws.as_posix()}/uploads/a/持仓.csv'" in out and f'"{ws.as_posix()}"' in out
    assert "'/workspaces/no'" in out
