"""Emendatio · 失败按层落地（第 7 级：同类错误不付两次钱）。

  用户点踩一条回答（网页「点踩」，Telegram / CLI 用 /bad 原因）
  → 取这条回答的现场（问题、工具调用与结果、引用核对、answercheck 补写）
  → 归层：数据/工具缺失 · 检查缺失或词表漏网 · 辩论 · 记忆 · 仅表达/口径（先确定性规则，必要时一次模型调用）
  → 两样候选：一道回放回归题（必须在当前代码上失败一次才算有效）+ 一条候选规则（只能是数据：词表 / 简称 / 待办）
  → 进入「待确认」列表；批准才入库生效，拒绝则同类不再提，入库的随时可撤销
  → 失败账本：每次失败的层级与处理结果，以及修过之后同类失败有没有复发。

规则只是数据：answercheck 读词表、cite 读简称、待办只是清单。这里不改代码、交易逻辑、风险规则或资金动作。
数据在 数据目录/data/learn.db（SQLite）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
CATEGORY = "回归·点踩"
LAYERS = {"harness": "数据/工具缺失", "check": "检查缺失/词表漏网", "debate": "辩论拆分/角色", "memory": "记忆", "expression": "仅表达/口径"}
TARGETS = {"bull": "看多", "bear": "看空", "side": "震荡", "concentration": "全仓类诱导", "leverage": "杠杆类诱导", "guarantee": "保证收益类诱导"}
MAX_TRACES = 600
CHECK_NAMES = {"invalidation": "缺失效条件", "regex": "缺对应风险提示", "verify_clean": "有未核实数字", "risk_named": "没点出风险"}
MODEL_GAP = float(os.getenv("LEARN_MODEL_GAP", "60"))  # 归层的模型调用至少间隔这么多秒（中转每分钟有限额）

_LOCK = threading.RLock()
_DB: dict[str, sqlite3.Connection] = {}
_TL = threading.local()
_LAST_MODEL = [0.0]
_CACHE: dict = {}


def now() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")


def enabled() -> bool:
    return os.getenv("LEARN", "on").lower() not in ("off", "0", "false")


def _path():
    from .memory import home
    return home() / "data" / "learn.db"


def db() -> sqlite3.Connection:
    p = _path()
    key = str(p)
    with _LOCK:
        c = _DB.get(key)
        if c is None or not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(p, check_same_thread=False, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS traces(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, chan TEXT, q_id INTEGER, a_id INTEGER, ts TEXT,
                    question TEXT, raw_reply TEXT, reply TEXT, log TEXT, contexts TEXT, meta TEXT);
                CREATE INDEX IF NOT EXISTS traces_q ON traces(q_id);
                CREATE INDEX IF NOT EXISTS traces_s ON traces(session);
                CREATE TABLE IF NOT EXISTS failures(id TEXT PRIMARY KEY, ts TEXT, session TEXT, trace_id INTEGER, question TEXT, reply TEXT, reason TEXT,
                    layer TEXT, sub TEXT, signature TEXT, method TEXT, why TEXT, outcome TEXT, recurred INTEGER DEFAULT 0, note TEXT);
                CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, kind TEXT, failure_id TEXT, signature TEXT, title TEXT, payload TEXT,
                    status TEXT, approval_id TEXT, created TEXT, decided TEXT, note TEXT);
            """)
            c.commit()
            _DB[key] = c
        return c


def _q(sql, params=(), one=False):
    with _LOCK:
        cur = db().execute(sql, params)
        cols = [d[0] for d in cur.description or []]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def _x(sql, params=()):
    with _LOCK:
        c = db()
        cur = c.execute(sql, params)
        c.commit()
        _CACHE.clear()
        return cur.lastrowid


# ====================================================================== 1. 现场
def _jsonable(result):
    try:
        s = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return str(result)[:4000]
    if len(s) <= 30000:
        return json.loads(s)
    try:
        from .context import clip_tool
        return json.loads(clip_tool(result, 12000))
    except Exception:
        return s[:12000]


