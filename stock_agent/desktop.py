"""桌面模式：Windows 绿色版双击 Stoa.exe 走这里。

- 数据目录默认 %LOCALAPPDATA%\\Stoa（记忆、账本、config.yaml、.env 都在这，升级覆盖程序文件不会丢）
- 读数据目录里的 .env；首次运行从模板生成，但不弹窗：模型和密钥在界面「设置 → 模型」里填
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


TITLE = "Stoa"
CAPTION = (0xEC, 0xEA, 0xE5)   # 标题栏 = 大理石底色，和界面连成一片
CAPTION_TEXT = (0x18, 0x17, 0x15)  # --ink
CAPTION_BORDER = (0xD3, 0xD0, 0xC9)


def _colorref(rgb) -> int:
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


def tint_titlebar(title: str = TITLE, tries: int = 50) -> bool:
    """Windows 11：把原生标题栏改成界面的石色（保留贴靠布局、拖拽缩放）；Windows 10 不支持，静默跳过。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        u32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        hwnd = None
        for _ in range(tries):
            hwnd = u32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            return False
        ok = True
        for attr, val in ((20, 0), (35, _colorref(CAPTION)), (36, _colorref(CAPTION_TEXT)), (34, _colorref(CAPTION_BORDER))):
            v = wintypes.DWORD(val)  # 20 深色模式关、35 标题栏底色、36 标题文字、34 窗口边框
            ok &= dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v)) == 0
        return bool(ok)
    except Exception as e:
        print("标题栏着色跳过:", e)
        return False


