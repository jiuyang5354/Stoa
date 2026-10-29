"""检查更新与一键更新（Windows 绿色版）。

- check()：问 GitHub Releases 最新版本（STOA_REPO，默认 jiuyang5354/Stoa），结果缓存 6 小时
- apply()：下载 Stoa-Windows.zip → 解压到临时目录 → 写一个 .bat：等当前进程退出后，
  用 robocopy 把新文件盖到程序文件夹，再重新打开 Stoa.exe。数据在 %LOCALAPPDATA%\\Stoa，不受影响。
  STOA_UPDATE=off 可关闭检查。
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

from . import __version__

APP = Path(__file__).resolve().parent.parent  # 绿色版里是 Stoa\app
INSTALL = APP.parent                           # 绿色版里是 Stoa\
_CACHE = {"t": 0, "v": None}


def repo() -> str:
    return os.getenv("STOA_REPO", "jiuyang5354/Stoa")


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3]) or (0,)


def portable() -> bool:
    """是不是 Windows 绿色版（有 Stoa.exe 和自带 python）。"""
    return sys.platform == "win32" and (INSTALL / "Stoa.exe").exists() and (INSTALL / "python").is_dir()


def check(force: bool = False) -> dict:
    out = {"current": __version__, "portable": portable(), "enabled": os.getenv("STOA_UPDATE", "on") != "off"}
    if not out["enabled"]:
        return out
    if not force and _CACHE["v"] and time.time() - _CACHE["t"] < 6 * 3600:
        return {**out, **_CACHE["v"]}
    try:
        r = requests.get(f"https://api.github.com/repos/{repo()}/releases/latest", timeout=15,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "Stoa-updater"})
        r.raise_for_status()
        j = r.json()
        asset = next((a for a in j.get("assets") or [] if a["name"] == "Stoa-Windows.zip"), None)
        v = {"latest": j["tag_name"].lstrip("v"), "name": j.get("name") or j["tag_name"], "notes": (j.get("body") or "")[:1500],
             "url": j["html_url"], "asset": asset and asset["browser_download_url"], "size": asset and asset["size"],
             "published": (j.get("published_at") or "")[:10]}
        v["available"] = _ver(v["latest"]) > _ver(__version__)
        _CACHE.update(t=time.time(), v=v)
        return {**out, **v}
    except Exception as e:
        return {**out, "error": f"检查更新失败：{str(e)[:80]}"}


def apply() -> dict:
    info = check(force=True)
    if not info.get("available"):
        return {"error": "已经是最新版本。"}
    if not info.get("portable"):
        return {"error": "只有 Windows 绿色版能一键更新；其它方式请 git pull 或拉新镜像。", "url": info.get("url")}
    if not info.get("asset"):
        return {"error": "新版本的 Windows 包还在打包，过几分钟再试。", "url": info.get("url")}
    tmp = Path(tempfile.mkdtemp(prefix="stoa-update-"))
    z = tmp / "Stoa-Windows.zip"
    with requests.get(info["asset"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(z, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if info.get("size") and z.stat().st_size != info["size"]:
        return {"error": "下载不完整，请重试。"}
    with zipfile.ZipFile(z) as zf:
        bad = zf.testzip()
        if bad:
            return {"error": f"压缩包损坏：{bad}"}
        zf.extractall(tmp / "x")
    src = tmp / "x" / "Stoa"
    if not (src / "Stoa.exe").exists():
        return {"error": "压缩包结构不对，已取消。"}
    bat = tmp / "update.bat"
    bat.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        f":wait\r\ntasklist /FI \"PID eq {os.getpid()}\" | find \"{os.getpid()}\" >nul && (timeout /t 1 /nobreak >nul & goto wait)\r\n"
        f"robocopy \"{src}\" \"{INSTALL}\" /E /R:3 /W:1 /NFL /NDL /NJH /NJS >nul\r\n"
        f"start \"\" \"{INSTALL / 'Stoa.exe'}\"\r\n"
        f"rmdir /s /q \"{tmp}\"\r\n", encoding="utf-8")
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen(["cmd.exe", "/c", str(bat)], creationflags=flags, close_fds=True, cwd=str(tmp))
    return {"msg": f"正在更新到 v{info['latest']}，Stoa 会自动重开。", "restart": True}
