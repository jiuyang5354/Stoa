"""MCP 客户端：让 Stoa 用别人的 MCP 服务器上的工具。

配置文件 = 数据目录/mcp.json，格式和 Claude Desktop / Cursor 一样：

    {"mcpServers": {
        "fetch":  {"command": "uvx", "args": ["mcp-server-fetch"]},
        "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${MY_KEY}"}},
        "old":    {"url": "https://example.com/sse", "disabled": true}
    }}

- command：本地进程，stdio 传输（一行一个 JSON-RPC 消息）
- url：远程服务器，Streamable HTTP；URL 以 /sse 结尾或 "transport": "sse" 时走旧版 SSE 传输
- env / headers 里的 ${VAR} 用环境变量（含数据目录 .env）替换
- 工具在 Agent 里叫 mcp__服务器__工具名，描述前加 [MCP·服务器]
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from .memory import home

PROTOCOL = "2025-06-18"
CLIENT = {"name": "stoa", "version": "0.11.0"}
TIMEOUT = float(os.getenv("MCP_TIMEOUT", "60"))
MAX_RESULT = 12000  # 工具结果最多回给模型这么多字


def path() -> Path:
    return home() / "mcp.json"


def load_config() -> dict:
    p = path()
    if not p.exists():
        return {}
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("mcpServers", {}) or {}
    except Exception:
        return {}


def save_config(servers: dict):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _expand(v):
    if isinstance(v, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.getenv(m.group(1), ""), v)
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:n] or "x"


class MCPError(Exception):
    pass


# ---------------- 传输 ----------------
class _Base:
    def __init__(self, name: str, cfg: dict):
        self.name, self.cfg = name, cfg
        self.lock = threading.Lock()
        self._id = 0
        self.server_info = {}

    def next_id(self):
        self._id += 1
        return self._id

    def handshake(self):
        r = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT}, timeout=min(TIMEOUT, 45))
        self.server_info = r.get("serverInfo", {}) if isinstance(r, dict) else {}
        self.notify("notifications/initialized")

    def list_tools(self) -> list[dict]:
        out, cursor = [], None
        for _ in range(20):
            r = self.request("tools/list", {"cursor": cursor} if cursor else {})
            out += r.get("tools", [])
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return out


class Stdio(_Base):
    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        cmd = cfg["command"]
        exe = shutil.which(cmd) or cmd
        argv = [exe] + [str(a) for a in cfg.get("args", [])]
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c"] + argv  # npx.cmd、uvx.cmd 之类要经 cmd 才能启动
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (cfg.get("env") or {}).items()})
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW：桌面版不弹黑框
        try:
            self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=env, cwd=cfg.get("cwd") or None, creationflags=flags, bufsize=0)
        except FileNotFoundError:
            raise MCPError(f"找不到命令 {cmd}（需要先安装，比如 npx 要装 Node.js，uvx 要装 uv）")
        self.pending: dict[int, queue.Queue] = {}
        self.err = ""
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for line in iter(self.p.stdout.readline, b""):
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                q = self.pending.pop(msg["id"], None)
                if q:
                    q.put(msg)
            elif isinstance(msg, dict) and "id" in msg and "method" in msg:  # 服务器反过来问（ping 等）
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        for q in list(self.pending.values()):
            q.put({"error": {"message": "进程已退出 " + self.err[-300:]}})
        self.pending.clear()

    def _read_err(self):
        for line in iter(self.p.stderr.readline, b""):
            self.err = (self.err + line.decode("utf-8", "replace"))[-2000:]

    def _write(self, msg):
        try:
            self.p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.p.stdin.flush()
        except Exception as e:
            raise MCPError(f"进程已退出：{self.err[-300:] or e}")

    def alive(self):
        return self.p.poll() is None

    def notify(self, method, params=None):
        self._write({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params=None, timeout=None):
        with self.lock:
            i = self.next_id()
            q = self.pending[i] = queue.Queue()
            self._write({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})
        try:
            msg = q.get(timeout=timeout or TIMEOUT)
        except queue.Empty:
            self.pending.pop(i, None)
            raise MCPError(f"{method} 超时（{int(timeout or TIMEOUT)} 秒）" + (f"：{self.err[-200:]}" if self.err else ""))
        if "error" in msg:
            raise MCPError(str(msg["error"].get("message", msg["error"]))[:400])
        return msg.get("result", {})

    def close(self):
        try:
            self.p.terminate()
            self.p.wait(3)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


def _sse_events(resp):
    """逐个产出 (event, data)。"""
    ev, data = "message", []
    resp.raw.decode_content = True
    while True:  # 逐行读：iter_lines 会攒满 512 字节才吐，长连接里的小事件会被卡住
        b = resp.raw.readline()
        if not b:
            break
        raw = b.decode("utf-8", "replace").rstrip("\r\n")
        if raw == "":
            if data:
                yield ev, "\n".join(data)
            ev, data = "message", []
        elif raw.startswith(":"):
            continue
        elif raw.startswith("event:"):
            ev = raw[6:].strip()
        elif raw.startswith("data:"):
            data.append(raw[5:].lstrip())
    if data:
        yield ev, "\n".join(data)


class Http(_Base):
    """Streamable HTTP（2025-03-26 起的标准远程传输）。"""

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.url = cfg["url"]
        self.headers = {k: str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.sid = None
        self.s = requests.Session()

    def alive(self):
        return True

    def _post(self, msg, timeout):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        r = self.s.post(self.url, json=msg, headers=h, timeout=timeout, stream=True)
        if r.headers.get("Mcp-Session-Id"):
            self.sid = r.headers["Mcp-Session-Id"]
        if r.status_code in (401, 403):
            raise MCPError(f"HTTP {r.status_code}：没有权限，检查 headers 里的密钥")
        if r.status_code == 404 and self.sid:
            self.sid = None
            raise MCPError("会话已过期，请重试")
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}：{r.text[:200]}")
        return r

    def notify(self, method, params=None):
        try:
            self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 15).close()
        except MCPError:
            pass

    def request(self, method, params=None, timeout=None):
        with self.lock:
            i = self.next_id()
            r = self._post({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}, timeout or TIMEOUT)
        ct = r.headers.get("Content-Type", "")
        msg = None
        if "text/event-stream" in ct:
            for _, data in _sse_events(r):
                try:
                    m = json.loads(data)
                except Exception:
                    continue
                if isinstance(m, dict) and m.get("id") == i:
                    msg = m
                    break
            r.close()
        else:
            try:
                m = r.json()
            except Exception:
                raise MCPError(f"返回的不是 JSON：{r.text[:200]}")
            msg = next((x for x in m if x.get("id") == i), None) if isinstance(m, list) else m
        if not msg:
            raise MCPError(f"{method} 没有收到回复")
        if "error" in msg:
            raise MCPError(str(msg["error"].get("message", msg["error"]))[:400])
        return msg.get("result", {})

    def close(self):
        if self.sid:
            try:
                self.s.delete(self.url, headers={"Mcp-Session-Id": self.sid, **self.headers}, timeout=5)
            except Exception:
                pass
        self.s.close()


class SSE(_Base):
    """旧版 HTTP+SSE 传输（2024-11-05）：GET 长连接收消息，POST 到 endpoint 发消息。"""

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.url = cfg["url"]
        self.headers = {k: str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.pending: dict[int, queue.Queue] = {}
        self.endpoint = queue.Queue()
        self.post_url = None
        self.dead = None
        self.s = requests.Session()
        threading.Thread(target=self._listen, daemon=True).start()
        try:
            self.post_url = self.endpoint.get(timeout=20)
        except queue.Empty:
            raise MCPError(self.dead or "SSE 连接 20 秒没有给出 endpoint")
        if isinstance(self.post_url, Exception):
            raise MCPError(str(self.post_url))

    def _listen(self):
        from urllib.parse import urljoin
        try:
            with self.s.get(self.url, headers={"Accept": "text/event-stream", **self.headers}, stream=True, timeout=(15, None)) as r:
                self.resp = r
                if r.status_code >= 400:
                    raise MCPError(f"HTTP {r.status_code}")
                for ev, data in _sse_events(r):
                    if ev == "endpoint":
                        self.endpoint.put(urljoin(self.url, data.strip()))
                        continue
                    try:
                        m = json.loads(data)
                    except Exception:
                        continue
                    if isinstance(m, dict) and "id" in m:
                        q = self.pending.pop(m["id"], None)
                        if q:
                            q.put(m)
        except requests.ConnectionError:
            self.dead = f"连不上 {self.url}（服务器没开，或地址写错）"
            self.endpoint.put(MCPError(self.dead))
        except Exception as e:
            self.dead = f"SSE 连接断开：{e}"
            self.endpoint.put(MCPError(self.dead))
        for q in list(self.pending.values()):
            q.put({"error": {"message": self.dead or "SSE 连接已关闭"}})

    def alive(self):
        return not self.dead

    def _post(self, msg):
        r = self.s.post(self.post_url, json=msg, headers={"Content-Type": "application/json", **self.headers}, timeout=20)
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}：{r.text[:200]}")

    def notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params=None, timeout=None):
        with self.lock:
            i = self.next_id()
            q = self.pending[i] = queue.Queue()
            self._post({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})
        try:
            msg = q.get(timeout=timeout or TIMEOUT)
        except queue.Empty:
            self.pending.pop(i, None)
            raise MCPError(f"{method} 超时")
        if "error" in msg:
            raise MCPError(str(msg["error"].get("message", msg["error"]))[:400])
        return msg.get("result", {})

    def close(self):
        self.dead = "closed"
        try:  # 直接关 socket 让监听线程的 readline 返回；关文件对象会和读线程抢锁卡死
            import socket
            self.resp.raw._fp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass


def connect(name: str, cfg: dict) -> _Base:
    cfg = _expand(cfg)
    if cfg.get("command"):
        c = Stdio(name, cfg)
    elif cfg.get("url"):
        t = (cfg.get("transport") or cfg.get("type") or "").lower()
        c = SSE(name, cfg) if t == "sse" or (t not in ("http", "streamable-http", "streamable_http") and cfg["url"].rstrip("/").endswith("/sse")) else Http(name, cfg)
    else:
        raise MCPError("配置里要有 command（本地命令）或 url（远程地址）")
    try:
        c.handshake()
    except Exception:
        c.close()
        raise
    return c


def flatten(result: dict) -> str | dict:
    """把 tools/call 的结果整理成模型好读的文字。"""
    if not isinstance(result, dict):
        return str(result)[:MAX_RESULT]
    parts = []
    for c in result.get("content", []) or []:
        t = c.get("type")
        if t == "text":
            parts.append(c.get("text", ""))
        elif t == "image":
            parts.append(f"[图片 {c.get('mimeType', '')}]")
        elif t == "resource":
            r = c.get("resource", {})
            parts.append(r.get("text") or f"[资源 {r.get('uri', '')}]")
        elif t == "resource_link":
            parts.append(f"[链接 {c.get('name', '')} {c.get('uri', '')}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(parts).strip()
    if len(text) > MAX_RESULT:
        text = text[:MAX_RESULT] + f"\n…（截断，共 {len(text)} 字）"
    return {"error": text or "工具报错"} if result.get("isError") else (text or "（工具没有返回内容）")


# ---------------- 管理 ----------------
class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.clients: dict[str, _Base] = {}
        self.tools_cache: dict[str, list[dict]] = {}
        self.errors: dict[str, str] = {}
        self.err_at: dict[str, float] = {}
        self.cfg_sig = None

    def _sync(self):
        """配置文件变了就关掉被删/被改/被停用的服务器。"""
        cfg = load_config()
        sig = json.dumps(cfg, sort_keys=True)
        if sig == self.cfg_sig:
            return cfg
        old = json.loads(self.cfg_sig) if self.cfg_sig else {}
        self.cfg_sig = sig
        for name in list(self.clients):
            if name not in cfg or cfg[name] != old.get(name) or cfg[name].get("disabled"):
                self._drop(name)
        for name in list(self.errors):
            if name not in cfg or cfg[name] != old.get(name):
                self.errors.pop(name, None)
        return cfg

    def _drop(self, name):
        c = self.clients.pop(name, None)
        self.tools_cache.pop(name, None)
        if c:
            try:
                c.close()
            except Exception:
                pass

    def _client(self, name, cfg, retry_failed=False):
        c = self.clients.get(name)
        if c and c.alive():
            return c
        if c:
            self._drop(name)
        if name in self.errors and not retry_failed and time.time() - self.err_at.get(name, 0) < 300:
            raise MCPError(self.errors[name])  # 失败的服务器 5 分钟内不重试，免得每轮对话都卡住
        try:
            c = connect(name, cfg)
            tools = c.list_tools()
        except requests.ConnectionError:
            self.errors[name], self.err_at[name] = f"连不上 {cfg.get('url')}（服务器没开，或地址写错）", time.time()
            raise MCPError(self.errors[name])
        except requests.Timeout:
            self.errors[name], self.err_at[name] = f"连接 {cfg.get('url')} 超时", time.time()
            raise MCPError(self.errors[name])
        except Exception as e:
            self.errors[name], self.err_at[name] = str(e)[:400], time.time()
            raise MCPError(self.errors[name])
        self.errors.pop(name, None)
        self.clients[name] = c
        self.tools_cache[name] = tools
        return c

    def tools(self) -> list[dict]:
        """所有启用服务器的工具，转成 Agent 的工具格式。失败的服务器跳过（设置页里能看到原因）。"""
        with self.lock:
            cfg = self._sync()
            names = [n for n, c in cfg.items() if not c.get("disabled")]
        out = []
        for name in names:
            with self.lock:
                try:
                    self._client(name, cfg[name])
                except Exception:
                    continue
                for t in self.tools_cache.get(name, []):
                    if t.get("name") in (cfg[name].get("disabledTools") or []):
                        continue
                    schema = {k: v for k, v in (t.get("inputSchema") or {}).items() if k not in ("$schema", "$id")}
                    schema.setdefault("type", "object")
                    schema.setdefault("properties", {})
                    out.append({"name": f"mcp__{slug(name, 20)}__{slug(t['name'])}"[:64],
                                "description": f"[MCP·{name}] " + (t.get("description") or t.get("title") or t["name"])[:900],
                                "parameters": schema})
        return out

    def _resolve(self, fullname):
        cfg = load_config()
        for name, c in cfg.items():
            pre = f"mcp__{slug(name, 20)}__"
            if fullname.startswith(pre) and not c.get("disabled"):
                for t in self.tools_cache.get(name, []):
                    if f"{pre}{slug(t['name'])}"[:64] == fullname:
                        return name, c, t["name"]
        return None, None, None

    def call(self, fullname: str, args: dict):
        with self.lock:
            name, cfg, tool = self._resolve(fullname)
            if not name:
                self.tools()
                name, cfg, tool = self._resolve(fullname)
            if not name:
                return {"error": f"没有这个 MCP 工具：{fullname}"}
        for attempt in range(2):
            try:
                with self.lock:
                    c = self._client(name, cfg, retry_failed=True)
                return flatten(c.request("tools/call", {"name": tool, "arguments": args or {}}))
            except MCPError as e:
                with self.lock:
                    self._drop(name)
                if attempt or "超时" in str(e):
                    return {"error": f"MCP·{name} 调用失败：{e}"}

    def status(self) -> list[dict]:
        with self.lock:
            cfg = self._sync()
            out = []
            for name, c in cfg.items():
                c2 = self.clients.get(name)
                out.append({"name": name, "config": c, "enabled": not c.get("disabled"),
                            "kind": "stdio" if c.get("command") else ("sse" if (c.get("transport") == "sse" or str(c.get("url", "")).rstrip("/").endswith("/sse")) else "http"),
                            "connected": bool(c2 and c2.alive()), "error": self.errors.get(name, ""),
                            "server": (c2.server_info if c2 else {}),
                            "tools": [{"name": t["name"], "description": (t.get("description") or "")[:160],
                                       "enabled": t["name"] not in (c.get("disabledTools") or [])} for t in self.tools_cache.get(name, [])]})
            return out

    def test(self, name: str) -> dict:
        with self.lock:
            cfg = self._sync()
            if name not in cfg:
                return {"ok": False, "error": "没有这个服务器"}
            self._drop(name)
            self.errors.pop(name, None)
            t0 = time.time()
            try:
                self._client(name, cfg[name], retry_failed=True)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            n = len(self.tools_cache.get(name, []))
            return {"ok": True, "msg": f"已连接 {name}，{n} 个工具，用时 {time.time() - t0:.1f} 秒"}

    def close_all(self):
        with self.lock:
            for n in list(self.clients):
                self._drop(n)


MCP = Manager()
import atexit  # noqa: E402
atexit.register(MCP.close_all)  # 退出时关掉启动的子进程


# ---------------- 设置页用的增删改 ----------------
def upsert(name: str, config: dict, old_name: str | None = None) -> dict:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,40}", name):
        return {"error": "名字只能用字母、数字、_ - .，最多 40 个字"}
    if not isinstance(config, dict) or not (config.get("command") or config.get("url")):
        return {"error": "要填 command（本地命令）或 url（远程地址）"}
    if config.get("url") and not re.match(r"https?://", str(config["url"])):
        return {"error": "url 要以 http:// 或 https:// 开头"}
    servers = load_config()
    if old_name and old_name != name:
        servers.pop(old_name, None)
    servers[name] = {k: v for k, v in config.items() if v not in (None, "", [], {})}
    save_config(servers)
    return {"msg": f"已保存 {name}"}


def remove(name: str) -> dict:
    servers = load_config()
    if servers.pop(name, None) is None:
        return {"error": "没有这个服务器"}
    save_config(servers)
    return {"msg": f"已删除 {name}"}


def set_enabled(name: str, enabled: bool, tool: str | None = None) -> dict:
    servers = load_config()
    if name not in servers:
        return {"error": "没有这个服务器"}
    s = servers[name]
    if tool:
        off = set(s.get("disabledTools") or [])
        off.discard(tool) if enabled else off.add(tool)
        if off:
            s["disabledTools"] = sorted(off)
        else:
            s.pop("disabledTools", None)
    elif enabled:
        s.pop("disabled", None)
    else:
        s["disabled"] = True
    save_config(servers)
    return {"msg": "已更新"}


def import_json(text: str) -> dict:
    """粘贴 Claude Desktop / Cursor / Cline 的配置（整段或只有 mcpServers 里的一项）。"""
    try:
        j = json.loads(text)
    except Exception as e:
        return {"error": f"不是合法 JSON：{e}"}
    if isinstance(j, dict) and "mcpServers" in j:
        j = j["mcpServers"]
    if isinstance(j, dict) and ("command" in j or "url" in j):
        return {"error": "只有配置没有名字，请写成 {\"名字\": {...}}"}
    if not isinstance(j, dict) or not j:
        return {"error": "没有找到服务器配置"}
    servers, added = load_config(), []
    for name, c in j.items():
        if isinstance(c, dict) and (c.get("command") or c.get("url") or c.get("serverUrl")):
            if c.get("serverUrl") and not c.get("url"):  # Windsurf 的写法
                c = {**c, "url": c.pop("serverUrl")}
            servers[slug(name)] = c
            added.append(slug(name))
    if not added:
        return {"error": "没有可用的服务器（每项要有 command 或 url）"}
    save_config(servers)
    return {"msg": f"已导入 {len(added)} 个：{'、'.join(added)}"}
