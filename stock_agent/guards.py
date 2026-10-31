"""硬约束：放在代码里，不靠提示词。Agent 每次调工具前过一遍 check()，违规直接返回错误给模型，工具不执行。

规则（都有单测）：
1. confirm_action 只在用户本轮明确表态（确认/同意/批准/拒绝…）时可用；定时任务、早晚报等无人值守运行里一律不行——模型不能替用户点头。
2. 无人值守运行（提问以「[定时任务」开头）不能做破坏性或「生出新任务」的操作：建/删定时任务、删提醒、删技能、批准动作、改自选股。
3. record_call 入账前校验：方向只能是 看涨/看跌/震荡，周期 1–365 天，置信度 0–100，必须写理由——账本是评估自己的依据，脏数据进不去。
4. 单轮副作用上限：一轮对话里写记忆 ≤ 8 次、记判断 ≤ 6 次、设提醒 ≤ 10 次、写文件 ≤ 6 次，防止模型陷入循环刷写。
5. 覆盖已有文件 → 不直接写，转为待确认（exports.write(replace=True) 里实现）。往新推送渠道发消息 → 待确认（notify.push 里实现）。
"""
from __future__ import annotations

import re

UNATTENDED_PREFIXES = ("[定时任务",)
CONSENT_YES = re.compile(r"(?<![待要需])确认(?![吗么一下什的])|同意|批准|准了|可以(?:执行|覆盖|发|写)?|执行吧|就这么办|没问题|好的|是的|覆盖吧|\bapprove\b|\bconfirm\b|\byes\b|\bok(?:ay)?\b|go ahead", re.I)
CONSENT_NO = re.compile(r"拒绝|不要|别(?:覆盖|发|写|执行)|取消|算了|不同意|不行|\breject\b|\bdeny\b|\bno\b|\bcancel\b", re.I)
UNATTENDED_BLOCK = {"task_create", "task_delete", "alert_delete", "skill_delete", "confirm_action", "watchlist"}
VIEWS = {"看涨", "看跌", "震荡"}
TURN_LIMITS = {"memory": 8, "record_call": 6, "alert_set": 10, "write_file": 6, "skill_save": 3}


def unattended(user_text: str) -> bool:
    return (user_text or "").lstrip().startswith(UNATTENDED_PREFIXES)


def check(name: str, args: dict, user_text: str, counts: dict) -> dict | None:
    """违规返回 {"error", "blocked_by"}；通过返回 None。counts：本轮各工具已执行次数（调用方维护）。"""
    text = user_text or ""
    if unattended(text) and name in UNATTENDED_BLOCK:
        return _no("unattended", f"无人值守运行（定时任务）里不能调用 {name}；需要的话在结果里提醒用户自己处理。")
    if name == "confirm_action":
        want = bool(args.get("approve"))
        if want and (not CONSENT_YES.search(text) or CONSENT_NO.search(text)):  # 有任何否定词就不算同意（宁可多问一次）
            return _no("consent", "用户本轮没有明确表示确认，不能替用户批准。先用 pending_actions 列出待确认的动作，问用户要不要执行。")
        if not want and not (CONSENT_NO.search(text) or CONSENT_YES.search(text)):
            return _no("consent", "用户本轮没有明确表态，不要替用户拒绝；先问用户。")
    if name == "record_call":
        v = str(args.get("view", ""))
        if v not in VIEWS:
            return _no("ledger", f"view 只能是 {'/'.join(sorted(VIEWS))}，收到「{v}」")
        try:
            h = int(args.get("horizon_days"))
        except (TypeError, ValueError):
            return _no("ledger", "horizon_days 要填 1–365 的整数")
        if not 1 <= h <= 365:
            return _no("ledger", "horizon_days 要在 1–365 天之间")
        c = args.get("confidence")
        if c not in (None, ""):
            try:
                if not 0 <= float(c) <= 100:
                    raise ValueError
            except (TypeError, ValueError):
                return _no("ledger", "confidence 要在 0–100 之间")
        if len(str(args.get("reason") or "").strip()) < 4:
            return _no("ledger", "记判断要写清楚理由（reason）")
    lim = TURN_LIMITS.get(name)
    if lim is not None and counts.get(name, 0) >= lim:
        return _no("turn_limit", f"这一轮 {name} 已经调用 {counts[name]} 次（上限 {lim}），停下来整理后再回答。")
    return None


def _no(rule: str, msg: str) -> dict:
    return {"error": msg, "blocked_by": f"guard:{rule}"}
