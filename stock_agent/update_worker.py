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
        carry_installer_files(install, stage)
        install.rename(backup); moved = True
        stage.rename(install)
        child = launch(install)
        if not ready(child):
            raise RuntimeError("新版未能启动，恢复旧版")
        prune_backups(install, keep=backup)
        return {"ok": True, "backup": str(backup)}
    except Exception as exc:
        if not moved and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if child is not None and child.poll() is None:
            child.terminate(); child.wait(timeout=10)
        if moved:
            if install.exists():
                shutil.rmtree(install)
            backup.rename(install)
            launch(install)
        return {"ok": False, "rollback": moved, "error": str(exc)[:200]}


def carry_installer_files(install: Path, stage: Path):
    """安装包（NSIS）在程序目录里写的文件（Uninstall.exe 等）不在 zip 里；换目录时带过去，免得卸载入口失效。"""
    for item in install.iterdir():
        if item.is_file() and not (stage / item.name).exists():
            shutil.copy2(item, stage / item.name)


def prune_backups(install: Path, keep: Path):
    """只留最近一份旧版（用于回滚），更早的 previous-* 和残留的 next-* 删掉，免得每次更新多占几百 MB。"""
    for item in install.parent.iterdir():
        if item != keep and item.is_dir() and item.name.startswith((install.name + ".previous-", install.name + ".next-")):
            shutil.rmtree(item, ignore_errors=True)


def update_registry(install: Path, version: str):
    """安装版：把「应用和功能」里的版本号改成新版（绿色版没有这个键，跳过）。"""
    if os.name != "nt":
        return
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Stoa"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            location = winreg.QueryValueEx(k, "InstallLocation")[0]
            if Path(location).resolve() == install.resolve():
                winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, version)
    except OSError:
        pass


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
    if result.get("ok"):
        update_registry(Path(plan["install"]), plan["version"])
    result["time"] = time.time()
    Path(plan["result"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
