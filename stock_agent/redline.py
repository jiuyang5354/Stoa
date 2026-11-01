"""自进化的安全红线（v0.39）：哪些改动永远要人批准、哪些直接拦下。

不能被自进化改掉的东西：
  · 风控红线（全仓 / 杠杆 / 保证收益的风险提示、失效条件、止损与仓位提醒、免责）
  · 出处与核对规则（引用编号、未核实标记、cite / answercheck / sanity / gates 的检查）
  · 提示注入防护（taint.py：外部内容只是资料）
  · guards.py 硬约束（确认、无人值守、记账校验、单轮上限）

规则：
  1. 改到这些代码文件（PROTECTED_FILES）或这些工具的描述（PROTECTED_TOOLS）→ 永远走人工批准（代码本来就只出补丁）。
  2. 技能 / 提示 / 记忆的新增或删除行里，带红线词又带指令词（「必须 / 不要 / 可以 / 跳过…」）→ 永远走人工批准，
     不管 evolve.write_approval 怎么配。
  3. 明显在削弱红线（「不用标出处」「可以全仓」「跳过核对」「忽略风险」…）或文字里有注入指令 → 直接拦下，不进待批准。
这些判断在 evolve.gate_write() 里调用；配置只能让普通改动更严，不能让红线改动自动生效。
"""
from __future__ import annotations

import difflib
import re

PROTECTED_FILES = {"guards.py", "taint.py", "cite.py", "answercheck.py", "sanity.py", "gates.py", "qgate.py", "redline.py", "evolve.py", "ledger.py"}
PROTECTED_TOOLS = {"record_call", "confirm_action", "pending_actions", "memory", "watchlist", "alert_set", "alert_delete", "task_create", "task_delete",
                   "skill_save", "skill_delete", "write_file", "run_python"}

RULE_TERMS = re.compile(
    r"风控|红线|风险提示|止损|失效条件|仓位|杠杆|全仓|满仓|梭哈|保证|承诺|稳赚|必涨|免责|仅供参考|不构成投资建议|"
    r"出处|引用|角标|\[编号\]|_cite|未核实|核对|核实|编造|注入|指令|系统提示|确认|批准|guard|taint|redline|write_approval|"
    r"citation|source|disclaimer|stop[- ]?loss|leverage|guarantee|injection|verify", re.I)
INSTRUCT = re.compile(
    r"不要|不用|无需|不必|不需要|必须|务必|一律|禁止|可以|允许|跳过|忽略|省略|去掉|删掉|删除|关闭|总是|永远|只要|直接|"
    r"\balways\b|\bnever\b|\bskip\b|\bignore\b|don'?t|no need|\bmust\b|\ballowed?\b|\bomit\b", re.I)
WEAKEN = re.compile(
    r"(?:不用|无需|不必|不需要|不要|别|省略|去掉|删掉|跳过|忽略|关闭)\s*(?:再)?\s*(?:标|写|加|附|给|做|提示|提醒|核对|核实|检查|引用)?\s*(?:出处|来源|引用|角标|编号|免责|免责声明|风险提示|风险|失效条件|止损|核对|核实|确认|批准)|"
    r"(?<![不别勿没])(?:可以|建议|直接|放心|鼓励)[^，。；,;不别勿没]{0,4}(?:全仓|满仓|梭哈|加杠杆|上杠杆|借钱|配资|融资买)|"
    r"(?:保证|承诺)\s*(?:收益|赚|涨|翻倍)|"
    r"(?:无需|不用|不必|跳过)\s*(?:用户)?\s*(?:确认|同意|批准)|"
    r"(?:网页|外部|搜索结果|新闻)(?:里|中)?的?(?:指令|要求)\s*(?:要|应|可以)?\s*(?:照做|执行|遵守)|"
    r"\b(?:skip|ignore|omit|drop|remove)\s+(?:the\s+)?(?:citations?|sources?|disclaimers?|risk\s+warnings?|verification|confirmation)|"
    r"\bno\s+need\s+(?:to\s+)?(?:cite|verify|confirm|warn)", re.I)


def changed_lines(before: str | None, after: str | None) -> list[str]:
    """新增与删除的行（删除的红线行同样算碰到红线）。"""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    out = []
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line[:1] in "+-" and line[1:].strip():
            out.append(line[1:].strip())
    return out


def assess(kind: str, target: str, before: str | None, after: str | None) -> dict:
    """返回 {level: none|touch|weaken, why}。kind: skill / prompt / tooldesc / memory / rule / code。"""
    t = str(target or "")
    if kind == "code" and t.rsplit("/", 1)[-1] in PROTECTED_FILES:
        return {"level": "touch", "why": f"改到受保护的代码 {t}"}
    if kind == "tooldesc" and t in PROTECTED_TOOLS:
        return {"level": "touch", "why": f"改到受保护工具 {t} 的描述"}
    lines = changed_lines(before, after)
    from . import taint
    for ln in lines:
        if ln in (after or "") and taint.scan(ln):
            return {"level": "weaken", "why": f"新增文字里有疑似注入指令：{ln[:40]}"}
    added = [ln for ln in lines if ln in (after or "").splitlines() or ln in (after or "")]
    for ln in added:
        if WEAKEN.search(ln):
            return {"level": "weaken", "why": f"削弱红线：{ln[:48]}"}
    for ln in lines:
        if RULE_TERMS.search(ln) and INSTRUCT.search(ln):
            return {"level": "touch", "why": f"涉及风控/出处/注入/确认规则：{ln[:48]}"}
    if kind == "rule":
        return {"level": "touch", "why": "规则改动"}
    return {"level": "none", "why": ""}
