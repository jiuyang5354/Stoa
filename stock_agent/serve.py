"""常驻模式（Hermes 式网关）：网页聊天 + Telegram + 内置定时早晚报，全部共享一套记忆。
python -m stock_agent serve
"""
import hmac
import json
import re
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from . import alerts, notify, settings, skills, updater
from .agent import Agent, load_cfg
from .memory import Memory, home

CN_TZ = timezone(timedelta(hours=8))
IDLE_END = 30 * 60  # 闲置 30 分钟就结束会话并整理记忆
LOCK = threading.Lock()
MEM = Memory()
SESSIONS: dict[str, dict] = {}  # channel -> {"agent": Agent, "last": ts}
UI = (Path(__file__).parent / "web.html").read_text(encoding="utf-8")
ASSETS = Path(__file__).parent / "assets"
ASSET_TYPES = {"bust.glb": "model/gltf-binary", "archeologicaps.woff2": "font/woff2",
               **{f: "font/woff2" for f in ("optimus-princeps.woff2", "optimus-princeps-semibold.woff2", "libertine-regular.woff2",
                                            "libertine-bold.woff2", "libertine-italic.woff2", "alte-haas-grotesk.woff2", "alte-haas-grotesk-bold.woff2")},
               "travertine009_color.jpg": "image/jpeg", "travertine009_normal.jpg": "image/jpeg", "travertine009_roughness.jpg": "image/jpeg",
               "three.module.min.js": "text/javascript; charset=utf-8"}  # 本地带一份 three.js，国内网不用连 CDN


def log(*a):
    print(datetime.now(CN_TZ).strftime("%m-%d %H:%M:%S"), *a, flush=True)


def llm_ready() -> bool:
    if os.getenv("STOA_MOCK_LLM") == "1":
        return True
    try:
        from .providers import LLM
        return LLM(cfg=load_cfg().get("llm", {})).ready
    except Exception:
        return True  # 判断不了就交给真正的调用去报错


_MODELS = {"at": 0, "key": None, "list": []}


def models_list() -> dict:
    """对话框的模型下拉：当前模型 + 服务商 /models 返回的列表（OpenAI 兼容，缓存 10 分钟）+ 备用链 + config.yaml llm.models。"""
    from .providers import LLM, fetch_models
    cfg = load_cfg().get("llm", {})
    cur = LLM(cfg=cfg)
    out = [cur.model]
    out += [x for x in (cfg.get("models") or []) if isinstance(x, str)]
    out += [s.partition(":")[2] for s in os.getenv("LLM_FALLBACK", "").split(",") if ":" in s]
    remote = []
    key = (cur.base, cur.key)
    if cur.ready or cur.name in ("ollama", "lmstudio", "vllm"):
        if _MODELS["key"] == key and time.time() - _MODELS["at"] < 600:
            remote = _MODELS["list"]
        else:
            try:
                remote = fetch_models(cur.base, cur.key, "openai" if cur.proto == "azure" else cur.proto)
            except Exception:
                remote = []
            _MODELS.update(at=time.time(), key=key, list=remote)
    seen, models = set(), []
    for m in out + remote:
        if m and m not in seen:
            seen.add(m); models.append(m)
    return {"provider": cur.name, "current": cur.model, "models": models[:200], "ready": cur.ready}


def chat(channel: str, text: str, model: str | None = None, on_event=None) -> str:
    """全局锁只管会话表；每个会话自己一把锁，一个对话在查数据时，别的对话和 Telegram 不用排队。"""
    with LOCK:
        s = SESSIONS.get(channel)
        if text.strip() in ("/new", "/reset") and s:
            with s.setdefault("lock", threading.Lock()):
                s["agent"].end_session()
            SESSIONS.pop(channel, None)
            return "已整理记忆，开始新对话。"
        if text.strip() == "/memory":
            return MEM.snapshot()
        if not s:
            kind, _, sid = channel.partition(":")
            # 网页每个对话一个 id，存成 web-<id>，重启后接着聊能带回上下文
            s = SESSIONS[channel] = {"agent": Agent(MEM, channel=kind, session=f"web-{sid}" if kind == "web" and sid else None)}
        s["last"] = time.time()
        lock = s.setdefault("lock", threading.Lock())
        stop = s["stop"] = threading.Event()  # 每个请求一个停止开关；/api/chat/stop 按下它
    with lock:
        ag = s["agent"]
        if stop.is_set():
            return "已停止。"
        ag._stop = stop
        if model:  # 对话框里选的模型：只换这个对话的主模型，不改全局设置
            try:
                ag.llm.chain[0].model = model
            except Exception:
                pass
        ag.on_event = on_event
        try:
            return ag.ask(text) or "（空回复）"
        except Exception as e:
            log("chat error", e)
            return f"出错了：{e}"
        finally:
            ag.on_event = None
            s["last"] = time.time()


