"""定时早晚报的调度与补跑策略（v0.34）。

每次计划执行有一个键 report:<场次>:<计划时间>，状态记在 ledger.jobs（重启后还在）：
- 准点（晚 ≤ 2 分钟）→ 正常生成并推送；
- 错过了（服务没开 / 电脑睡眠）但没超过该场次的「最长补发时限」→ 开机后补发，报告标题标「补发」，正文写原定时间；
- 超过时限 → 跳过并记一条 skipped（过期的早报没有意义）；
- 上次出错 → 下一轮重试，推送用同一个键（ledger.outbox），已经推过的渠道不会再推。

配置（环境变量，设置页同名项；config.yaml 的 schedule: 段作默认）：
  REPORT_TIMES     morning=08:30,evening=21:00（off 关闭）
  REPORT_CATCHUP   on（默认）/ off：错过的要不要补
  REPORT_MAX_LATE  morning=3,evening=12（小时）：超过就不补
  TASK_CATCHUP_HOURS 6：通用定时任务错过多久以内补跑（见 tasks.py）
只在工作日（周一到周五）出报告。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
GRACE = timedelta(minutes=2)
DEFAULT_LATE = {"morning": 3.0, "evening": 12.0}


def _cfg() -> dict:
    try:
        from .agent import load_cfg
        return load_cfg().get("schedule") or {}
    except Exception:
        return {}


def report_times() -> dict:
    raw = os.getenv("REPORT_TIMES") or _cfg().get("report_times") or "morning=08:30,evening=21:00"
    if str(raw).strip().lower() == "off":
        return {}
    return {s.split("=")[0].strip(): s.split("=")[1].strip() for s in str(raw).split(",") if "=" in s}


def catchup_on() -> bool:
    v = os.getenv("REPORT_CATCHUP") or str(_cfg().get("catchup", "on"))
    return v.strip().lower() not in ("off", "0", "false", "no")


def max_late() -> dict:
    out = dict(DEFAULT_LATE)
    raw = os.getenv("REPORT_MAX_LATE") or _cfg().get("max_late_hours")
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = [s.split("=", 1) for s in str(raw or "").split(",") if "=" in s]
    for k, v in items:
        try:
            out[str(k).strip()] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def task_catchup() -> timedelta:
    try:
        return timedelta(hours=float(os.getenv("TASK_CATCHUP_HOURS") or _cfg().get("task_catchup_hours") or 6))
    except ValueError:
        return timedelta(hours=6)


def planned_slots(now: datetime, times: dict, lookback_hours: float) -> list[tuple[str, datetime]]:
    """now 往回 lookback 小时内（含当前）所有工作日的计划时刻。"""
    out = []
    for back in range(0, int(lookback_hours // 24) + 2):
        day = (now - timedelta(days=back)).date()
        if day.weekday() >= 5:
            continue
        for session, hhmm in times.items():
            try:
                h, m = (int(x) for x in hhmm.split(":"))
            except ValueError:
                continue
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=CN)
            if t <= now and now - t <= timedelta(hours=lookback_hours):
                out.append((session, t))
    return sorted(out, key=lambda x: x[1])


def late_note(planned: datetime, now: datetime) -> str:
    mins = int((now - planned).total_seconds() // 60)
    span = f"{mins // 60} 小时 {mins % 60} 分钟" if mins >= 60 else f"{mins} 分钟"
    return f"原定 {planned:%m-%d %H:%M}，补发于 {now:%m-%d %H:%M}（晚了 {span}）"


def tick_reports(runner, now: datetime | None = None, log=print) -> list[str]:
    """runner(session, planned_str, late_note|None, key) -> 返回码（0 成功）。返回本轮处理过的键。"""
    from . import ledger
    now = (now or datetime.now(CN)).replace(second=0, microsecond=0)
    times = report_times()
    if not times:
        return []
    lim = max_late()
    look = max([lim.get(s, 3.0) for s in times] + [0.1])
    done = []
    for session, t in planned_slots(now, times, look + 24):  # 多看一天：过期的也记一条 skipped，界面上看得到
        key = f"report:{session}:{t:%Y-%m-%d %H:%M}"
        j = ledger.job_get(key)
        if j and j["status"] in ("done", "skipped"):
            continue
        late = now - t
        on_time = late <= GRACE
        if not on_time and (not catchup_on() or late > timedelta(hours=lim.get(session, 3.0))):
            if not j:
                ledger.job_skip(key, "report", f"{t:%Y-%m-%d %H:%M}",
                                "补跑已关闭" if not catchup_on() else f"过期 {late.total_seconds() / 3600:.1f} 小时，超过补发时限 {lim.get(session, 3.0):g} 小时")
                log("报告错过太久，跳过", key)
            continue
        if not ledger.job_claim(key, "report", f"{t:%Y-%m-%d %H:%M}", late=not on_time):
            continue
        note = None if on_time else late_note(t, now)
        log("生成报告", session, "（补发）" if note else "")
        try:
            rc = runner(session, f"{t:%Y-%m-%d %H:%M}", note, key)
        except Exception as e:
            rc = 1
            log("报告出错", e)
        ledger.job_done(key, "done" if rc == 0 else "error", note)
        done.append(key)
    return done
