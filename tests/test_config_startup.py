"""首次启动多个线程同时建 / 读 config.yaml（v0.34.1 Windows 自检失败：读时 PermissionError）。"""
from concurrent.futures import ThreadPoolExecutor
import threading

from stock_agent import memory


def test_first_run_config_created_once_and_never_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    replaced = []
    original = memory.os.replace
    monkeypatch.setattr(memory.os, "replace", lambda a, b: (replaced.append(b), original(a, b)))
    start = threading.Barrier(8)

    def work(_):
        start.wait()
        return memory.read_config_text()

    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(work, range(8)))
    assert len(replaced) == 1
    assert all(t and "watchlist" in t for t in texts)
    assert not list(tmp_path.glob("config.yaml.*.tmp"))


def test_read_retries_while_file_is_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    memory.config_path()
    calls = {"n": 0}
    original = type(tmp_path).read_text

    def flaky(path, *a, **k):
        if path.name == "config.yaml" and calls["n"] < 3:
            calls["n"] += 1
            raise PermissionError(13, "Permission denied")
        return original(path, *a, **k)

    monkeypatch.setattr(type(tmp_path), "read_text", flaky)
    assert "watchlist" in memory.read_config_text()
    assert calls["n"] == 3
