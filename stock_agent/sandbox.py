"""Agent 的代码执行工具。容器本身就是沙箱（非 root、只读根文件系统、无特权），
这里再加一层：独立子进程、超时、去掉所有密钥环境变量、限制内存、输出截断。"""
import os
import subprocess
import sys
from pathlib import Path

from .memory import home

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "WEBHOOK", "PASSWORD")


def enabled() -> bool:
    return os.getenv("ALLOW_CODE", "0") == "1"


def _limits():
    try:
        import resource
        mem = 1024 * 1024 * 1024  # 1 GB
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except Exception:
        pass


def run_python(code: str, timeout: int = 60) -> str:
    if not enabled():
        return "代码执行未开启（设置 ALLOW_CODE=1）。"
    ws = home() / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not any(h in k.upper() for h in SECRET_HINTS)}
    env["MPLBACKEND"] = "Agg"
    env["PYTHONIOENCODING"] = "utf-8"   # Windows 默认 GBK，打印中文/emoji 会崩
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=ws, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, preexec_fn=_limits if os.name == "posix" else None)
    except subprocess.TimeoutExpired:
        return f"超时（>{timeout}s），已终止。"
    out = (r.stdout or "") + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
    out = out.strip() or "（无输出）"
    return out[:5000] + ("\n…（已截断）" if len(out) > 5000 else "")
