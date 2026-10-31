"""通用定时任务：用户说「每天 9 点帮我…」，Agent 建一个任务，serve 后台按时把提示词交给 Agent 跑一遍。

存 数据目录/data/tasks.json。每个任务的结果写进自己的对话 web-task-<id>（History 里能看到），
同时进提醒事件流（网页弹 toast、桌面通知）并推送到已开的渠道。
时间一律北京时间。服务停着时错过的：TASK_CATCHUP_HOURS（默认 6）小时以内补一次并标「补发」，更久的跳过。
"""
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .memory import home

CN = timezone(timedelta(hours=8))
KINDS = {"daily": "每天", "weekdays": "工作日", "weekly": "每周", "interval": "每隔", "once": "一次"}
WEEK = "一二三四五六日"
LOCK = threading.Lock()
MAX_TASKS = 50
MIN_INTERVAL = 30          # 分钟
CATCH_UP = timedelta(hours=6)
TASK_PREFIX = "[定时任务"


def _path():
    return home() / "data" / "tasks.json"


def now() -> datetime:
    return datetime.now(CN).replace(second=0, microsecond=0)


def _fmt(t: datetime | None) -> str | None:
    return t.strftime("%Y-%m-%d %H:%M") if t else None


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:16].replace("T", " "), "%Y-%m-%d %H:%M").replace(tzinfo=CN)
    except ValueError:
        return None


def load() -> list[dict]:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("tasks", [])
        except Exception:
            return []
    return []


def save(ts: list[dict]):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"tasks": ts}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _hm(s) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:：点](\d{0,2})\s*", str(s or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def next_run(t: dict, after: datetime) -> datetime | None:
    """after 之后（不含）的下一次运行时间。"""
    k = t["kind"]
    if k == "once":
        at = _parse(t.get("at"))
        return at if at and at > after else None
    if k == "interval":
        return after + timedelta(minutes=max(MIN_INTERVAL, int(t.get("every_minutes") or 60)))
    h, mi = _hm(t.get("time")) or (9, 0)
    days = {"daily": range(7), "weekdays": range(5), "weekly": t.get("weekdays") or [0]}[k]
    c = after.replace(hour=h, minute=mi, second=0, microsecond=0)
    if c <= after:
        c += timedelta(days=1)
    for _ in range(8):
        if c.weekday() in days:
            return c
        c += timedelta(days=1)
    return None


def describe(t: dict) -> str:
    k = t["kind"]
    if k == "once":
        return f"{t.get('at')} 一次"
    if k == "interval":
        m = int(t.get("every_minutes") or 60)
        return f"每 {m // 60} 小时" if m % 60 == 0 else f"每 {m} 分钟"
    if k == "weekly":
        return "每周" + "、".join(WEEK[d] for d in sorted(t.get("weekdays") or [0])) + " " + t.get("time", "")
    return f"{KINDS[k]} {t.get('time', '')}"


