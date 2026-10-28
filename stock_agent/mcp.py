"""MCP 服务端：把行情、记忆、判断账本和整个 Agent 暴露给 Claude Desktop、Cursor、Cherry Studio 等 MCP 客户端。

本地（stdio）：  python -m stock_agent mcp
远程（HTTP）：   serve 模式下 POST http://host:8080/mcp （Authorization: Bearer <WEB_TOKEN>）
"""
import json
import sys
import threading

from . import __version__
from .agent import TOOLS, Agent

PROTOCOL = "2025-06-18"
_LOCK = threading.Lock()
_STATE: dict = {}

ASK = {"name": "ask_agent", "description": "把问题交给股市 Agent 本身回答：它会自己查行情、结合对用户的长期记忆、需要时记账判断。需要服务端配置了大模型。",
       "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}


def _agent(kind: str) -> Agent:
    with _LOCK:
        if kind not in _STATE:
            _STATE[kind] = Agent(channel=f"mcp-{kind}")
        return _STATE[kind]


def tool_list() -> list[dict]:
    a = _agent("tools")
    names = {t["name"] for t in a.tools}
    tools = [t for t in TOOLS if t["name"] in names] + ([ASK] if a.llm.ready else [])
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"]} for t in tools]


def call_tool(name: str, args: dict):
    if name == "ask_agent":
        a = _agent("chat")
        with _LOCK:
            return a.ask(str(args.get("question", ""))[:4000])
    a = _agent("tools")
    if name not in {t["name"] for t in a.tools}:
        raise ValueError(f"未知工具 {name}")
    return a.run_tool(name, args)


def handle(req: dict) -> dict | None:
    """处理一条 JSON-RPC 消息；通知（没有 id）返回 None。"""
    mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if mid is None:
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "stoa", "version": __version__},
                      "instructions": "A股/港股/美股行情、技术指标、用户长期记忆和判断账本。代码用 Yahoo 格式：600519.SS、0700.HK、AAPL、^GSPC。"}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list()}
        elif method == "tools/call":
            try:
                out = call_tool(params.get("name", ""), params.get("arguments") or {})
                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
                result = {"content": [{"type": "text", "text": text[:20000]}], "isError": False}
            except Exception as e:
                result = {"content": [{"type": "text", "text": f"出错：{e}"}], "isError": True}
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持 {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def handle_payload(payload):
    if isinstance(payload, list):
        out = [r for r in (handle(x) for x in payload) if r]
        return out or None
    return handle(payload)


def stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle_payload(json.loads(line))
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