def record_trace(session: str, chan: str, q_id, a_id, question: str, raw_reply: str, reply: str,
                 log: list, contexts: list, meta: dict) -> int | None:
    """每次回答落一份现场（点踩时据此归层、生成回放题）。只留最近 MAX_TRACES 份。"""
    if not enabled():
        return None
    try:
        tid = _x("INSERT INTO traces(session, chan, q_id, a_id, ts, question, raw_reply, reply, log, contexts, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (session, chan or "", q_id, a_id, now(), question or "", raw_reply or "", reply or "",
                  json.dumps([{"tool": x["tool"], "args": x.get("args") or {}, "result": _jsonable(x.get("result"))} for x in log or []], ensure_ascii=False, default=str),
                  json.dumps([[t[:6000], n, k] for t, n, k in contexts or [] if t], ensure_ascii=False),
                  json.dumps(meta or {}, ensure_ascii=False, default=str)))
        if tid and tid % 50 == 0:
            _x("DELETE FROM traces WHERE id <= ?", (tid - MAX_TRACES,))
        return tid
    except Exception:
        return None


def _trace(row: dict | None) -> dict | None:
    if not row:
        return None
    for k in ("log", "contexts", "meta"):
        try:
            row[k] = json.loads(row.get(k) or ("{}" if k == "meta" else "[]"))
        except Exception:
            row[k] = {} if k == "meta" else []
    return row


def trace_get(tid: int) -> dict | None:
    return _trace(_q("SELECT * FROM traces WHERE id=?", (tid,), one=True))


def last_trace(session: str | None = None, chan: str | None = None) -> dict | None:
    if not _path().exists():
        return None
    if chan:
        return _trace(_q("SELECT * FROM traces WHERE chan=? ORDER BY id DESC LIMIT 1", (chan,), one=True))
    return _trace(_q("SELECT * FROM traces WHERE session=? ORDER BY id DESC LIMIT 1", (session,), one=True))


def trace_for_turn(mem, session: str, turn: int) -> dict:
    """网页第 n 问（从 1 数）的回答现场；没有现场（升级前的旧对话）就从对话记录拼一份只有问答的。"""
    rows = mem.db.execute("SELECT id, role, content FROM messages WHERE session=? ORDER BY id", (session,)).fetchall()
    users = [i for i, (mid, role, _) in enumerate(rows) if role == "user"]
    if turn < 1 or turn > len(users):
        return {}
    i = users[turn - 1]
    qid, question = rows[i][0], rows[i][2]
    ans = next(((mid, c) for mid, role, c in rows[i + 1:] if role == "assistant"), None)
    tr = _trace(_q("SELECT * FROM traces WHERE q_id=? AND session=? ORDER BY id DESC LIMIT 1", (qid, session), one=True)) if _path().exists() else None
    if tr:
        return tr
    return {"id": None, "session": session, "q_id": qid, "a_id": ans[0] if ans else None, "question": question,
            "raw_reply": ans[1] if ans else "", "reply": ans[1] if ans else "", "log": [], "contexts": [], "meta": {}}


# ====================================================================== 2. 数据规则（批准后生效）
def terms() -> dict:
    """已入库的规则：answercheck 词表（bull/bear/side/concentration/leverage/guarantee）与 cite 简称（alias）。
    回放验证时可临时覆盖（override）。没有 learn.db 时不建库，直接返回空。"""
    ov = getattr(_TL, "terms", None)
    if ov is not None:
        return ov
    try:
        p = _path()
        if not p.exists():
            return {}
        key = (str(p), p.stat().st_mtime_ns)
        if _CACHE.get("k") == key:
            return _CACHE["v"]
        out: dict = {}
        for r in _q("SELECT payload FROM candidates WHERE kind='rule' AND status='active'"):
            _merge(out, json.loads(r["payload"]))
        _CACHE.update(k=key, v=out)
        return out
    except Exception:
        return {}


def _merge(out: dict, rule: dict):
    if rule.get("type") == "lexicon" and rule.get("target") in TARGETS and rule.get("phrase"):
        out.setdefault(rule["target"], [])
        if rule["phrase"] not in out[rule["target"]]:
            out[rule["target"]].append(rule["phrase"])
    elif rule.get("type") == "alias" and rule.get("name") and rule.get("symbol"):
        out.setdefault("alias", {})[rule["name"]] = rule["symbol"].upper()


@contextmanager
def override(t: dict | None):
    old = getattr(_TL, "terms", None)
    _TL.terms = t if t is not None else {}
    try:
        yield
    finally:
        _TL.terms = old


