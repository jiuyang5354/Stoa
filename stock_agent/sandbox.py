"""代码只在独立 Docker 容器中执行；不回退到应用进程的用户权限。"""
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .memory import home

IMAGE = "stoa-sandbox:local"


def enabled() -> bool:
    return os.getenv("ALLOW_CODE", "0") == "1"


def stage_input(fid: str, source: Path) -> str | None:
    """Copy an uploaded table into the one directory visible to the container."""
    workspace = home().resolve() / "workspace"
    target = workspace / "uploads" / fid / source.name
    if workspace.resolve() != workspace or not target.resolve().is_relative_to(workspace):
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError:
        return None
    return "/workspace/" + target.relative_to(workspace).as_posix()


def run_python(code: str, timeout: int = 60) -> str:
    if not enabled():
        return "代码执行未开启（设置 ALLOW_CODE=1）。"
    docker = shutil.which("docker")
    if not docker:
        return "代码执行需要 Docker 隔离环境；当前不可用，未在本机执行代码。"
    ws = (home() / "workspace").resolve()
    if ws != home().resolve() / "workspace":
        return "workspace 不能是指向其他目录的链接，未执行代码。"
    ws.mkdir(parents=True, exist_ok=True)
    uid = os.getuid() if hasattr(os, "getuid") and os.getuid() else 65534
    gid = os.getgid() if uid != 65534 else 65534
    name = "stoa-code-" + uuid.uuid4().hex
    # Docker 客户端只继承定位运行时所需的环境；不把应用的密钥交给子进程或容器。
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "TEMP", "TMP", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    }}
    argv = [docker, "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=64", "--memory=1g", "--memory-swap=1g", "--cpus=1", "--log-driver=none",
            f"--user={uid}:{gid}", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", f"type=bind,source={ws},target=/workspace", "--workdir=/workspace",
            "--env", "MPLBACKEND=Agg", IMAGE, "python", "-I", "-c", code]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        r = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, **flags)
    except subprocess.TimeoutExpired:
        return f"超时（>{timeout}s），已终止。"
    except OSError:
        return "Docker 隔离环境启动失败，未在本机执行代码。"
    finally:
        # 杀掉 CLI 不等于停止容器；超时和异常时也清理独立任务。
        try:
            subprocess.run([docker, "rm", "-f", name], env=env, capture_output=True, timeout=10, **flags)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if r.returncode == 125:
        return f"Docker 隔离环境不可用；请确认 Docker 已运行且已构建 {IMAGE} 镜像。未在本机执行代码。"
    out = (r.stdout or "") + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
    out = out.strip() or (f"执行失败（退出码 {r.returncode}）。" if r.returncode else "（无输出）")
    return out[:5000] + ("\n…（已截断）" if len(out) > 5000 else "")
