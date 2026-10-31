from concurrent.futures import ThreadPoolExecutor
import threading
import time

from stock_agent.memory import Memory


def test_parallel_chat_writes_and_history_search(tmp_path):
    mem = Memory(tmp_path)
    mem.log("web-seed", "user", "并发搜索核验")
    mem.log("web-seed", "assistant", "已保存")
    start = threading.Barrier(8)

    def work(i):
        start.wait()
        for j in range(30):
            if i % 2 == 0:
                mem.log(f"web-{i}", "user", f"并发搜索核验 {j}")
                mem.log(f"web-{i}", "assistant", "回答")
            else:
                assert mem.sessions(query="并发搜索核验")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    assert mem.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 242


def test_read_during_atomic_memory_replacement(tmp_path, monkeypatch):
    import stock_agent.memory as module
    mem = Memory(tmp_path)
    mem.overwrite("memory", ["old snapshot"])
    replacing, release = threading.Event(), threading.Event()
    original_replace = module.os.replace
    original_read = type(tmp_path).read_text

    def replace(source, destination):
        replacing.set()
        assert release.wait(2)
        return original_replace(source, destination)

    def read(path, *args, **kwargs):
        if path.name == "MEMORY.md" and replacing.is_set() and not release.is_set():
            raise PermissionError("Windows file replacement in progress")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(type(tmp_path), "read_text", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(mem.overwrite, "memory", ["new snapshot"])
        assert replacing.wait(2)
        reader = pool.submit(mem.entries, "memory")
        time.sleep(.05)
        release.set()
        writer.result()
        assert reader.result()[0].endswith("new snapshot")
