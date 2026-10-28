"""常驻模式（Hermes 式网关）：网页聊天 + Telegram + 内置定时早晚报，全部共享一套记忆。
python -m stock_agent serve
"""
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from . import notify
from .agent import Agent
from .memory import Memory, home

CN_TZ = timezone(timedelta(hours=8))
IDLE_END = 30 * 60  # 闲置 30 分钟就结束会话并整理记忆
LOCK = threading.Lock()
MEM = Memory()
SESSIONS: dict[str, dict] = {}  # channel -> {"agent": Agent, "last": ts}
UI = (Path(__file__).parent / "web.html").read_text(encoding="utf-8")


def log(*a):
    print(datetime.now(CN_TZ).strftime("%m-%d %H:%M:%S"), *a, flush=True)


def chat(channel: str, text: str) -> str:
    with LOCK:
        s = SESSIONS.get(channel)
        if text.strip() in ("/new", "/reset") and s:
            s["agent"].end_session()
            SESSIONS.pop(channel, None)
            return "已整理记忆，开始新对话。"
        if text.strip() == "/memory":
            return MEM.snapshot()
        if not s:
            s = SESSIONS[channel] = {"agent": Agent(MEM, channel=channel.split(":")[0])}
        s["last"] = time.time()
        try:
            return s["agent"].ask(text) or "（空回复）"
        except Exception as e:
            log("chat error", e)
            return f"出错了：{e}"


def reaper():
    while True:
        time.sleep(60)
        with LOCK:
            for ch, s in list(SESSIONS.items()):
                if time.time() - s.get("last", 0) > IDLE_END:
                    log("end session", ch)
                    try:
                        s["agent"].end_session()
                    except Exception as e:
                        log("end_session error", e)
                    SESSIONS.pop(ch, None)


# ---------------- 网页 ----------------
class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self) -> bool:
        token = os.getenv("WEB_TOKEN", "")
        got = self.headers.get("X-Token", "")
        return not token or hmac.compare_digest(token, got)

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, UI, "text/html")
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if not self._auth():
            return self._send(401, {"error": "token"})
        if self.path == "/api/memory":
            return self._send(200, {"user": MEM.entries("user"), "memory": MEM.entries("memory"),
                                    "usage": {"user": MEM.usage("user"), "memory": MEM.usage("memory")},
                                    "calls": MEM.call_stats()})
        if self.path == "/api/report":
            p = home() / "reports" / "latest.md"
            return self._send(200, {"markdown": p.read_text(encoding="utf-8") if p.exists() else "还没有报告。"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "token"})
        if self.path != "/api/chat":
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        reply = chat("web:" + str(body.get("session", "default"))[:40], str(body.get("text", ""))[:4000])
        self._send(200, {"reply": reply})


# ---------------- Telegram ----------------
def telegram():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = {x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()}
    if not token:
        return
    if not allowed:
        log("TELEGRAM_CHAT_ID 未设置：为安全起见不接收任何人的消息")
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    log("Telegram 网关已启动")
    while True:
        try:
            r = requests.get(f"{api}/getUpdates", params={"timeout": 50, "offset": offset}, timeout=60).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                cid, text = str((m.get("chat") or {}).get("id", "")), m.get("text")
                if not text or cid not in allowed:
                    continue
                requests.post(f"{api}/sendChatAction", json={"chat_id": cid, "action": "typing"}, timeout=10)
                reply = chat(f"telegram:{cid}", text)
                for i in range(0, len(reply), 3900):
                    requests.post(f"{api}/sendMessage", json={"chat_id": cid, "text": reply[i:i + 3900]}, timeout=30)
        except Exception as e:
            log("telegram error", e)
            time.sleep(5)


# ---------------- 定时早晚报 ----------------
def scheduler():
    times = {s.split("=")[0]: s.split("=")[1] for s in os.getenv("REPORT_TIMES", "morning=08:30,evening=21:00").split(",") if "=" in s}
    done = set()
    log("定时报告：", times, "（北京时间，工作日）")
    while True:
        now = datetime.now(CN_TZ)
        if now.weekday() < 5:
            for session, hhmm in times.items():
                key = (now.date(), session)
                if now.strftime("%H:%M") == hhmm and key not in done:
                    done.add(key)
                    log("生成报告", session)
                    subprocess.run([sys.executable, "-m", "stock_agent", "--session", session, "--push"], check=False)
        time.sleep(20)


def main():
    port = int(os.getenv("PORT", "8080"))
    for fn in (reaper, telegram, scheduler):
        threading.Thread(target=fn, daemon=True).start()
    log(f"股市 Agent 已启动：http://0.0.0.0:{port}  数据目录 {home()}")
    log(f"记忆 USER {MEM.usage('user')} · MEMORY {MEM.usage('memory')} · 代码执行 {'开' if os.getenv('ALLOW_CODE') == '1' else '关'}")
    ThreadingHTTPServer(("0.0.0.0", port), Web).serve_forever()
