"""Run from a copied runtime outside the install directory; keep the previous install for rollback."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def replace_install(plan, launch, ready):
    install, source, backup, stage = (Path(plan[k]).resolve() for k in ("install", "source", "backup", "stage"))
    if backup.parent != install.parent or stage.parent != install.parent:
        raise ValueError("更新目录必须与安装目录同级")
    if not backup.name.startswith(install.name + ".previous-") or not stage.name.startswith(install.name + ".next-"):
        raise ValueError("更新目录名称无效")
    moved, child = False, None
    try:
        shutil.copytree(source, stage)
        install.rename(backup); moved = True
        stage.rename(install)
        child = launch(install)
        if not ready(child):
            raise RuntimeError("新版未能启动，恢复旧版")
        return {"ok": True, "backup": str(backup)}
    except Exception as exc:
        if child is not None and child.poll() is None:
            child.terminate(); child.wait(timeout=10)
        if moved:
            if install.exists():
                shutil.rmtree(install)
            backup.rename(install)
            launch(install)
        return {"ok": False, "rollback": moved, "error": str(exc)[:200]}


def wait_parent(pid):
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if handle:
            try:
                if kernel.WaitForSingleObject(ctypes.c_void_p(handle), 60000) != 0:
                    raise TimeoutError("原程序未退出，取消更新")
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))


def wait_ready(child, port, version, token="", timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", headers={"X-Token": token})
            with urllib.request.urlopen(request, timeout=min(1, max(.01, deadline - time.monotonic()))) as response:
                if json.load(response).get("version") == version:
                    return True
        except Exception:
            pass
        time.sleep(.1)
    return False


def main():
    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    wait_parent(plan["pid"])
    def launch(path):
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        return subprocess.Popen([str(path / "python" / "pythonw.exe"), "-B", "-m", "stock_agent", "desktop"],
                                cwd=path, env=dict(os.environ), **flags)
    def ready(child):
        return wait_ready(child, plan["port"], plan["version"], os.getenv("WEB_TOKEN", ""))
    result = replace_install(plan, launch, ready)
    result["time"] = time.time()
    Path(plan["result"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
