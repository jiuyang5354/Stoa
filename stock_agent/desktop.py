"""桌面模式：Windows 绿色版双击 Stoa.exe 走这里。

- 数据目录默认 %LOCALAPPDATA%\\Stoa（记忆、账本、config.yaml、.env 都在这，升级覆盖程序文件不会丢）
- 读数据目录里的 .env；首次运行从模板生成并用记事本打开，让用户填模型密钥
- 只绑 127.0.0.1，端口被占就顺延
- 界面开在独立的 Stoa 窗口里：优先 pywebview（Windows 自带的 Edge WebView2 内核），
  不行就用 Edge/Chrome 的 --app 无地址栏窗口，再不行才退回默认浏览器。关窗口即退出。
- Stoa.exe 用 pythonw 启动、没有控制台；输出写到数据目录的 stoa.log，出错弹框
"""
import os
import subprocess
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


def msgbox(text: str, title: str = "Stoa"):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
            return
        except Exception:
            pass
    print(text)


def wait_port(port: int, secs: float = 40) -> bool:
    end = time.time() + secs
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def icon_path():
    for p in (APP.parent / "Stoa.ico", APP / "packaging" / "windows" / "icon.ico"):
        if p.exists():
            return str(p)
    return None


def window_webview(url: str, home: Path) -> bool:
    """原生窗口。阻塞到窗口关闭；不可用返回 False。"""
    try:
        import webview
    except Exception as e:
        print("pywebview 不可用:", e)
        return False
    try:
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True  # 设置页「导出备份」
        except Exception:
            pass
        webview.create_window("Stoa · 股市", url, width=1440, height=920, min_size=(960, 640),
                              background_color="#EFECE6", text_select=True)
        kw = {"private_mode": False, "storage_path": str(home / "webview")}
        if sys.platform == "win32":
            kw["gui"] = "edgechromium"
        ic = icon_path()
        if ic:
            kw["icon"] = ic
        try:
            webview.start(**kw)
        except TypeError:
            kw.pop("icon", None)
            webview.start(**kw)
        return True
    except Exception as e:
        print("pywebview 启动失败:", e)
        return False


def window_app(url: str, home: Path) -> bool:
    """Edge/Chrome 的 --app 窗口（无地址栏、无标签页），独立配置目录，阻塞到关闭。"""
    pf = [os.getenv(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA") if os.getenv(k)]
    cands = [Path(b) / r for b in pf for r in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe")]
    cands += [Path(p) for p in map(shutil.which, ("msedge", "chromium", "google-chrome")) if p]
    for exe in cands:
        if exe.exists():
            try:
                subprocess.run([str(exe), f"--app={url}", f"--user-data-dir={home / 'appwin'}",
                                "--window-size=1440,920", "--no-first-run", "--no-default-browser-check"])
                return True
            except OSError:
                continue
    return False


def main():
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None or os.getenv("STOA_LOG") == "1":  # pythonw 没有控制台
        log = open(home / "stoa.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print("\n----", time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        _main(home)
    except Exception as e:
        import traceback
        traceback.print_exc()
        msgbox(f"Stoa 启动失败：{e}\n\n详细日志：{home / 'stoa.log'}")
        os._exit(1)


def _main(home: Path):
    if sys.platform == "win32":  # 只开一个，避免两套定时报告
        import ctypes
        k = ctypes.windll.kernel32
        k.CreateMutexW(None, False, "Local\\StoaDesktop")
        if k.GetLastError() == 183:
            msgbox("Stoa 已经在运行了。")
            os._exit(0)
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
    print(f"Stoa  界面 {url}  数据 {home}")

    no_key = not os.getenv("LLM_API_KEY") and not any(os.getenv(k) for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY")) and os.getenv("LLM_PROVIDER") not in ("ollama", "lmstudio", "vllm")
    if no_key:
        open_file(env)

    from .serve import main as serve
    threading.Thread(target=serve, daemon=True).start()
    if not wait_port(port):
        raise RuntimeError(f"本机服务没能在 127.0.0.1:{port} 启动")
    if no_key and first:
        threading.Thread(target=msgbox, args=("还没填大模型密钥。\n\n已用记事本打开 .env：填好 LLM_PROVIDER 和 LLM_API_KEY 后保存，关掉 Stoa 再打开即可。\n没填也能看行情，只是不能对话。",), daemon=True).start()

    mode = os.getenv("STOA_WINDOW", "auto")  # auto | webview | app | browser | none
    if mode in ("auto", "webview") and window_webview(url, home):
        os._exit(0)
    if mode in ("auto", "app") and window_app(url, home):
        os._exit(0)
    if mode != "none":
        webbrowser.open(url)
    while True:  # 退回浏览器时常驻，靠结束进程退出
        time.sleep(3600)