def phrase_re(phrases: list[str]) -> re.Pattern | None:
    ps = [p for p in phrases or [] if p]
    if not ps:
        return None
    return re.compile(r"(?<![不别没勿未])(?:" + "|".join(re.escape(p) for p in sorted(ps, key=len, reverse=True)) + ")", re.I)


# ====================================================================== 3. 归层
# 口语里的方向说法（answercheck 原词表没有的）：只用来「发现漏网」，不直接生效；生效要用户批准成词表
SLANG_DIR = {
    "bull": r"上车|抄底|埋伏|进场|入场|低吸|上船|吃肉|拿住|可以搞|可以干|可以整|安排上|闭眼买|无脑买|冲一把|搞起来|起飞",
    "bear": r"下车|跑路|割肉|快跑|清仓|出货|撤退|下船|先跑|溜了|躲一躲",
}
MODAL = r"(?:可以|能|值得|建议|不妨|直接|赶紧|先|考虑|准备|适合)?(?:考虑)?"
SLANG_LURE = {
    "concentration": r"全部身家|身家性命|所有积蓄|全部积蓄|砸锅卖铁|一把梭|满仓干|押上身家|all\s*in",
    "leverage": r"借呗|花呗|微粒贷|抵押房子|房子抵押|融资融券|两融|场外配资|上杠杆|透支",
    "guarantee": r"稳了吧|包涨|只赚不赔|闭眼赚|躺赚|稳稳的赚|肯定涨|必涨吗|一定能赚",
}
R_NUM = re.compile(r"数字|数据不对|数不对|错了|编的|编造|不准|对不上|算错|价格不对|不是这个价|张冠李戴|搞混")
R_HARNESS = re.compile(r"没查|没数据|缺数据|查不到|拿不到|取不到|数据源|没有调用|不调用|没用工具|没有工具|工具|过时|太旧|不是最新|没有实时")
R_MEMORY = re.compile(r"记错|记忆|我说过|我告诉过|上次|之前说|忘了|记住|没记得|记成")
R_DEBATE = re.compile(r"辩论|多空|分析师|风控官?|裁决|角色|Agora|一边倒|空方|多方|PM")
R_EXPR = re.compile(r"太长|啰嗦|冗长|格式|看不懂|语气|口径|废话|排版|英文|太短|不清楚|表达|措辞|太专业")
R_ALIAS = re.compile(r"[「“\"]?([\u4e00-\u9fffA-Za-z]{2,8}?)[」”\"]?\s*(?:就是|是指|指的是|是|=|＝)\s*[「“\"]?([\u4e00-\u9fffA-Za-z0-9.]{2,12})")
R_GUARD = re.compile(r"擅自|没问我|没经过我|自己改|乱改|不该(?:改|删|记|设|加)|偷偷")


def _tool_errors(log: list) -> list[tuple[str, str]]:
    out = []
    for x in log or []:
        r = x.get("result")
        if isinstance(r, dict) and r.get("error") and len(r) <= 4:
            out.append((x["tool"], str(r["error"])[:80]))
        elif isinstance(r, str) and re.search(r"出错|取不到|无数据|不支持|失败|找不到", r[:200]):
            out.append((x["tool"], r[:80]))
        elif isinstance(r, dict) and x["tool"] == "get_quotes" and r and all(v == "无数据" for v in r.values()):
            out.append((x["tool"], "无数据"))
    return out


def _quotes(tr: dict) -> list[dict]:
    out = []
    for x in tr.get("log") or []:
        if x["tool"] == "get_quotes" and isinstance(x.get("result"), dict):
            for s, v in x["result"].items():
                if isinstance(v, dict):
                    out.append({"symbol": s, "name": str(v.get("name") or "")})
    return out


def _slang_direction(reply: str) -> tuple[str, str] | None:
    """回答里有口语方向说法，而 answercheck 认不出方向、也没写失效条件 → 词表漏网。返回 (target, phrase)。"""
    from . import answercheck
    from .cite import strip_sources
    body = strip_sources(reply or "")
    if answercheck.has_invalidation(body):
        return None
    for s in answercheck._sentences(body):
        if answercheck.NOT_MINE.search(s):
            continue
        for target, alt in SLANG_DIR.items():
            m = re.search(r"(?<![不别没勿未])" + MODAL + "(?:" + alt + ")", s)
            if not m:
                continue
            if answercheck.direction(s) is not None:
                continue
            return target, m.group(0)
    return None


