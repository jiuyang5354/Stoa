"""长任务的「账」：都在 数据目录/data/runs.db（SQLite），进程被关、崩溃、重开后仍在。

1. runs / run_steps —— Agent 运行断点：每次模型回复、每个工具结果按 (run_id, 步号) 落库。
   重开后 Agent.resume(run_id) 按原样重放已落库的步骤（模型不重新调用、工具不重跑），从断点继续。
2. outbox —— 推送幂等：每条推送带 key（任务 id + 计划时间），按「key + 渠道」记已发；重试 / 补跑同一个 key 不会重复推。
3. jobs —— 定时报告 / 定时任务的每一次计划执行（key = 任务 + 计划时间）：pending → running → done / skipped / error。
   重启后据此判断哪些错过了、要不要补发（补发策略见 schedule.py）。
4. approvals —— 需要用户点头的动作（覆盖已有文件、往新开的推送渠道发消息）挂起存库，重启后还在；批准后再执行。
5. trusted_channels —— 用户确认过的推送渠道。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
_LOCK = threading.RLock()
RETRY_AFTER = timedelta(minutes=5)  # 计划执行出错后，至少隔这么久再重试
_DB: dict[str, sqlite3.Connection] = {}


def now() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")


def db() -> sqlite3.Connection:
    from .memory import home
    p = home() / "data" / "runs.db"
    key = str(p)
    with _LOCK:
        c = _DB.get(key)
        if c is None:
            p.parent.mkdir(parents=True, exist_ok=True)
            fresh = not p.exists()
            c = sqlite3.connect(p, check_same_thread=False, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, session TEXT, channel TEXT, text TEXT, user_msg_id INTEGER,
                    status TEXT, started TEXT, updated TEXT, error TEXT, reply TEXT, attempts INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS run_steps(run_id TEXT, step INTEGER, kind TEXT, name TEXT, payload TEXT, ts TEXT,
                    PRIMARY KEY(run_id, step));
                CREATE TABLE IF NOT EXISTS outbox(key TEXT, channel TEXT, title TEXT, ts TEXT, PRIMARY KEY(key, channel));
                CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY, kind TEXT, planned TEXT, status TEXT, started TEXT, finished TEXT,
                    late INTEGER DEFAULT 0, note TEXT);
                CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, kind TEXT, title TEXT, payload TEXT, status TEXT, created TEXT,
                    decided TEXT, session TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS trusted_channels(channel TEXT PRIMARY KEY, ts TEXT, how TEXT);
                CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            """)
            if fresh:
                c.execute("INSERT OR REPLACE INTO meta VALUES('created', ?)", (now(),))
            c.commit()
            _DB[key] = c
        return c


def _q(sql: str, params=(), one=False):
    with _LOCK:
        cur = db().execute(sql, params)
        names = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        db().commit()
    return (rows[0] if rows else None) if one else rows


def _x(sql: str, params=()):
    with _LOCK:
        cur = db().execute(sql, params)
        db().commit()
        return cur


