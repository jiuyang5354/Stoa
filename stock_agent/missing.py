"""缺失调用记录与重放（v0.37）：工具失败不再只是一段 {"error": …} 字符串进上下文就没了。

每次失败记成结构化条目（工具名、参数、参数哈希、时间、错误、会话），存 数据目录/data/missing.json（只留最近 MAX 条，
同一调用重复失败合并计数）。回答下方的「缺失的数据」卡片列出本轮失败的调用，网页可一键原样重放（POST /api/missing/replay）；
重放成功标 resolved。点踩归到 harness 层（工具/数据缺失）时，待办里附上相关的缺失条目。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))
MAX = 300
_lock = threading.Lock()
# 这些「失败」是设计内的拦截或没有可重放意义，不记
SKIP_TOOLS = {"tool_search", "memory", "record_call", "confirm_action", "pending_actions", "write_file", "skill_save", "skill_delete",
              "task_create", "task_delete", "alert_set", "alert_delete", "watchlist"}


def _path():
    from .memory import home
    d = home() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d / "missing.json"


def call_hash(tool: str, args: dict) -> str:
    return hashlib.sha1(json.dumps([tool, args or {}], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def is_failure(result) -> str | None:
    """返回错误文字；不是失败返回 None。guard 拦截（blocked_by）不算数据缺失。"""
    if isinstance(result, dict):
        if result.get("blocked_by"):
            return None
        err = result.get("error")
        if err and len([k for k in result if not str(k).startswith("_")]) <= 6:
            return str(err)[:300]
    if isinstance(result, str) and result.strip().startswith(("工具出错", "Error", "错误：")):
        return result[:300]
    return None


def _load() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list[dict]):
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[-MAX:], ensure_ascii=False, indent=0, default=str), encoding="utf-8")
    os.replace(tmp, p)


def record(tool: str, args: dict, error: str, session: str = "", label: str = "") -> dict | None:
    if tool in SKIP_TOOLS:
        return None
    h = call_hash(tool, args)
    now = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        items = _load()
        for it in items:
            if it["hash"] == h and it["status"] != "resolved":
                it.update(error=error, last=now, count=it.get("count", 1) + 1, session=session or it.get("session"))
                items.remove(it)
                items.append(it)
                _save(items)
                return it
        it = {"id": h, "hash": h, "tool": tool, "label": label or tool, "args": args or {}, "error": error, "time": now, "last": now,
              "count": 1, "session": session, "status": "open"}
        items.append(it)
        _save(items)
        return it


def get(mid: str) -> dict | None:
    return next((it for it in _load() if it["id"] == mid), None)


def listing(status: str | None = None, limit: int = 50) -> list[dict]:
    items = [it for it in reversed(_load()) if not status or it["status"] == status]
    return items[:limit]


def mark(mid: str, status: str, note: str = "") -> dict | None:
    with _lock:
        items = _load()
        for it in items:
            if it["id"] == mid:
                it.update(status=status, note=note[:200], replayed=datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                          replays=it.get("replays", 0) + 1)
                _save(items)
                return it
    return None


def replay(mid: str, runner) -> dict:
    """原样重放：runner(tool, args) → 结果。成功标 resolved 并返回结果摘要；仍失败更新错误。"""
    it = get(mid)
    if not it:
        return {"error": "没有这条缺失记录"}
    try:
        res = runner(it["tool"], it["args"])
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    err = is_failure(res)
    if err:
        mark(mid, "open", err)
        return {"id": mid, "ok": False, "error": err, "tool": it["tool"]}
    mark(mid, "resolved", "重放成功")
    brief = json.dumps(res, ensure_ascii=False, default=str)
    return {"id": mid, "ok": True, "tool": it["tool"], "preview": brief[:600] + ("…" if len(brief) > 600 else "")}


def card(entries: list[dict]) -> dict | None:
    items = [{"id": e["id"], "tool": e["tool"], "label": e.get("label") or e["tool"], "args": e.get("args"), "error": (e.get("error") or "")[:160],
              "count": e.get("count", 1)} for e in entries if e]
    if not items:
        return None
    seen, uniq = set(), []
    for x in items:
        if x["id"] not in seen:
            seen.add(x["id"])
            uniq.append(x)
    return {"type": "missing", "items": uniq[:8], "source": "Stoa 工具调用记录", "warnings": [], "cite": None}
