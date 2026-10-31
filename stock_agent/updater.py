"""检查更新与一键更新（Windows 绿色版）。

- check()：问 GitHub Releases 最新版本（STOA_REPO，默认 jiuyang5354/Stoa），结果缓存 6 小时
- apply()：校验 SHA-256 → 安装到同级暂存目录 → 保留旧版 → 启动检查，失败回滚。
  STOA_UPDATE=off 可关闭检查。
"""
import os
import hashlib
import json
import re
import shutil
import stat
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
    result = INSTALL.parent / (INSTALL.name + ".update-result.json")
    if result.exists():
        try:
            out["last_update"] = json.loads(result.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
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
             "digest": asset and asset.get("digest"),
             "published": (j.get("published_at") or "")[:10]}
        v["available"] = _ver(v["latest"]) > _ver(__version__)
        _CACHE.update(t=time.time(), v=v)
        return {**out, **v}
    except Exception as e:
        return {**out, "error": f"检查更新失败：{str(e)[:80]}"}


def verify_download(path: Path, digest: str | None):
    if not digest or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise ValueError("发布包缺少可信 SHA-256，已取消更新")
    with path.open("rb") as file:
        hasher = hashlib.sha256()
        for chunk in iter(lambda: file.read(1 << 20), b""):
            hasher.update(chunk)
        actual = hasher.hexdigest()
    if actual.lower() != digest[7:].lower():
        raise ValueError("下载包 SHA-256 不匹配，已取消更新")


def extract_package(path: Path, target: Path):
    root = target.resolve()
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            dest = (root / item.filename).resolve()
            if not dest.is_relative_to(root) or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("压缩包包含越界路径或链接，已取消更新")
        if archive.testzip():
            raise ValueError("压缩包损坏，已取消更新")
        archive.extractall(root)


def apply() -> dict:
    info = check(force=True)
    if not info.get("available"):
        return {"error": "已经是最新版本。"}
    if not info.get("portable"):
        return {"error": "只有 Windows 绿色版能一键更新；其它方式请 git pull 或拉新镜像。", "url": info.get("url")}
    if not info.get("asset"):
        return {"error": "新版本的 Windows 包还在打包，过几分钟再试。", "url": info.get("url")}
    from .memory import home
    if home().resolve().is_relative_to(INSTALL.resolve()):
        return {"error": "数据目录位于程序目录内，请先迁移并备份数据后再更新。"}
    tmp = Path(tempfile.mkdtemp(prefix="stoa-update-"))
    z = tmp / "Stoa-Windows.zip"
    with requests.get(info["asset"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(z, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if info.get("size") and z.stat().st_size != info["size"]:
        return {"error": "下载不完整，请重试。"}
    try:
        verify_download(z, info.get("digest"))
        extract_package(z, tmp / "x")
    except ValueError as exc:
        return {"error": str(exc)}
    src = tmp / "x" / "Stoa"
    if not (src / "Stoa.exe").exists():
        return {"error": "压缩包结构不对，已取消。"}
    # 更新进程自身不占用安装目录内的 Python DLL，目录才能替换与回滚。
    runtime = tmp / "runtime"
    shutil.copytree(INSTALL / "python", runtime)
    worker = tmp / "update_worker.py"
    shutil.copy2(Path(__file__).with_name("update_worker.py"), worker)
    tag = str(time.time_ns())
    plan = {"install": str(INSTALL), "source": str(src), "pid": os.getpid(), "version": info["latest"],
            "port": int(os.getenv("PORT", "8080")), "backup": str(INSTALL.with_name(INSTALL.name + ".previous-" + tag)),
            "stage": str(INSTALL.with_name(INSTALL.name + ".next-" + tag)),
            "result": str(INSTALL.parent / (INSTALL.name + ".update-result.json"))}
    plan_path = tmp / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    subprocess.Popen([str(runtime / "pythonw.exe"), "-B", str(worker), str(plan_path)],
                     creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True, cwd=tmp)
    return {"msg": f"正在更新到 v{info['latest']}，Stoa 会自动重开。", "restart": True}
