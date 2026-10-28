"""桌面模式：Windows 绿色版双击 Stoa.exe 走这里。

- 数据目录默认 %LOCALAPPDATA%\\Stoa（记忆、账本、config.yaml、.env 都在这，升级覆盖程序文件不会丢）
- 读数据目录里的 .env；首次运行从模板生成并用记事本打开，让用户填模型密钥
- 只绑 127.0.0.1，端口被占就顺延，启动后自动打开浏览器
"""
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    if os.getenv("STOCK_AGENT_HOME"):
        return Path(os.environ["STOCK_AGENT_HOME"])
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "Stoa"


def load_env(p: Path):
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and k not in os.environ:
            os.environ[k] = v


def free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def open_file(p: Path):
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # 记事本 / 默认编辑器
    except OSError:
        pass


def main():
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["STOCK_AGENT_HOME"] = str(home)
    env = home / ".env"
    first = not env.exists()
    if first and (APP / ".env.example").exists():
        shutil.copy(APP / ".env.example", env)
    if env.exists():
        load_env(env)

    port = free_port(int(os.getenv("PORT", "8080")))
    os.environ["PORT"], os.environ["HOST"] = str(port), "127.0.0.1"
    token = os.getenv("WEB_TOKEN", "")
    url = f"http://127.0.0.1:{port}/" + (f"?token={token}" if token else "")

    print("=" * 56)
    print("  Stoa · 股市 Agent")
    print(f"  界面    {url}")
    print(f"  数据    {home}")
    print("  关闭这个窗口即退出")
    print("=" * 56)
    if not os.getenv("LLM_API_KEY") and not any(os.getenv(k) for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY")) and os.getenv("LLM_PROVIDER") not in ("ollama", "lmstudio", "vllm"):
        print("\n  还没填大模型密钥：已打开 .env，填好 LLM_PROVIDER 和 LLM_API_KEY 后保存，")
        print("  关掉本窗口再双击 Stoa.exe 即可。没填也能看行情，只是不能对话。\n")
        open_file(env)

    def browse():
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.5)
        if os.getenv("NO_BROWSER") != "1":
            webbrowser.open(url)

    threading.Thread(target=browse, daemon=True).start()
    from .serve import main as serve
    serve()