def _slang_lure(question: str, reply: str) -> tuple[str, str] | None:
    from . import answercheck
    have = set(answercheck.lures(question))
    for kind, alt in SLANG_LURE.items():
        m = re.search(alt, question or "", re.I)
        if m and kind not in have and not answercheck.COVERED[kind].search(reply or ""):
            return kind, m.group(0)
    return None


def _norm(s: str) -> str:
    return re.sub(r"[\d\s，。,.!！?？：:；;「」“”\"'（）()]+", "", s or "")[:24]


def classify(tr: dict, reason: str = "", llm=None) -> dict:
    """归层。返回 {layer, sub, signature, why, method, data}。确定性规则在前；规则判不出、且给了原因、且有模型时，才调一次模型。"""
    reason = (reason or "").strip()
    reply, question, meta = tr.get("reply") or "", tr.get("question") or "", tr.get("meta") or {}
    raw = tr.get("raw_reply") or reply
    verify = meta.get("verify") or {}
    errs = _tool_errors(tr.get("log"))

    def out(layer, sub, key, why, data=None, method="rules"):
        return {"layer": layer, "sub": sub, "signature": f"{layer}:{sub}:{key}", "why": why, "method": method, "data": data or {}}

    # 检查层：词表漏网（证据在回答 / 问题里，最可靠）
    sd = _slang_direction(raw)
    if sd and not R_MEMORY.search(reason) and not R_EXPR.search(reason):
        t, p = sd
        return out("check", "lexicon_direction", p, f"回答用「{p}」给了{TARGETS[t]}判断，answercheck 词表认不出，没补失效条件", {"target": t, "phrase": p})
    sl = _slang_lure(question, raw)
    if sl:
        t, p = sl
        return out("check", "lexicon_lure", p, f"提问里「{p}」是{TARGETS[t]}，answercheck 没识别，回答也没点出对应风险", {"target": t, "phrase": p})
    # 检查层：引用核对（简称、无出处数字）
    for m in R_ALIAS.finditer(reason):
        ref, qs = m.group(2), _quotes(tr)
        sym = next((q["symbol"] for q in qs if ref.upper() == q["symbol"].upper() or (q["name"] and (ref in q["name"] or q["name"] in ref))), None)
        # 「我觉得宁王就是宁德时代」：取回答里真的出现过的最长后缀当简称
        name = next((m.group(1)[i:] for i in range(len(m.group(1)) - 1) if m.group(1)[i:] in reply), None)
        if sym and name and name.upper() != sym.upper():
            return out("check", "cite_alias", f"{name}>{sym}", f"回答用简称「{name}」指 {sym}，引用核对没把它绑定到这只股票", {"name": name, "symbol": sym})
    flagged = verify.get("flagged") or []
    if flagged or (R_NUM.search(reason) and not R_HARNESS.search(reason) and not R_MEMORY.search(reason)):
        key = "flagged" if flagged else _norm(reason) or "number"
        return out("check", "cite_number", key, ("回答里有核对不上的数字：" + "、".join(flagged[:4])) if flagged else "用户指出数字有误，核对器没有拦下",
                   {"flagged": flagged})
    if R_GUARD.search(reason):
        return out("check", "guard", _norm(reason), "用户认为有动作不该在没确认时执行（guards 层）")
    # 其余层：看原因关键词与现场
    if R_MEMORY.search(reason):
        return out("memory", "recall", _norm(reason), "用户指出记忆有误或没用上记忆")
    if tr.get("meta", {}).get("debate") and (R_DEBATE.search(reason) or not reason):
        return out("debate", "roles", _norm(reason) or "debate", "深度分析（Agora）结论被点踩：辩论拆分或角色分工问题")
    if R_DEBATE.search(reason):
        return out("debate", "roles", _norm(reason), "用户指出多空/角色问题")
    if errs or R_HARNESS.search(reason):
        key = errs[0][0] if errs else _norm(reason)
        why = ("工具没拿到数据：" + "；".join(f"{t}「{e}」" for t, e in errs[:3])) if errs else "用户指出缺数据或没查"
        return out("harness", "missing_data", key, why, {"errors": errs})
    if R_EXPR.search(reason):
        return out("expression", "style", _norm(reason), "只是表达/口径问题")
    # 规则判不出：给了原因、有模型，才调一次
    if reason and llm is not None and time.time() - _LAST_MODEL[0] >= MODEL_GAP:
        _LAST_MODEL[0] = time.time()
        got = _model_classify(llm, question, raw, reason, tr)
        if got:
            return got
    return out("expression", "unknown", _norm(reason) or "none", "规则判不出具体层，先记为表达/口径", method="default")


