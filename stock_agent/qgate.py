"""Agora 子报告质量门（v0.37）：每位分析师的报告进辩论前先过硬检查，烂报告不进辩论。

评级（规则，0 次模型请求）：
- F：空报告（没有摘要也没有证据）
- D：报告正文过短（< min_chars() 字符），或含「无法获取 / 工具调用失败 / I cannot retrieve」这类放弃标记
  （参考实现对自由文本报告用 200 字符；Stoa 的分析师输出是压缩 JSON——摘要 ≤200 字、证据 ≤5 条，一份带数字的合格技术面报告约 70 字，
   所以默认 60 字符，AGORA_QGATE_MIN_CHARS 可调）
- B：通过但证据里一个数字都没有（只是提示，不拦）
- A：通过
F/D → 预算允许时带着不合格原因重做一次（最多一次，计入同一个调用预算，保证后面辩论/风控/裁决的调用不被挤掉）；
仍不合格（或没预算）→ 降级为规则摘要（status=gated），辩论里标「规则降级」，不当成分析师观点。
"""
from __future__ import annotations

import os
import re

MIN_CHARS = 60


def min_chars() -> int:
    try:
        return max(1, int(os.getenv("AGORA_QGATE_MIN_CHARS") or MIN_CHARS))
    except ValueError:
        return MIN_CHARS
BAD = re.compile(r"无法获取|未能获取|无法取得|获取失败|工具调用失败|调用失败|数据(?:暂)?不可用|没有(?:可用|拿到)(?:的)?数据|无法访问|无法查询|暂无数据|"
                 r"I (?:cannot|can't|am unable to) (?:retrieve|access|fetch|get)|unable to (?:retrieve|access|fetch)|tool (?:call )?failed|no data available|"
                 r"as an AI|作为(?:一个)?AI", re.I)


def text_of(report: dict) -> str:
    return " ".join([str(report.get("summary") or "")] + [str(x) for x in report.get("evidence") or []]).strip()


def grade(report: dict) -> tuple[str, list[str]]:
    t = text_of(report)
    if not t:
        return "F", ["空报告"]
    why = []
    n = min_chars()
    if len(t) < n:
        why.append(f"过短（{len(t)} 字符 < {n}）")
    m = BAD.search(t)
    if m:
        why.append(f"含放弃标记「{m.group(0)}」")
    if why:
        return "D", why
    if not re.search(r"\d", " ".join(str(x) for x in report.get("evidence") or [])):
        return "B", ["证据里没有数字"]
    return "A", []


def retry_allowed(budget, reserve: int) -> bool:
    """重做最多一次，且只在调用预算够用时：留出后面多空辩论 + 风控 + 裁决要用的调用数。AGORA_QGATE_RETRY=off 关闭重做。"""
    if (os.getenv("AGORA_QGATE_RETRY") or "on").lower() in ("off", "0", "false"):
        return False
    try:
        return budget.left() > reserve
    except Exception:
        return False


def retry_note(why: list[str]) -> str:
    return ("\n\n[上一版报告没有通过质量检查：" + "；".join(why) + "。请只基于上面的数据重写：summary 写清结论，evidence 至少 3 条、每条带数据里的具体数字；"
            "某项数据缺失就直接跳过它，不要写「无法获取」之类的话。]")