def reaper():
    while True:
        time.sleep(60)
        with LOCK:
            for ch, s in list(SESSIONS.items()):
                if time.time() - s.get("last", 0) > IDLE_END and not (s.get("lock") and s["lock"].locked()):
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


_HEAD = {"ts": 0, "data": None}
_HEAD_LOCK = threading.Lock()


def headlines() -> dict:
    """右侧「要闻」：A股 / 港股 / 美股各几条最新市场新闻（东方财富搜索，按时间），缓存 20 分钟。"""
    with _HEAD_LOCK:
        if _HEAD["data"] is not None and time.time() - _HEAD["ts"] < 1200:
            return _HEAD["data"]
    from concurrent.futures import ThreadPoolExecutor
    from . import research
    # 搜索会匹配正文，所以多取一些、只留标题里真提到这个市场的
    kws = {"A": ("A股", ("A股", "沪指", "上证", "深成指", "创业板", "沪深", "两市", "北向")),
           "HK": ("港股", ("港股", "恒指", "恒生", "南向", "港交所")),
           "US": ("美股", ("美股", "纳指", "纳斯达克", "标普", "道指", "道琼斯", "美联储", "华尔街", "美债"))}

    def one(spec):
        kw, words = spec
        try:
            xs = [{k: it.get(k, "") for k in ("time", "title", "source", "url")} for it in research._em_news(kw, 30)]
        except Exception:
            return []
        hit = [x for x in xs if any(w in x["title"] for w in words)]
        return (hit if len(hit) >= 3 else hit + [x for x in xs if x not in hit])[:8]
    with ThreadPoolExecutor(3) as ex:
        res = dict(zip(kws, ex.map(one, kws.values())))
    out = {"items": res, "updated": datetime.now(CN_TZ).strftime("%H:%M")}
    if any(res.values()):
        with _HEAD_LOCK:
            _HEAD.update(ts=time.time(), data=out)
    return out