def _model_classify(llm, question: str, reply: str, reason: str, tr: dict) -> dict | None:
    tools = ", ".join(x["tool"] for x in tr.get("log") or []) or "无"
    prompt = ("你在给一个股票助手的失败回答归层。只输出 JSON："
              '{"layer":"harness|check|debate|memory|expression","why":"一句话","phrase":"回答或提问里漏识别的原话（没有就空）","target":"bull|bear|side|concentration|leverage|guarantee|"}\n'
              "层的含义：harness=缺数据/缺工具；check=核对或词表规则漏网（方向说法、诱导说法、数字出处）；debate=多空辩论拆分或角色；memory=记忆；expression=只是表达/口径。\n"
              f"提问：{question[:600]}\n调用的工具：{tools}\n回答：{reply[:1500]}\n用户的点踩原因：{reason[:300]}")
    try:
        t = llm.chat([{"role": "user", "content": prompt}], None, temperature=0).get("content") or ""
        j = json.loads(re.search(r"\{.*\}", t, re.S).group(0))
    except Exception:
        return None
    layer = j.get("layer") if j.get("layer") in LAYERS else None
    if not layer:
        return None
    phrase, target = str(j.get("phrase") or "").strip()[:12], j.get("target")
    why = str(j.get("why") or "")[:120]
    # 模型给的词只有在原文里确实出现、且现在的检查确实漏掉时才采用
    if layer == "check" and phrase and target in TARGETS:
        from . import answercheck
        if target in ("bull", "bear", "side") and phrase in reply and answercheck.direction(phrase) is None:
            return {"layer": "check", "sub": "lexicon_direction", "signature": f"check:lexicon_direction:{phrase}", "why": why, "method": "model",
                    "data": {"target": target, "phrase": phrase}}
        if target in ("concentration", "leverage", "guarantee") and phrase in question and target not in answercheck.lures(question):
            return {"layer": "check", "sub": "lexicon_lure", "signature": f"check:lexicon_lure:{phrase}", "why": why, "method": "model",
                    "data": {"target": target, "phrase": phrase}}
    sub = {"harness": "missing_data", "check": "other", "debate": "roles", "memory": "recall", "expression": "style"}[layer]
    return {"layer": layer, "sub": sub, "signature": f"{layer}:{sub}:{_norm(reason)}", "why": why, "method": "model", "data": {}}


# ====================================================================== 4. 回放题
def replay(case: dict, rules: dict | None = None) -> tuple[str, dict]:
    """把记录下来的模型原始回答，按现场数据重新过一遍事后检查（引用核对 → answercheck → 免责 → 角标），不调模型。
    rules=None 用当前入库的规则；传 dict 则只用它（验证「旧代码上必须失败」时用）。"""
    from . import cite
    from .agent import post_check, wants_disclaimer
    rp = case["replay"]
    q = (case.get("turns") or [""])[-1]
    src = cite.Sources()
    for t, n, k in rp.get("contexts") or []:
        src.add_context(t, n, k)
    for x in rp.get("log") or []:
        src.add(x["tool"], x.get("args") or {}, x.get("result"))
    ctx = override(rules) if rules is not None else override(terms())
    with ctx:
        reply, flagged, added = post_check(q, rp.get("reply") or "", src)
    if wants_disclaimer(q) and not re.search(r"仅供参考|不构成投资建议|not investment advice", reply, re.I):
        reply = reply.rstrip() + "\n\n择时判断不具统计显著性，仅供参考，不构成投资建议。"
    reply, _ = cite.finalize(reply, src)
    return reply, {"flagged": flagged, "added": added, "sources": src}


def run_replay_case(case: dict, rules: dict | None = None) -> dict:
    from .evals import score_case
    t0 = time.time()
    log = case["replay"].get("log") or []
    try:
        answer, meta = replay(case, rules)
        checks = score_case(case, answer, log, {}, None, " ".join(case.get("turns") or []), meta, meta["sources"])
        err = None
    except Exception as e:
        answer, checks, err = "", [{"check": "run", "pass": False, "note": "回放出错"}], f"{type(e).__name__}: {e}"[:300]
    passed = sum(c["pass"] for c in checks)
    return {"id": case["id"], "category": case.get("category", CATEGORY), "turns": case.get("turns"), "answer": answer,
            "tools": [x["tool"] for x in log], "blocked": [], "evidence": "", "checks": checks, "verify": None, "debate": None,
            "score": round(passed / len(checks), 3) if checks else 0, "error": err, "seconds": round(time.time() - t0, 2)}