class WinChrome:
    """无边框窗口的自绘标题栏接口（js_api）。拖动/缩放交给 Windows 原生（WM_NCLBUTTONDOWN），贴靠和多屏都正常。"""
    HT = {10, 11, 12, 13, 14, 15, 16, 17}

    def __init__(self):
        self._win = None
        self._restore = None

    def _hwnd(self):
        if sys.platform != "win32":
            return 0
        import ctypes
        return ctypes.windll.user32.FindWindowW(None, TITLE)

    def _work_area(self, hwnd):
        import ctypes
        from ctypes import wintypes

        class MI(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        u = ctypes.windll.user32
        mi = MI(); mi.cbSize = ctypes.sizeof(MI)
        u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
        return mi.rcWork

    def _rect(self, hwnd):
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r

    def is_max(self):
        h = self._hwnd()
        if not h:
            return False
        r, w = self._rect(h), self._work_area(h)
        return (r.left, r.top, r.right, r.bottom) == (w.left, w.top, w.right, w.bottom)

    def toggle_max(self):
        """无边框窗口直接最大化会盖住任务栏，所以手动铺满当前屏幕的工作区。"""
        h = self._hwnd()
        if not h:
            return False
        import ctypes
        u = ctypes.windll.user32
        if self.is_max():
            r = self._restore
            if not r:
                w = self._work_area(h)
                cw, ch = min(1440, w.right - w.left - 80), min(920, w.bottom - w.top - 80)
                r = (w.left + (w.right - w.left - cw) // 2, w.top + (w.bottom - w.top - ch) // 2, cw, ch)
            u.SetWindowPos(h, 0, *r, 0x0014)  # NOZORDER | NOACTIVATE
            return False
        r = self._rect(h)
        self._restore = (r.left, r.top, r.right - r.left, r.bottom - r.top)
        w = self._work_area(h)
        u.SetWindowPos(h, 0, w.left, w.top, w.right - w.left, w.bottom - w.top, 0x0014)
        return True

    def minimize(self):
        h = self._hwnd()
        if h:
            import ctypes
            ctypes.windll.user32.ShowWindow(h, 6)  # SW_MINIMIZE

    def close(self):
        if self._win is not None:
            self._win.destroy()

    def _nc(self, code: int):
        h = self._hwnd()
        if h:
            import ctypes
            u = ctypes.windll.user32
            u.ReleaseCapture()
            u.PostMessageW(h, 0xA1, code, 0)  # WM_NCLBUTTONDOWN：交给系统的移动/缩放循环

    def drag(self):
        if self.is_max() and self._restore:  # 最大化时拖标题栏 = 先还原到鼠标下面
            import ctypes
            from ctypes import wintypes
            pt = wintypes.POINT(); ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            x, y, cw, ch = self._restore
            self._restore = (pt.x - cw // 2, pt.y - 16, cw, ch)
            self.toggle_max()
        self._nc(2)  # HTCAPTION

    def resize(self, edge):
        if int(edge) in self.HT and not self.is_max():
            self._nc(int(edge))


def round_corners():
    """Windows 11 圆角 + 细边框；Windows 10 静默跳过。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        for _ in range(50):
            h = ctypes.windll.user32.FindWindowW(None, TITLE)
            if h:
                break
            time.sleep(0.1)
        for attr, val in ((33, 2), (34, _colorref(CAPTION_BORDER))):  # 圆角、边框色
            v = wintypes.DWORD(val)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(h), attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception as e:
        print("圆角跳过:", e)


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
        custom = sys.platform == "win32" and os.getenv("STOA_FRAME", "custom") != "native"
        chrome = WinChrome()
        if custom:  # 自绘标题栏：和界面同一套石色与字体；STOA_FRAME=native 可退回系统标题栏
            url += ("&" if "?" in url else "?") + "frame=custom"
            win = webview.create_window(TITLE, url, width=1440, height=920, min_size=(960, 640), frameless=True, easy_drag=False,
                                        shadow=True, js_api=chrome, background_color="#ECEAE5", text_select=True)
            chrome._win = win
            after = round_corners
        else:
            win = webview.create_window(TITLE, url, width=1440, height=920, min_size=(960, 640),
                                        background_color="#ECEAE5", text_select=True)
            after = tint_titlebar
        try:
            win.events.shown += lambda: threading.Thread(target=after, daemon=True).start()
        except Exception:
            threading.Thread(target=after, daemon=True).start()
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


def smoke(port: int, home: Path):
    """真机自检（CI 用）：STOA_SMOKE=1 时检查本机服务各接口和独立窗口，结果写到数据目录 smoke.json 后退出。"""
    import json
    import urllib.request
    res, ok = {"python": sys.version.split()[0], "platform": sys.platform, "port": port}, True

    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=60) as r:
            return r.status, r.read()

    for path, need in (("/", b"Stoa"), ("/api/settings", b"version"), ("/api/alerts", b"rules"), ("/api/memory", b"usage"),
                       ("/assets/bust.glb", b"glTF"), ("/api/update", b"current")):
        try:
            code, body = get(path)
            res[path] = code == 200 and need in body
        except Exception as e:
            res[path] = f"失败：{e}"
        ok = ok and res[path] is True
    try:
        _, body = get("/api/indices")
        res["/api/indices"] = len(json.loads(body)) > 0  # 行情要联网，只记录不判失败
    except Exception as e:
        res["/api/indices"] = f"失败：{e}"
    # 独立窗口：pywebview + WebView2 打开界面，页面加载完就关
    win = {"done": False}
    try:
        import webview

        def loaded(w):
            try:
                win["title"] = w.evaluate_js("document.title")
            except Exception as e:
                win["title"] = f"evaluate_js 失败：{e}"
            win["done"] = True
            w.destroy()

        w = webview.create_window("Stoa smoke", f"http://127.0.0.1:{port}/", width=1280, height=800)
        w.events.loaded += lambda: loaded(w)
        threading.Timer(90, lambda: (not win["done"]) and w.destroy()).start()
        webview.start(gui="edgechromium" if sys.platform == "win32" else None, private_mode=True)
        res["window"] = win.get("title") or "未加载完成"
        ok = ok and "Stoa" in str(win.get("title"))
    except Exception as e:
        res["window"] = f"失败：{e}"
        ok = False
    res["ok"] = ok
    (home / "smoke.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("smoke", res)
    os._exit(0 if ok else 1)


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

    from .serve import main as serve
    threading.Thread(target=serve, daemon=True).start()
    if not wait_port(port):
        raise RuntimeError(f"本机服务没能在 127.0.0.1:{port} 启动")

    if os.getenv("STOA_SMOKE") == "1":
        smoke(port, home)
    mode = os.getenv("STOA_WINDOW", "auto")  # auto | webview | app | browser | none
    if mode in ("auto", "webview") and window_webview(url, home):
        os._exit(0)
    if mode in ("auto", "app") and window_app(url, home):
        os._exit(0)
    if mode != "none":
        webbrowser.open(url)
    while True:  # 退回浏览器时常驻，靠结束进程退出
        time.sleep(3600)