def meta(k: str, v: str | None = None) -> str | None:
    if v is not None:
        _x("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
        return v
    r = _q("SELECT v FROM meta WHERE k=?", (k,), one=True)
    return r["v"] if r else None


# ---------------------------------------------------------------- 1. Agent 运行断点
def run_start(session: str, channel: str, text: str, user_msg_id: int | None) -> str:
    rid = datetime.now(CN).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    _x("INSERT INTO runs(run_id, session, channel, text, user_msg_id, status, started, updated) VALUES(?,?,?,?,?,?,?,?)",
       (rid, session, channel, text, user_msg_id, "running", now(), now()))
    return rid


def run_get(rid: str) -> dict | None:
    return _q("SELECT * FROM runs WHERE run_id=?", (rid,), one=True)


def step_get(rid: str, step: int) -> dict | None:
    r = _q("SELECT kind, name, payload FROM run_steps WHERE run_id=? AND step=?", (rid, step), one=True)
    if r:
        r["payload"] = json.loads(r["payload"])
    return r


def step_put(rid: str, step: int, kind: str, name: str, payload) -> None:
    _x("INSERT OR REPLACE INTO run_steps VALUES(?,?,?,?,?,?)", (rid, step, kind, name, json.dumps(payload, ensure_ascii=False, default=str), now()))
    _x("UPDATE runs SET updated=? WHERE run_id=?", (now(), rid))


def steps(rid: str) -> list[dict]:
    return _q("SELECT step, kind, name, ts FROM run_steps WHERE run_id=? ORDER BY step", (rid,))


def run_finish(rid: str, status: str, reply: str | None = None, error: str | None = None) -> None:
    _x("UPDATE runs SET status=?, reply=?, error=?, updated=? WHERE run_id=?", (status, (reply or "")[:4000] or None, error, now(), rid))


def run_retry(rid: str) -> None:
    _x("UPDATE runs SET status='running', attempts=attempts+1, updated=? WHERE run_id=?", (now(), rid))


def interrupted(max_age_hours: float = 6, stale_seconds: float = 0) -> list[dict]:
    """没跑完的运行：status 为 running（进程死了）或 failed（异常中断），且在 max_age_hours 内开始。
    stale_seconds>0 时只要 updated 早于这么多秒前的（排除本进程正在跑的）。"""
    since = (datetime.now(CN) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = _q("SELECT r.*, (SELECT COUNT(*) FROM run_steps s WHERE s.run_id=r.run_id) AS n_steps FROM runs r "
              "WHERE status IN ('running','failed') AND started>=? ORDER BY started", (since,))
    if stale_seconds:
        cut = (datetime.now(CN) - timedelta(seconds=stale_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        rows = [r for r in rows if r["updated"] <= cut]
    return rows


def runs_list(limit: int = 30) -> list[dict]:
    return _q("SELECT r.run_id, r.session, r.status, r.started, r.updated, r.attempts, substr(r.text,1,80) AS text, r.error, "
              "(SELECT COUNT(*) FROM run_steps s WHERE s.run_id=r.run_id) AS n_steps FROM runs r ORDER BY started DESC, rowid DESC LIMIT ?", (limit,))


def prune(days: int = 7) -> int:
    cut = (datetime.now(CN) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    old = [r["run_id"] for r in _q("SELECT run_id FROM runs WHERE updated<?", (cut,))]
    for rid in old:
        _x("DELETE FROM run_steps WHERE run_id=?", (rid,))
        _x("DELETE FROM runs WHERE run_id=?", (rid,))
    _x("DELETE FROM outbox WHERE ts<?", ((datetime.now(CN) - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S"),))
    return len(old)


# ---------------------------------------------------------------- 2. 推送幂等
def outbox_sent(key: str) -> set[str]:
    return {r["channel"] for r in _q("SELECT channel FROM outbox WHERE key=?", (key,))}


def outbox_mark(key: str, channel: str, title: str = "") -> None:
    _x("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?)", (key, channel, title[:120], now()))


# ---------------------------------------------------------------- 3. 计划执行
def job_get(key: str) -> dict | None:
    return _q("SELECT * FROM jobs WHERE key=?", (key,), one=True)


def job_claim(key: str, kind: str, planned: str, late: bool = False, force: bool = False) -> bool:
    """原子占位：pending/不存在/上次 running 已超时 → running，返回 True；已 done/skipped/正在跑 → False。"""
    with _LOCK:
        j = job_get(key)
        if j and j["status"] in ("done", "skipped"):
            return False
        if j and j["status"] == "running" and not force:
            started = datetime.strptime(j["started"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
            if datetime.now(CN) - started < timedelta(minutes=30):
                return False
        if j and j["status"] == "error" and j.get("finished") and not force:  # 出错后至少隔 5 分钟再重试
            fin = datetime.strptime(j["finished"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
            if datetime.now(CN) - fin < RETRY_AFTER:
                return False
        _x("INSERT OR REPLACE INTO jobs(key, kind, planned, status, started, late, note) VALUES(?,?,?,?,?,?,?)",
           (key, kind, planned, "running", now(), int(late), (j or {}).get("note")))
        return True


def job_done(key: str, status: str = "done", note: str | None = None) -> None:
    _x("UPDATE jobs SET status=?, finished=?, note=COALESCE(?, note) WHERE key=?", (status, now(), note, key))


def job_skip(key: str, kind: str, planned: str, note: str) -> None:
    _x("INSERT OR REPLACE INTO jobs(key, kind, planned, status, started, finished, note) VALUES(?,?,?,?,?,?,?)",
       (key, kind, planned, "skipped", now(), now(), note))


def jobs_list(limit: int = 30) -> list[dict]:
    return _q("SELECT * FROM jobs ORDER BY planned DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- 4. 待确认动作
def approval_add(kind: str, title: str, payload: dict, session: str | None = None, dedupe: str | None = None) -> dict:
    if dedupe:  # 同一件事只挂一条
        for a in approvals("pending"):
            if a["kind"] == kind and a["payload"].get("_dedupe") == dedupe:
                return a
        payload = {**payload, "_dedupe": dedupe}
    aid = uuid.uuid4().hex[:10]
    _x("INSERT INTO approvals(id, kind, title, payload, status, created, session) VALUES(?,?,?,?,?,?,?)",
       (aid, kind, title, json.dumps(payload, ensure_ascii=False, default=str), "pending", now(), session))
    return approval_get(aid)


def approval_get(aid: str) -> dict | None:
    r = _q("SELECT * FROM approvals WHERE id=?", (aid,), one=True)
    if r:
        r["payload"] = json.loads(r["payload"] or "{}")
    return r


def approvals(status: str | None = "pending") -> list[dict]:
    rows = _q("SELECT * FROM approvals" + (" WHERE status=?" if status else "") + " ORDER BY created DESC LIMIT 100", (status,) if status else ())
    for r in rows:
        r["payload"] = json.loads(r["payload"] or "{}")
    return rows


def approval_decide(aid: str, approve: bool) -> dict:
    """批准 → 执行挂起的动作（handlers 里按 kind 找执行函数）；拒绝 → 只记结果。重复决定无效。"""
    a = approval_get(aid)
    if not a:
        return {"error": f"没有 id 为 {aid} 的待确认动作"}
    if a["status"] != "pending":
        return {"error": f"这条已经{ {'approved': '批准', 'rejected': '拒绝', 'failed': '执行失败'}.get(a['status'], a['status']) }过了", "approval": a}
    if not approve:
        _x("UPDATE approvals SET status='rejected', decided=? WHERE id=?", (now(), aid))
        return {"ok": True, "status": "rejected", "id": aid}
    from . import notify, exports  # noqa: F401  确保各模块的执行函数已注册
    fn = HANDLERS.get(a["kind"])
    try:
        res = fn(a["payload"]) if fn else {"note": "无需执行"}
        st = "approved"
    except Exception as e:
        res, st = {"error": str(e)[:300]}, "failed"
    _x("UPDATE approvals SET status=?, decided=?, result=? WHERE id=?", (st, now(), json.dumps(res, ensure_ascii=False, default=str)[:2000], aid))
    return {"ok": st == "approved", "status": st, "id": aid, "result": res}


HANDLERS: dict = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


# ---------------------------------------------------------------- 5. 推送渠道信任
def trusted(channel: str) -> bool:
    return bool(_q("SELECT 1 FROM trusted_channels WHERE channel=?", (channel,)))


def trust(channel: str, how: str = "user") -> None:
    _x("INSERT OR REPLACE INTO trusted_channels VALUES(?,?,?)", (channel, now(), how))


def trusted_list() -> list[dict]:
    return _q("SELECT * FROM trusted_channels ORDER BY ts")


def wait_until(pred, timeout: float = 5.0, every: float = 0.05) -> bool:  # 测试用
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(every)
    return False


def jobs_stuck(kind: str) -> list[dict]:
    """上个进程留下的 running（重启时调用：这些一定是跑到一半被打断的）。"""
    return _q("SELECT * FROM jobs WHERE kind=? AND status='running' ORDER BY planned", (kind,))