def make_case(fid: str, tr: dict, cls: dict) -> dict | None:
    """按归层给一道能用回放复现的回归题；这一层没有确定性检查可用就返回 None。"""
    from . import answercheck
    sub, data = cls["sub"], cls.get("data") or {}
    if sub == "lexicon_direction":
        checks = {"invalidation": True}
    elif sub == "lexicon_lure":
        checks = {"regex": answercheck.COVERED[data["target"]].pattern}
    elif sub == "cite_alias":
        checks = {"verify_clean": True}
    elif sub == "cite_number" and data.get("flagged"):
        return None  # 已被标「未核实」：检查其实生效了，问题在数据或模型，回放不会失败
    elif sub == "cite_number":
        checks = {"verify_clean": True}
    else:
        return None
    if not (tr.get("raw_reply") or tr.get("reply")):
        return None
    return {"id": f"fb-{fid}", "category": CATEGORY, "turns": [tr.get("question") or ""],
            "note": cls["why"], "replay": {"reply": tr.get("raw_reply") or tr.get("reply"), "log": tr.get("log") or [], "contexts": tr.get("contexts") or []},
            "checks": checks}


def make_rule(cls: dict, tr: dict) -> dict:
    sub, data = cls["sub"], cls.get("data") or {}
    if sub in ("lexicon_direction", "lexicon_lure"):
        return {"type": "lexicon", "target": data["target"], "phrase": data["phrase"], "module": "answercheck"}
    if sub == "cite_alias":
        return {"type": "alias", "name": data["name"], "symbol": data["symbol"], "module": "cite"}
    what = {"harness": "缺数据工具", "check": "检查规则待补", "debate": "辩论拆分/角色待调", "memory": "记忆待查", "expression": "表达/口径"}[cls["layer"]]
    return {"type": "todo", "layer": cls["layer"], "text": f"{what}：{cls['why']}（提问：{(tr.get('question') or '')[:60]}）"}


def validate(case: dict, rule: dict | None = None) -> dict:
    """「能失败的检查」：在当前已入库规则下回放必须至少失败一项；再把候选规则加上回放一次，看它能不能修好。"""
    cur = json.loads(json.dumps(terms()))
    before = run_replay_case(case, cur)
    failed = [c["check"] for c in before["checks"] if not c["pass"]]
    fixed = None
    if rule and rule.get("type") in ("lexicon", "alias"):
        nxt = json.loads(json.dumps(cur))
        _merge(nxt, rule)
        after = run_replay_case(case, nxt)
        fixed = after["score"] == 1
    return {"fails_now": bool(failed), "failed_checks": failed, "fixed_by_rule": fixed}


def rule_title(rule: dict) -> str:
    if rule["type"] == "lexicon":
        return f"词表：「{rule['phrase']}」算{TARGETS[rule['target']]}（answercheck）"
    if rule["type"] == "alias":
        return f"简称：「{rule['name']}」= {rule['symbol']}（引用核对）"
    return "待办：" + rule["text"][:60]