def add(prompt: str, kind: str = "daily", time: str | None = None, weekdays=None, every_minutes=None, at: str | None = None,
        title: str = "", session: str | None = None) -> dict:
    prompt = str(prompt or "").strip()
    if not prompt:
        return {"error": "要写清楚到时候做什么（prompt）"}
    if kind not in KINDS:
        return {"error": f"kind 只能是 {'/'.join(KINDS)}"}
    t = {"id": uuid.uuid4().hex[:8], "title": (str(title or "").strip() or prompt)[:40], "prompt": prompt[:2000], "kind": kind,
         "created": _fmt(now()), "active": True, "runs": 0, "last_run": None, "last_status": None, "last_preview": None,
         "origin": session}
    if kind in ("daily", "weekdays", "weekly"):
        hm = _hm(time)
        if not hm:
            return {"error": "time 要写成 HH:MM，如 09:00"}
        t["time"] = f"{hm[0]:02d}:{hm[1]:02d}"
    if kind == "weekly":
        wd = sorted({int(d) for d in (weekdays or []) if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
        if not wd:
            return {"error": "每周任务要给 weekdays（0=周一 … 6=周日）"}
        t["weekdays"] = wd
    if kind == "interval":
        try:
            m = int(every_minutes)
        except (TypeError, ValueError):
            return {"error": "every_minutes 要填分钟数"}
        if m < MIN_INTERVAL:
            return {"error": f"最短每 {MIN_INTERVAL} 分钟一次"}
        t["every_minutes"] = m
    if kind == "once":
        a = _parse(at)
        if not a:
            return {"error": "at 要写成 YYYY-MM-DD HH:MM（北京时间）"}
        if a <= now():
            return {"error": "这个时间已经过了"}
        t["at"] = _fmt(a)
    t["next_run"] = _fmt(next_run(t, now()))
    with LOCK:
        ts = load()
        if len([x for x in ts if x.get("active")]) >= MAX_TASKS:
            return {"error": f"最多 {MAX_TASKS} 个定时任务，先删掉一些"}
        ts.append(t)
        save(ts)
    return {"ok": True, "id": t["id"], "title": t["title"], "schedule": describe(t), "next_run": t["next_run"],
            "note": "结果会写进 History 里的「定时 · 标题」对话，并弹提醒、推送到已开的渠道。"}


def remove(tid: str) -> dict:
    with LOCK:
        ts = load()
        left = [t for t in ts if t["id"] != str(tid).strip()]
        if len(left) == len(ts):
            return {"error": f"没有 id 为 {tid} 的任务"}
        save(left)
    return {"ok": True, "msg": f"已删除任务 {tid}"}


def set_active(tid: str, active: bool) -> dict:
    with LOCK:
        ts = load()
        for t in ts:
            if t["id"] == tid:
                t["active"] = bool(active)
                t["next_run"] = _fmt(next_run(t, now())) if active else None
                save(ts)
                return {"ok": True}
    return {"error": "not found"}


def listing() -> list[dict]:
    return [dict(t, schedule=describe(t), session="task-" + t["id"]) for t in load()]


def due(at: datetime | None = None) -> list[dict]:
    at = at or now()
    out = []
    for t in load():
        nr = _parse(t.get("next_run"))
        if t.get("active") and nr and nr <= at:
            out.append(t)
    return out


def run_one(t: dict, runner, push=None, log=print, at: datetime | None = None, planned: str | None = None, recovering: bool = False) -> dict:
    """跑一个任务：runner(session_id, text) -> 回答文字。
    v0.34：每次计划执行一个键 task:<id>:<计划时间>，记在 ledger.jobs——同一个键不会跑两次、推两次（推送也用这个键去重）；
    晚于计划 2 分钟以上算补跑，结果与推送标「补发」；错过超过 TASK_CATCHUP_HOURS（默认 6 小时）就跳过。"""
    from . import ledger, schedule
    at = at or now()
    planned = planned or t.get("next_run")
    nr = _parse(planned)
    lateness = (at - nr) if nr else timedelta(0)
    stale = nr is not None and lateness > schedule.task_catchup()
    if not recovering:
        with LOCK:
            ts = load()
            cur = next((x for x in ts if x["id"] == t["id"]), None)
            if not cur:
                return {"skipped": True}
            nxt = next_run(cur, at)
            cur["next_run"] = _fmt(nxt)
            if cur["kind"] == "once" or nxt is None:
                cur["active"] = False
            save(ts)
    key = f"task:{t['id']}:{planned}"
    if stale:  # 服务停了很久：错过的不补，直接排下一次
        ledger.job_skip(key, "task", planned or "", f"错过 {lateness}，超过补跑时限")
        log("定时任务错过太久，跳过", t["id"], t.get("title"))
        return {"skipped": True, "stale": True}
    if not ledger.job_claim(key, "task", planned or "", late=lateness > timedelta(minutes=2), force=recovering):
        return {"skipped": True, "duplicate": True}
    late = lateness > timedelta(minutes=2)
    note = f"原定 {planned}，补发于 {_fmt(at)}" if late else ""
    text = (f"{TASK_PREFIX}「{t['title']}」· {describe(t)} · 自动执行，用户不在线；直接完成任务给出结果，不要反问，不要再创建定时任务]\n"
            f"{t['prompt']}")
    sid = "task-" + t["id"]
    try:
        reply = runner(sid, text) or ""
        ok = not reply.startswith("出错了")
    except Exception as e:
        reply, ok = f"出错了：{e}", False
    if late:
        reply = f"【补发】{note}\n\n" + reply
    preview = " ".join(re.sub(r"\[\[[^\]]*\]\]|\[\d+\]|[#*>`]", "", reply).split())[:120]
    with LOCK:
        ts = load()
        for x in ts:
            if x["id"] == t["id"]:
                x["runs"] = int(x.get("runs") or 0) + 1
                x["last_run"] = _fmt(at)
                x["last_status"] = "ok" if ok else "error"
                x["last_preview"] = preview
        save(ts)
    try:
        from . import alerts
        alerts.add_events([{"time": _fmt(at), "kind": "task", "title": f"{t['title']}{'（补发）' if late else ''}：{preview[:60] or '已完成'}", "session": sid, "task": t["id"]}])
    except Exception:
        pass
    if push is None:
        from . import notify
        push = notify.push
    title = f"Stoa 定时任务 · {t['title']}{'（补发）' if late else ''}"
    try:
        try:
            push(title, reply[:3500], key=key)
        except TypeError:  # 旧式 push(title, markdown)
            push(title, reply[:3500])
    except Exception:
        pass
    ledger.job_done(key, "done" if ok else "error", note or None)
    return {"ok": ok, "reply": reply, "late": late}


def recover(runner, push=None, log=print, at: datetime | None = None) -> int:
    """启动时调用：上个进程跑到一半的任务（job 停在 running）在补跑时限内重跑一次（推送键相同，不会重复推）。"""
    from . import ledger
    n = 0
    tasks_by_id = {x["id"]: x for x in load()}
    for j in ledger.jobs_stuck("task"):
        tid = j["key"].split(":")[1]
        t = tasks_by_id.get(tid)
        if not t:
            ledger.job_done(j["key"], "skipped", "任务已删除")
            continue
        log("补跑中断的定时任务", tid, t.get("title"))
        r = run_one(t, runner, push=push, log=log, at=at, planned=j["planned"], recovering=True)
        n += 0 if r.get("skipped") else 1
    return n


def tick(runner, push=None, log=print, at: datetime | None = None) -> int:
    n = 0
    for t in due(at):
        log("定时任务", t["id"], t.get("title"))
        r = run_one(t, runner, push=push, log=log, at=at)
        n += 0 if r.get("skipped") else 1
    return n


def loop(runner, log=print):
    import time
    time.sleep(10)
    try:
        recover(runner, log=log)
    except Exception as e:
        log("补跑中断任务出错", e)
    while True:
        try:
            tick(runner, log=log)
        except Exception as e:
            log("定时任务出错", e)
        time.sleep(30)