def apply_settings(watchlist: bool = False):
    """设置保存后：已开的会话换上新模型，自选股变了就清行情缓存。"""
    from .providers import Router
    with LOCK:
        for sess in SESSIONS.values():
            try:
                sess["agent"].llm = Router(load_cfg().get("llm", {}))
            except Exception as e:
                log("apply settings", e)
    if watchlist:
        with _IDX_LOCK:
            _IDX.update(ts=0, data=None)


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

    def _local_ok(self) -> bool:
        """设置接口能改密钥：没设口令时只允许本机访问。"""
        return bool(os.getenv("WEB_TOKEN")) or self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            return self._send(200, UI, "text/html")
        if self.path.startswith("/assets/"):
            # 静态素材白名单：胸像（Poly Haven CC0）、标题字体（dafont 100% Free）、石灰华贴图（ambientCG CC0）
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
        if self.path == "/api/graph":
            from . import graph
            return self._send(200, graph.build(MEM))
        if self.path.split("?")[0] == "/api/memory/search":
            from urllib.parse import parse_qs, urlparse
            from . import graph, vectors
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0][:200]
            try:
                return self._send(200, {"q": q, "results": graph.semantic(q, MEM), "backend": vectors.backend_status()})
            except Exception as e:
                return self._send(200, {"q": q, "results": [], "error": str(e)[:120]})
        if self.path.split("?")[0] == "/api/events":
            from . import earnings
            d = earnings.load()
            return self._send(200, {"tracked": sorted(d["items"].values(), key=lambda x: ((x.get("next") or {}).get("date") or "9999")),
                                    "reports": d["reports"][-30:][::-1], "updated": d.get("updated")})
        if self.path.startswith("/api/events/report/"):
            from . import earnings
            j = earnings.load_report(self.path.split("?")[0].rsplit("/", 1)[-1])
            return self._send(200 if j else 404, j or {"error": "not found"})
        if self.path == "/api/mcp":
            if not self._local_ok():
                return self._send(403, {"error": "forbidden"})
            from .mcpclient import MCP, path as mcp_path
            return self._send(200, {"servers": MCP.status(), "path": str(mcp_path())})
        if self.path.split("?")[0] == "/api/sessions":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0][:100]
            return self._send(200, {"sessions": MEM.sessions(query=q)})
        if self.path.startswith("/api/sessions/"):
            sid = re.sub(r"[^A-Za-z0-9_-]", "", self.path.split("?")[0].rsplit("/", 1)[-1])[:40]
            d = MEM.digest(f"web-{sid}")
            return self._send(200, {"id": sid, "messages": MEM.history(f"web-{sid}", limit=2000),
                                    "digest": {k: d[k] for k in ("upto_id", "text", "ts", "n_msgs")} if d else None})
        if self.path.split("?")[0] == "/api/update":
            return self._send(200, updater.check(force="force" in self.path))
        if self.path.split("?")[0] == "/api/alerts":
            return self._send(200, alerts.listing())
        if self.path.split("?")[0] == "/api/backtests":
            from . import backtest
            return self._send(200, {"items": backtest.listing()})
        if self.path.startswith("/api/backtests/"):
            from . import backtest
            name = self.path.split("?")[0].rsplit("/", 1)[-1]
            if name.endswith(".png"):
                f = backtest.bt_dir() / re.sub(r"[^A-Za-z0-9_.-]", "", name)
                data = f.read_bytes() if f.exists() else b""
                self.send_response(200 if data else 404)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            j = backtest.load(name)
            return self._send(200 if j else 404, j or {"error": "not found"})
        if self.path.startswith("/api/cards/"):
            from . import cards
            j = cards.load(self.path.split("?")[0].rsplit("/", 1)[-1])
            return self._send(200 if j else 404, j or {"error": "not found"})
        if self.path.split("?")[0] == "/api/filings":
            from . import filings
            return self._send(200, {"items": filings.cached_list()})
        if self.path.split("?")[0] == "/api/debates":
            from . import debate
            return self._send(200, {"items": debate.listing()})
        if self.path.startswith("/api/debates/"):
            from . import debate
            j = debate.load(self.path.split("?")[0].rsplit("/", 1)[-1])
            return self._send(200 if j else 404, j or {"error": "not found"})
        if self.path.split("?")[0] == "/api/evals":
            from . import evals
            hist = evals.history()
            latest = None
            if hist:
                f = evals.evals_dir() / f"{hist[-1]['id']}.json"
                if f.exists():
                    j = json.loads(f.read_text(encoding="utf-8"))
                    latest = {k: j.get(k) for k in ("id", "created", "version", "llm", "summary", "previous")}
                    latest["failed"] = [{"id": r["id"], "category": r["category"], "checks": [c for c in r["checks"] if not c["pass"]]}
                                        for r in j.get("results", []) if r["score"] < 1]
            return self._send(200, {"history": hist, "latest": latest})
        if self.path == "/api/memory":
            return self._send(200, {"user": MEM.entries("user"), "memory": MEM.entries("memory"),
                                    "usage": {"user": MEM.usage("user"), "memory": MEM.usage("memory")},
                                    "calls": MEM.call_stats(), "ledger": MEM.recent_calls()})
        if self.path == "/api/models":
            try:
                return self._send(200, models_list())
            except Exception as e:
                return self._send(200, {"models": [], "current": "", "error": str(e)[:120]})
        if self.path == "/api/headlines":
            try:
                return self._send(200, headlines())
            except Exception as e:
                return self._send(200, {"items": {}, "error": str(e)[:120]})
        if self.path == "/api/indices":
            try:
                return self._send(200, {"items": indices(), "updated": datetime.now(CN_TZ).strftime("%H:%M")})
            except Exception as e:
                return self._send(200, {"items": [], "error": str(e)})
        if self.path == "/api/settings":
            if not self._local_ok():
                return self._send(403, {"error": "设置页只能在本机打开，或先设 WEB_TOKEN"})
            return self._send(200, settings.get(MEM))
        if self.path.startswith("/api/export"):
            if not self._local_ok():
                return self._send(403, {"error": "forbidden"})
            data = settings.export_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="stoa-backup-{datetime.now(CN_TZ):%Y%m%d}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.path == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": "stoa", "object": "model", "owned_by": "stoa"}]})
        if self.path == "/api/report":
            p = home() / "reports" / "latest.md"
            return self._send(200, {"markdown": p.read_text(encoding="utf-8") if p.exists() else "还没有报告。"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "token"})
        path = self.path.split("?")[0]
        if path == "/api/chat/stop":
            body = self._body()
            sid = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("session", "default")))[:40] or "default"
            s = SESSIONS.get("web:" + sid) or {}
            ev = s.get("stop")
            if ev is not None:
                ev.set()
            return self._send(200, {"ok": True, "running": bool(s.get("lock") and s["lock"].locked())})
        if path == "/api/chat":
            body = self._body()
            if not llm_ready():  # 没配模型：不报错、不弹窗，前端直接打开「设置 → 模型」
                return self._send(200, {"reply": "还没配置大模型。已打开「设置 → 模型」：选服务商、填密钥，保存后立即生效，不用重启。行情、早晚报页面不受影响。", "need_model": True})
            sid = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("session", "default")))[:40] or "default"
            model = re.sub(r"[^A-Za-z0-9._:/@+-]", "", str(body.get("model") or ""))[:120] or None
            def payload(reply):
                meta = getattr((SESSIONS.get("web:" + sid) or {}).get("agent"), "last_meta", None) or {}
                return {"reply": reply, "sources": meta.get("sources") or [], "verify": meta.get("verify"), "debate": meta.get("debate"), "cards": meta.get("cards"),
                        "compressed": meta.get("compressed"), "archived": meta.get("archived") or 0, "steps": meta.get("steps") or [],
                        "stopped": bool(meta.get("stopped"))}
            text = str(body.get("text", ""))[:4000]
            if not body.get("stream"):
                return self._send(200, payload(chat("web:" + sid, text, model)))
            # 流式：边查边把每一步（查行情、读公告…）推给网页，最后一条是完整回答
            import queue
            q, box = queue.Queue(), {}
            th = threading.Thread(target=lambda: (box.__setitem__("r", chat("web:" + sid, text, model, on_event=q.put)), q.put(None)), daemon=True)
            th.start()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            alive = True
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    ev = {"t": "ping"}
                if ev is None:
                    break
                if alive:
                    try:
                        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        alive = False  # 页面关了：回答照样算完、存进记录，下次打开 History 能看到
            if alive:
                try:
                    self.wfile.write(f"data: {json.dumps({'t': 'done', **payload(box.get('r') or '（空回复）')}, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            self.close_connection = True
            return
        if path.startswith("/api/settings") or path in ("/api/memory/delete", "/api/memory/add", "/api/skills/delete", "/api/skills/view", "/api/alerts/add", "/api/alerts/delete", "/api/alerts/check", "/api/events/check", "/api/update/apply", "/api/sessions/delete", "/api/sessions/rename") or path.startswith("/api/mcp/"):
            if not self._local_ok():
                return self._send(403, {"error": "设置页只能在本机打开，或先设 WEB_TOKEN"})
            body = self._body()
            if path == "/api/settings":
                out = settings.save(body)
                apply_settings(watchlist="watchlist" in body)
                return self._send(200, out)
            if path == "/api/settings/models":  # 设置页「模型」自动检测：用表单里还没保存的服务商/地址/密钥去问
                return self._send(200, settings.probe_models(body))
            if path == "/api/settings/sources":
                from .providers import save_source, delete_source
                if body.get("action") == "delete":
                    return self._send(200, delete_source(str(body.get("id", ""))))
                out = save_source(body.get("source") or {}, body.get("old"))
                _MODELS["at"] = 0
                return self._send(200, out)
            if path == "/api/settings/test":
                return self._send(200, settings.test_push() if body.get("what") == "push" else settings.test_llm())
            if path.startswith("/api/mcp/"):
                from . import mcpclient as mc
                act, name = path.rsplit("/", 1)[-1], str(body.get("name", ""))
                if act == "save":
                    return self._send(200, mc.upsert(name, body.get("config") or {}, body.get("old_name")))
                if act == "delete":
                    return self._send(200, mc.remove(name))
                if act == "toggle":
                    return self._send(200, mc.set_enabled(name, bool(body.get("enabled")), body.get("tool")))
                if act == "import":
                    return self._send(200, mc.import_json(str(body.get("json", ""))))
                if act == "test":
                    return self._send(200, mc.MCP.test(name))
                return self._send(404, {"error": "not found"})
            if path in ("/api/sessions/delete", "/api/sessions/rename"):
                sid = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("id", "")))[:40]
                if not sid:
                    return self._send(400, {"error": "id"})
                with LOCK:
                    SESSIONS.pop(f"web:{sid}", None)  # 删掉/改名后下次重新从库里载入
                if path.endswith("delete"):
                    return self._send(200, {"msg": f"已删除对话（{MEM.delete_session('web-' + sid)} 条消息）"})
                MEM.rename_session("web-" + sid, body.get("title"), body.get("pinned"))
                return self._send(200, {"msg": "已更新"})
            if path == "/api/update/apply":
                try:
                    out = updater.apply()
                except Exception as e:
                    out = {"error": f"更新失败：{str(e)[:100]}"}
                self._send(200, out)
                if out.get("restart"):
                    threading.Timer(1.5, lambda: os._exit(0)).start()
                return
            if path == "/api/alerts/add":
                return self._send(200, alerts.add(str(body.get("symbol", "")), str(body.get("kind", "")), body.get("value"),
                                                  str(body.get("name", "")), bool(body.get("repeat")), str(body.get("note", ""))))
            if path == "/api/alerts/delete":
                return self._send(200, {"msg": alerts.remove(str(body.get("id", "")))})
            if path == "/api/alerts/check":
                return self._send(200, {"events": alerts.check()})
            if path == "/api/events/check":
                from . import earnings
                earnings.refresh(MEM, force=True)
                return self._send(200, {"events": earnings.tick(log, mem=MEM)})
            if path == "/api/skills/delete":
                return self._send(200, {"msg": skills.delete(str(body.get("name", "")))})
            if path == "/api/skills/view":
                return self._send(200, skills.view(str(body.get("name", "")), count=False))
            if path.startswith("/api/memory/"):
                t = body.get("target")
                if t not in ("user", "memory"):
                    return self._send(400, {"error": "target"})
                text = str(body.get("text", ""))[:600]
                return self._send(200, {"msg": MEM.add(t, text) if path.endswith("add") else MEM.remove(t, text)})
        if path == "/api/backtest":
            if not self._local_ok():
                return self._send(403, {"error": "回测只能在本机发起，或先设 WEB_TOKEN"})
            from . import backtest
            body = self._body()
            running = [x for x in backtest.listing(10) if x.get("status") == "running"]
            if running:
                return self._send(409, {"error": "已有一个回测在跑", "id": running[0]["id"]})
            return self._send(200, {"id": backtest.run_async(body), "status": "running"})
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
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "stoa",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._send(200, {"id": cid, "object": "chat.completion", "created": created, "model": "stoa",
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
def report_times() -> dict:
    """REPORT_TIMES=morning=08:30,evening=21:00；设成 off 就关掉。每轮重读，设置页改了立即生效。"""
    return {s.split("=")[0].strip(): s.split("=")[1].strip()
            for s in os.getenv("REPORT_TIMES", "morning=08:30,evening=21:00").split(",") if "=" in s}


def scheduler():
    done = set()
    log("定时报告：", report_times(), "（北京时间，工作日）")
    while True:
        now, times = datetime.now(CN_TZ), report_times()
        if now.weekday() < 5:
            for session, hhmm in times.items():
                key = (now.date(), session)
                if now.strftime("%H:%M") == hhmm and key not in done:
                    done.add(key)
                    log("生成报告", session)
                    subprocess.run([sys.executable, "-m", "stock_agent", "--session", session, "--push"], check=False)
        time.sleep(20)


def main():
    settings.load_env_file()
    port, host = int(os.getenv("PORT", "8080")), os.getenv("HOST", "0.0.0.0")
    from . import vectors
    from . import newsdaily
    for fn in (reaper, telegram, scheduler, lambda: alerts.loop(log), lambda: vectors.indexer_loop(MEM, log), lambda: newsdaily.loop(log)):
        threading.Thread(target=fn, daemon=True).start()
    log(f"Stoa 已启动：http://{host}:{port}  数据目录 {home()}")
    from .data import provider_order
    from .providers import Router
    log(f"大模型 {Router(load_cfg().get('llm', {}))}  行情源 {' → '.join(provider_order())}")
    log(f"对外接口：OpenAI 兼容 /v1/chat/completions · MCP /mcp · REST /api/*")
    log(f"新闻快照：{','.join(newsdaily.snapshot_times()) or '关'}（北京时间，每天；存 data/news/）")
    log(f"记忆 USER {MEM.usage('user')} · MEMORY {MEM.usage('memory')} · 代码执行 {'开' if os.getenv('ALLOW_CODE') == '1' else '关'}")
    ThreadingHTTPServer((host, port), Web).serve_forever()