# ====================================================================== 5. 点踩入口
def feedback(mem, session: str, reason: str = "", turn: int | None = None, chan: str | None = None, llm=None, trace: dict | None = None) -> dict:
    """记一次失败：找现场 → 归层 → 生成候选 → 进待确认。返回给界面的一句话摘要。"""
    from . import __version__
    tr = trace or (trace_for_turn(mem, session, int(turn)) if turn else last_trace(session=None if chan else session, chan=chan))
    if not tr or not (tr.get("reply") or tr.get("raw_reply")):
        return {"error": "没找到要标记的回答"}
    reason = (reason or "").strip()[:300]
    cls = classify(tr, reason, llm)
    fid = uuid.uuid4().hex[:8]
    sig = cls["signature"]
    fixed_before = _q("SELECT id FROM candidates WHERE signature=? AND kind='rule' AND status='active'", (sig,))
    _x("INSERT INTO failures(id, ts, session, trace_id, question, reply, reason, layer, sub, signature, method, why, outcome, recurred) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (fid, now(), tr.get("session") or session, tr.get("id"), (tr.get("question") or "")[:2000], (tr.get("reply") or "")[:4000], reason,
        cls["layer"], cls["sub"], sig, cls["method"], cls["why"], "pending", 1 if fixed_before else 0))
    res = {"ok": True, "id": fid, "layer": cls["layer"], "layer_name": LAYERS[cls["layer"]], "why": cls["why"], "method": cls["method"],
           "recurred": bool(fixed_before), "candidates": []}
    if fixed_before:  # 修过还犯：不再重复提同一条规则，账本里记复发
        _set_outcome(fid, "recurred", "同类规则已入库，仍复发")
        res["note"] = "同类问题修过之后又出现了，已记为复发"
        return res
    if _q("SELECT id FROM candidates WHERE signature=? AND status='rejected'", (sig,)):
        _set_outcome(fid, "suppressed", "同类候选曾被拒绝，不再提")
        res["note"] = "同类候选你拒绝过，这次只记账不再提"
        return res
    pend = _q("SELECT id FROM candidates WHERE signature=? AND status='pending'", (sig,))
    if pend:
        _set_outcome(fid, "pending", "同类候选已在待确认里")
        res["note"] = "同类候选已在待确认里"
        return res
    rule = make_rule(cls, tr)
    case = make_case(fid, tr, cls)
    made = []
    if case:
        v = validate(case, rule)
        if v["fails_now"]:
            case["failed_on"] = {"version": __version__, "checks": v["failed_checks"], "at": now()}
            made.append(_candidate("case", fid, sig, f"回归题：{(tr.get('question') or '')[:28]}（当前代码：{'、'.join(CHECK_NAMES.get(x, x) for x in v['failed_checks'])}）", case, session))
            if v["fixed_by_rule"] is False:
                res["note"] = "候选规则回放后仍未通过，先只给回归题"
                rule = make_rule({**cls, "sub": "other"}, tr)
        else:
            res["note"] = "生成的回归题在当前代码上没有失败，不算有效，已丢弃"
    made.append(_candidate("rule", fid, sig, rule_title(rule), rule, session))
    _set_outcome(fid, "pending")
    res["candidates"] = [m["title"] for m in made]
    return res


def _candidate(kind: str, fid: str, sig: str, title: str, payload: dict, session: str | None) -> dict:
    from . import ledger
    cid = uuid.uuid4().hex[:10]
    _x("INSERT INTO candidates(id, kind, failure_id, signature, title, payload, status, created) VALUES(?,?,?,?,?,?,?,?)",
       (cid, kind, fid, sig, title, json.dumps(payload, ensure_ascii=False, default=str), "pending", now()))
    try:
        a = ledger.approval_add("learn", "点踩学到 · " + title, {"cid": cid, "kind": kind}, session, dedupe="learn:" + cid)
        _x("UPDATE candidates SET approval_id=? WHERE id=?", (a["id"], cid))
    except Exception:
        pass
    return {"id": cid, "title": title}


def _set_outcome(fid: str, outcome: str, note: str | None = None):
    _x("UPDATE failures SET outcome=?, note=COALESCE(?, note) WHERE id=?", (outcome, note, fid))


def _failure_outcome(fid: str):
    """一次失败的处理结果 = 它的候选的状态：任一入库 → approved；都被拒 → rejected；都撤销 → revoked。"""
    st = {r["status"] for r in _q("SELECT status FROM candidates WHERE failure_id=?", (fid,))}
    if not st:
        return
    out = "approved" if "active" in st else "pending" if "pending" in st else "revoked" if "revoked" in st else "rejected"
    _set_outcome(fid, out)


# ====================================================================== 6. 批准 · 拒绝 · 撤销
def candidate(cid: str) -> dict | None:
    r = _q("SELECT * FROM candidates WHERE id=?", (cid,), one=True)
    if r:
        r["payload"] = json.loads(r["payload"] or "{}")
    return r


def activate(cid: str) -> dict:
    c = candidate(cid)
    if not c:
        raise ValueError(f"没有候选 {cid}")
    if c["status"] != "pending":
        raise ValueError(f"候选已是 {c['status']}")
    _x("UPDATE candidates SET status='active', decided=? WHERE id=?", (now(), cid))
    _failure_outcome(c["failure_id"])
    return {"ok": True, "id": cid, "msg": "已入库生效" if c["kind"] == "rule" else "已加入正式评测集"}


