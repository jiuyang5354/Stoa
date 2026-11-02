"""按次质量指标（v0.40）：每轮结束往 数据目录/data/quality/YYYY-MM.jsonl 追加一行。

记什么：时间、会话、模型、耗时、prompt/completion/cached token、工具调用数、数字核对（关键数字/已核实/未核实）、
事后检查补了什么（失效条件/风险/免责）、闸门与兜底是否触发、是否超时收尾；点踩后补一行 {"ev": "down"}。
不记用户原文和回答原文（隐私），只记长度。写入失败绝不影响回答。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

_LOCK = threading.Lock()


def _now() -> datetime:
    """北京时间（与 Stoa 其它记录一致），去掉时区写成本地 ISO。"""
    from .memory import CN_TZ
    return datetime.now(CN_TZ).replace(tzinfo=None)


def folder() -> Path:
    from .memory import home
    return home() / "data" / "quality"


def _write(row: dict, when: datetime | None = None) -> None:
    when = when or _now()
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    with _LOCK, open(d / f"{when:%Y-%m}.jsonl", "a", encoding="utf-8") as f:
        f.write(line)


def usage_snapshot() -> dict:
    try:
        from .providers import USAGE
        return {k: int(USAGE.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "calls")}
    except Exception:
        return {}


def _verify_counts(reply: str, sources) -> dict:
    from . import cite
    if sources is None:
        return {}
    v = cite.verify(reply or "", sources)
    nums = [n for n in v.get("numbers") or [] if not n.get("date_only")]
    unver = sum(1 for n in nums if n.get("flagged")) + len(v.get("unverified") or [])  # 标了「未核实」的 + 仍找不到出处的
    return {"numbers": len(nums), "verified": len(nums) - unver, "unverified": unver}


def build(agent, question: str, reply: str, u0: dict, t0: float, timeout: bool = False) -> dict:
    """从 Agent 本轮的 last_meta / 计数里拼一行（不含原文）。"""
    lm = getattr(agent, "last_meta", None) or {}
    vm = lm.get("verify") or {}
    u1 = usage_snapshot()
    tok = {k: max(0, u1.get(k, 0) - (u0 or {}).get(k, 0)) for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "calls")}
    added = vm.get("added") or {}
    gates = list(vm.get("gates") or [])
    debate = None
    if lm.get("debate"):
        try:
            from . import debate as db
            rec = db.load(lm["debate"]) or {}
            debate = {"degraded": len(rec.get("degraded") or []),
                      "gated": sum(1 for r in rec.get("analysts") or [] if isinstance(r, dict) and r.get("status") == "gated")}
        except Exception:
            debate = None
    try:
        vc = _verify_counts(reply, getattr(agent, "sources", None))
    except Exception:
        vc = {}
    stopped = vm.get("stopped")
    row = {
        "ts": _now().isoformat(timespec="seconds"),
        "session": getattr(agent, "session", "") or "",
        "q_id": getattr(agent, "_cur_id", None),
        "channel": getattr(agent, "channel", "") or "",
        "model": agent._model_name() if hasattr(agent, "_model_name") else "",
        "secs": round(time.monotonic() - t0, 2),
        "mode": "debate" if lm.get("debate") else "quick",
        "prompt_tokens": tok["prompt_tokens"], "completion_tokens": tok["completion_tokens"], "cached_tokens": tok["cached_tokens"],
        "llm_calls": tok["calls"],
        "tools": len(lm.get("steps") or []),
        "verify": vc,
        "rewrote": bool(vm.get("first_unverified")),  # 核对不过打回重写过一次
        "added": sorted(k for k, x in added.items() if x and k != "risks") + [f"risk:{r}" for r in added.get("risks") or []],
        "gates": gates,
        "qgate": (debate or {}).get("gated", 0),
        "fallback": bool(stopped) or bool((debate or {}).get("degraded")) or bool(lm.get("partial")),
        "timeout": bool(timeout or lm.get("timeout")),
        "q_len": len(question or ""), "a_len": len(reply or ""),
    }
    if stopped:
        row["stopped"] = str(stopped)[:40]
    return row


def record(agent, question: str, reply: str, u0: dict, t0: float, timeout: bool = False) -> None:
    """Agent.ask 收尾时调用。任何异常都吞掉：写指标不能影响回答。"""
    try:
        if getattr(agent, "channel", "") == "eval":
            return
        _write(build(agent, question, reply, u0, t0, timeout))
    except Exception:
        pass


def mark_down(session: str, q_id) -> None:
    """点踩：补一行事件，汇总时按 (session, q_id) 合并到那一轮。"""
    try:
        if session and q_id is not None:
            _write({"ev": "down", "ts": _now().isoformat(timespec="seconds"), "session": session, "q_id": q_id})
    except Exception:
        pass


def load(days: int = 7, now: datetime | None = None) -> list[dict]:
    now = now or _now()
    since = now - timedelta(days=days)
    months = {f"{since:%Y-%m}", f"{now:%Y-%m}"}
    rows, downs = [], set()
    for m in sorted(months):
        p = folder() / f"{m}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r.get("ts"))
            except Exception:
                continue
            if ts < since:
                continue
            if r.get("ev") == "down":
                downs.add((r.get("session"), r.get("q_id")))
            else:
                rows.append(r)
    for r in rows:
        r["down"] = (r.get("session"), r.get("q_id")) in downs
    return rows


def _agg(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"turns": 0}
    nums = sum((r.get("verify") or {}).get("numbers", 0) for r in rows)
    unv = sum((r.get("verify") or {}).get("unverified", 0) for r in rows)
    avg = lambda k: round(sum(float(r.get(k) or 0) for r in rows) / n, 1)
    return {"turns": n, "avg_prompt": avg("prompt_tokens"), "avg_completion": avg("completion_tokens"), "avg_cached": avg("cached_tokens"),
            "avg_tokens": round(sum(float(r.get("prompt_tokens") or 0) + float(r.get("completion_tokens") or 0) for r in rows) / n, 1),
            "avg_secs": avg("secs"), "avg_tools": avg("tools"),
            "numbers": nums, "unverified": unv, "unverified_rate": round(unv / nums * 100, 1) if nums else None,
            "fallback_rate": round(sum(1 for r in rows if r.get("fallback") or r.get("timeout")) / n * 100, 1),
            "timeouts": sum(1 for r in rows if r.get("timeout")),
            "added": sum(1 for r in rows if r.get("added")), "downs": sum(1 for r in rows if r.get("down"))}


def summary(days: int = 7, now: datetime | None = None) -> dict:
    """设置 → 用量页的近 N 天视图：总体 + 按模型 + 按天。"""
    rows = load(days, now)
    by_model: dict[str, list] = {}
    by_day: dict[str, list] = {}
    for r in rows:
        by_model.setdefault(r.get("model") or "?", []).append(r)
        by_day.setdefault(str(r.get("ts"))[:10], []).append(r)
    return {"days": days, "total": _agg(rows),
            "models": sorted(({"model": m, **_agg(rs)} for m, rs in by_model.items()), key=lambda x: -x["turns"]),
            "daily": [{"date": d, "turns": len(rs)} for d, rs in sorted(by_day.items())]}
