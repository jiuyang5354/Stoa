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
from .agent import Agent, load_cfg
from .memory import Memory, home

CN_TZ = timezone(timedelta(hours=8))
IDLE_END = 30 * 60  # 闲置 30 分钟就结束会话并整理记忆
LOCK = threading.Lock()
MEM = Memory()
SESSIONS: dict[str, dict] = {}  # channel -> {"agent": Agent, "last": ts}
UI = (Path(__file__).parent / "web.html").read_text(encoding="utf-8")
ASSETS = Path(__file__).parent / "assets"
ASSET_TYPES = {"bust.glb": "model/gltf-binary", "archeologicaps.woff2": "font/woff2"}


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


# ---------------- 行情（给网页用，缓存 10 分钟） ----------------
_IDX = {"ts": 0, "data": None}
_IDX_LOCK = threading.Lock()


def indices() -> list[dict]:
    with _IDX_LOCK:
        if _IDX["data"] is not None and time.time() - _IDX["ts"] < 600:
            return _IDX["data"]
        from .data import fetch_closes
        cfg = load_cfg()
        items = [(mkt, x) for mkt, xs in cfg.get("indices", {}).items() for x in xs]
        items += [("自选", x) for x in cfg.get("watchlist") or []]
        closes = fetch_closes([x["symbol"] for _, x in items], period="1y")
        out = []
        for mkt, x in items:
            ser = closes.get(x["symbol"])
            if ser is None or len(ser) < 2:
                continue
            last, prev = float(ser.iloc[-1]), float(ser.iloc[-2])
            out.append({"market": mkt, "symbol": x["symbol"], "name": x.get("name", x["symbol"]),
                        "last": round(last, 2), "chg": round(last - prev, 2), "pct": round((last / prev - 1) * 100, 2),
                        "closes": [round(float(v), 2) for v in ser.tolist()],
                        "dates": [d.strftime("%Y-%m-%d") for d in ser.index]})
        _IDX.update(ts=time.time(), data=out)
        return out


# ---------------- 网页 ----------------
class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self) -> bool:
        token = os.getenv("WEB_TOKEN", "")
        got = self.headers.get("X-Token", "")
        auth = self.headers.get("Authorization", "")
        if not got and auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        return not token or hmac.compare_digest(token, got)

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _cors(self):
        if os.getenv("CORS_ORIGIN"):
            self.send_header("Access-Control-Allow-Origin", os.getenv("CORS_ORIGIN"))
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Token, Mcp-Session-Id")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            return self._send(200, UI, "text/html")
        if self.path.startswith("/assets/"):
            # 静态素材白名单：胸像（Poly Haven CC0）、标题字体（dafont 100% Free）
            name = self.path[8:].split("?")[0]
            f = ASSETS / name
            ok = name in ASSET_TYPES and f.exists()
            data = f.read_bytes() if ok else b""
            self.send_response(200 if data else 404)
            self.send_header("Content-Type", ASSET_TYPES.get(name, "text/plain"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if not self._auth():
            return self._send(401, {"error": "token"})
        if self.path == "/api/memory":
            return self._send(200, {"user": MEM.entries("user"), "memory": MEM.entries("memory"),
                                    "usage": {"user": MEM.usage("user"), "memory": MEM.usage("memory")},
                                    "calls": MEM.call_stats()})
        if self.path == "/api/indices":
            try:
                return self._send(200, {"items": indices(), "updated": datetime.now(CN_TZ).strftime("%H:%M")})
            except Exception as e:
                return self._send(200, {"items": [], "error": str(e)})
        if self.path == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": "stock-agent", "object": "model", "owned_by": "stock-agent"}]})
        if self.path == "/api/report":
            p = home() / "reports" / "latest.md"
            return self._send(200, {"markdown": p.read_text(encoding="utf-8") if p.exists() else "还没有报告。"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "token"})
        path = self.path.split("?")[0]
        if path == "/api/chat":
            body = self._body()
            reply = chat("web:" + str(body.get("session", "default"))[:40], str(body.get("text", ""))[:4000])
            return self._send(200, {"reply": reply})
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self._openai(self._body())
        if path == "/mcp":
            from .mcp import handle_payload
            try:
                resp = handle_payload(self._body())
            except json.JSONDecodeError:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            if resp is None:
                self.send_response(202)
                self.end_headers()
                return
            return self._send(200, resp)
        self._send(404, {"error": "not found"})

    def _openai(self, body):
        """OpenAI 兼容接口：任何支持自定义 OpenAI 地址的客户端（Cherry Studio、LobeChat、NextChat、Open WebUI…）都能把它当模型用。
        Agent 自己记着上下文，所以只取最后一条用户消息；会话按 body.user 或客户端区分。"""
        msgs = body.get("messages") or []
        text = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), "") or ""
        if isinstance(text, list):  # 多模态格式里只取文字
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        who = str(body.get("user") or self.headers.get("X-Session") or self.client_address[0])[:40]
        if len([m for m in msgs if m.get("role") == "user"]) == 1 and (who_s := SESSIONS.get("api:" + who)):
            with LOCK:  # 客户端开了新对话：先整理旧会话
                try:
                    who_s["agent"].end_session()
                except Exception:
                    pass
                SESSIONS.pop("api:" + who, None)
        reply = chat("api:" + who, str(text)[:8000])
        cid, created = "chatcmpl-" + os.urandom(8).hex(), int(time.time())
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            for delta, fin in (({"role": "assistant", "content": reply}, None), ({}, "stop")):
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "stock-agent",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._send(200, {"id": cid, "object": "chat.completion", "created": created, "model": "stock-agent",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})


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
    from .data import provider_order
    from .providers import Router
    log(f"大模型 {Router(load_cfg().get('llm', {}))}  行情源 {' → '.join(provider_order())}")
    log(f"对外接口：OpenAI 兼容 /v1/chat/completions · MCP /mcp · REST /api/*")
    log(f"记忆 USER {MEM.usage('user')} · MEMORY {MEM.usage('memory')} · 代码执行 {'开' if os.getenv('ALLOW_CODE') == '1' else '关'}")
    ThreadingHTTPServer(("0.0.0.0", port), Web).serve_forever()