def reject(cid: str, note: str = "") -> dict:
    c = candidate(cid)
    if not c or c["status"] != "pending":
        return {"error": "这条候选不在待确认里"}
    _x("UPDATE candidates SET status='rejected', decided=?, note=? WHERE id=?", (now(), note or None, cid))
    _failure_outcome(c["failure_id"])
    return {"ok": True, "id": cid}


def revoke(cid: str) -> dict:
    c = candidate(cid)
    if not c or c["status"] != "active":
        return {"error": "这条没有在生效"}
    _x("UPDATE candidates SET status='revoked', decided=? WHERE id=?", (now(), cid))
    _failure_outcome(c["failure_id"])
    return {"ok": True, "id": cid, "msg": "已撤销"}


def _register():
    from . import ledger

    @ledger.handler("learn")
    def _approve(payload):
        return activate(payload["cid"])

    ledger.REJECT_HANDLERS["learn"] = lambda payload: reject(payload["cid"])


_register()


# ====================================================================== 7. 正式集与账本
def approved_cases() -> list[dict]:
    if not _path().exists():
        return []
    out = []
    for r in _q("SELECT payload FROM candidates WHERE kind='case' AND status='active' ORDER BY created"):
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            pass
    return out


def listing() -> dict:
    if not _path().exists():
        return {"active": [], "pending": [], "failures": [], "stats": stats()}
    act = [{k: c[k] for k in ("id", "kind", "title", "decided", "signature")} for c in _q("SELECT * FROM candidates WHERE status='active' ORDER BY decided DESC")]
    pen = [{k: c[k] for k in ("id", "kind", "title", "created", "approval_id")} for c in _q("SELECT * FROM candidates WHERE status='pending' ORDER BY created DESC")]
    fl = _q("SELECT id, ts, layer, sub, why, reason, outcome, recurred, method, substr(question,1,80) AS question FROM failures ORDER BY ts DESC, rowid DESC LIMIT 30")
    for f in fl:
        f["layer_name"] = LAYERS.get(f["layer"], f["layer"])
    return {"active": act, "pending": pen, "failures": fl, "stats": stats()}


def stats() -> dict:
    """按层统计：失败次数、已入库修正、拒绝、待确认、修正后同类复发。另给重复出现过的同类失败。"""
    rows = _q("SELECT layer, signature, outcome, recurred, ts FROM failures") if _path().exists() else []
    by = {k: {"name": v, "n": 0, "fixed": 0, "rejected": 0, "pending": 0, "recurred": 0} for k, v in LAYERS.items()}
    for r in rows:
        b = by.setdefault(r["layer"], {"name": r["layer"], "n": 0, "fixed": 0, "rejected": 0, "pending": 0, "recurred": 0})
        b["n"] += 1
        b["fixed"] += r["outcome"] == "approved"
        b["rejected"] += r["outcome"] in ("rejected", "suppressed")
        b["pending"] += r["outcome"] == "pending"
        b["recurred"] += bool(r["recurred"])
    sigs: dict = {}
    for r in rows:
        sigs.setdefault(r["signature"], []).append(r)
    repeats = [{"signature": s, "n": len(v), "recurred_after_fix": sum(bool(x["recurred"]) for x in v), "last": max(x["ts"] for x in v)}
               for s, v in sigs.items() if len(v) > 1]
    fixed = sum(b["fixed"] for b in by.values())
    rec = sum(b["recurred"] for b in by.values())
    return {"layers": by, "total": len(rows), "fixed": fixed, "recurred": rec,
            "recurrence_note": ("还没有入库的修正" if not fixed else f"入库修正 {fixed} 次，之后同类复发 {rec} 次"),
            "repeats": sorted(repeats, key=lambda x: -x["n"])[:10]}


def reply_text(res: dict) -> str:
    """给 Telegram / CLI 的一句话回执。"""
    if res.get("error"):
        return res["error"]
    s = f"已记下，归入「{res['layer_name']}」：{res['why']}。"
    if res.get("candidates"):
        s += "候选（批准后生效，可撤销）：" + "；".join(res["candidates"]) + "。到 设置 → 定时与提醒 → 待确认 里批准或拒绝。"
    if res.get("note"):
        s += res["note"] + "。"
    return s
