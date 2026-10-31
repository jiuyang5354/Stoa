"""Agent 的代码执行工具。

优先在独立 Docker 容器里跑（无网络、只读根文件系统、只挂载 workspace、非 root）。
Docker 不可用（没装、没启动、没构建镜像）时，默认退回「本机受限模式」：独立子进程、超时、
去掉密钥环境变量、限制内存（POSIX）、工作目录固定在 workspace、输出截断，结果开头注明。
CODE_SANDBOX=docker 强制只用 Docker，不可用就拒绝执行、不回退。
"""
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .memory import home

IMAGE = "stoa-sandbox:local"
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "WEBHOOK", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
LOCAL_NOTE = "【本机受限模式：未检测到可用的 Docker 隔离环境，代码在本机独立子进程中运行（超时、去掉密钥、只在 workspace 目录内）】"
MAX_OUT = 5000


def enabled() -> bool:
    return os.getenv("ALLOW_CODE", "0") == "1"


def mode() -> str:
    """auto（默认）：Docker 优先、不可用时本机受限模式；docker：只用 Docker。"""
    m = (os.getenv("CODE_SANDBOX") or "auto").split("#", 1)[0].strip().lower()
    return "docker" if m in ("docker", "docker-only", "strict") else "auto"


def _workspace() -> Path | None:
    ws = home().resolve() / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    if ws.resolve() != ws:  # workspace 是指向别处的链接
        return None
    return ws


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


def _clip(out: str) -> str:
    return out[:MAX_OUT] + ("\n…（已截断）" if len(out) > MAX_OUT else "")


def _format(r) -> str:
    out = (r.stdout or "") + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
    out = out.strip() or (f"执行失败（退出码 {r.returncode}）。" if r.returncode else "（无输出）")
    return _clip(out)


def _docker(code: str, ws: Path, timeout: int):
    """在容器里执行。返回 (结果文字, 是否真的跑了)；Docker 不可用时第二项为 False。"""
    docker = shutil.which("docker")
    if not docker:
        return "未找到 Docker", False
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
        return f"超时（>{timeout}s），已终止。", True
    except OSError:
        return "Docker 启动失败", False
    finally:
        # 杀掉 CLI 不等于停止容器；超时和异常时也清理独立任务。
        try:
            subprocess.run([docker, "rm", "-f", name], env=env, capture_output=True, timeout=10, **flags)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if r.returncode == 125:  # docker 自己的错误（守护进程没起、镜像不存在）：代码没跑
        return f"Docker 隔离环境不可用（请确认 Docker 已运行且已构建 {IMAGE} 镜像）", False
    return _format(r), True


def _limits():
    try:
        import resource
        mem = 1024 * 1024 * 1024  # 1 GB
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except Exception:
        pass


_WS_PATH = re.compile(r"(?<![\w./\\])/workspace(?=[/\"']|$)")


def local_code(code: str, ws: Path) -> str:
    """模型按容器路径写 /workspace/…；本机模式换成真实的 workspace 绝对路径。"""
    return _WS_PATH.sub(lambda _: ws.as_posix(), code)


def _local(code: str, ws: Path, timeout: int) -> str:
    env = {k: v for k, v in os.environ.items() if not any(h in k.upper() for h in SECRET_HINTS)}
    # -I 会忽略 PYTHON* 环境变量，所以 UTF-8 用 -X utf8 打开（Windows 默认 GBK/cp1252，打印中文会崩）
    env.update(MPLBACKEND="Agg", HOME=str(ws))
    env.pop("PYTHONPATH", None)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"preexec_fn": _limits}
    try:
        r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", local_code(code, ws)], cwd=ws, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, **flags)
    except subprocess.TimeoutExpired:
        return _clip(f"{LOCAL_NOTE}\n超时（>{timeout}s），已终止。")
    except OSError as e:
        return f"{LOCAL_NOTE}\n本机子进程启动失败：{e}"
    return LOCAL_NOTE + "\n" + _format(r)


def run_python(code: str, timeout: int = 60) -> str:
    if not enabled():
        return "代码执行未开启（设置 ALLOW_CODE=1）。"
    ws = _workspace()
    if ws is None:
        return "workspace 不能是指向其他目录的链接，未执行代码。"
    out, ran = _docker(code, ws, timeout)
    if ran:
        return out
    if mode() == "docker":
        return f"{out}；已设置只用 Docker（CODE_SANDBOX=docker），未在本机执行代码。"
    return _local(code, ws, timeout)
