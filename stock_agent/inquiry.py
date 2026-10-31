"""长程研究课题（Inquisitio）：给一个研究目标，Stoa 自己拆问题、逐个查证、随时补问题，直到达标或用完时长。

和核心功能的关系（全部复用，不另起一套）：
- 每个子问题都交给一个完整的 Agent 回合：同一套工具、投研技能、记忆图谱、数字引用核对、图卡。
- 研究笔记落盘（data/inquiries/<id>/state.json），不塞在对话上下文里，所以能跑几小时、能中断续跑。
- 收尾报告：方向性判断进判断账本（record_call，到期复盘）；结论一句话写进 MEMORY.md；
  可复用的研究路径存成技能；失效价位设成提醒（备注带课题号）。
- 常驻模式下课题会被「续研」：新财报出来、失效价位被触发、或到了复查周期，自动查新数据对照结论，
  结论被削弱/推翻就推送。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from .memory import Memory, home

CN_TZ = timezone(timedelta(hours=8))
LOCK = threading.RLock()
META_RE = re.compile(r"<<<META(.*?)(?:>>>|$)", re.S)
DEFAULT_MIN = 60
MAX_MIN = 8 * 60
REFLECT_EVERY = 3
MIN_USE = 0.6      # 用掉预算的 60% 之前，模型说「达标」也不收尾，先深挖
MAX_ERRORS = 3     # 连续出错几次就暂停（多半是网络或模型限流），而不是草草收尾
MAX_DEPTH = 2


def _now() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def root():
    p = home() / "data" / "inquiries"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file(iid: str):
    return root() / re.sub(r"[^a-z0-9]", "", iid.lower())[:16] / "state.json"


def load(iid: str) -> dict | None:
    f = _file(iid)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(st: dict):
    with LOCK:
        st["updated"] = _now()
        f = _file(st["id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(f)


def listing() -> list[dict]:
    out = []
    for f in sorted(root().glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(summary(st))
    return out


def summary(st: dict) -> dict:
    qs = st.get("questions") or []
    cur = next((q for q in qs if q["status"] == "doing"), None)
    fu = (st.get("followups") or [])[-1:] or [None]
    return {"id": st["id"], "goal": st["goal"], "status": st["status"], "symbols": st.get("symbols") or [],
            "created": st["created"], "updated": st.get("updated"), "minutes": st["budget"]["minutes"],
            "elapsed_s": int(st["used"]["seconds"] + (time.time() - st["_t0"] if st.get("_t0") and st["status"] in ("planning", "running", "synthesizing") else 0)),
            "done": sum(q["status"] == "done" for q in qs), "total": sum(q["status"] != "skipped" for q in qs),
            "current": cur["text"] if cur else None, "one_line": st.get("one_line"), "view": st.get("view"),
            "next_check": st.get("next_check"), "watch": (st.get("watch") or {}).get("on", False),
            "last_followup": fu[0], "error": st.get("error")}


def _log(st: dict, msg: str):
    st.setdefault("log", []).append({"ts": _now(), "msg": msg[:300]})
    st["log"] = st["log"][-200:]


def _meta(text: str) -> dict:
    ms = META_RE.findall(text or "")
    if not ms:
        return {}
    raw = re.sub(r"\[\d+\]", "", ms[-1]).strip()
    try:
        return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except Exception:
        return {}


def _json(text: str, default):
    t = re.sub(r"```(?:json)?", "", text or "")
    try:
        a, b = (t.index("{"), t.rindex("}") + 1) if isinstance(default, dict) else (t.index("["), t.rindex("]") + 1)
        return json.loads(t[a:b])
    except Exception:
        return default


# ---------------------------------------------------------------- 新建
def create(goal: str, minutes: int | None = None, every_days: int | None = None, mem: Memory | None = None, lang: str | None = None) -> dict:
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("研究目标不能为空")
    minutes = max(5, min(MAX_MIN, int(minutes or DEFAULT_MIN)))
    iid = uuid.uuid4().hex[:8]
    st = {"id": iid, "goal": goal[:500], "lang": "en" if lang == "en" else "zh", "created": _now(), "status": "queued", "symbols": [], "criteria": "",
          "budget": {"minutes": minutes, "max_questions": max(6, min(60, minutes // 3))},
          "used": {"seconds": 0, "agent_turns": 0},
          "questions": [], "one_line": None, "view": None, "report": None, "tracking": [], "alerts": [],
          "watch": {"on": True, "every_days": 7 if every_days is None else max(0, int(every_days))},
          "next_check": None, "last_run": None, "followups": [], "log": []}
    save(st)
    return st


def parse_minutes(text: str) -> tuple[str, int | None]:
    """「/research 茅台还能不能涨 2小时」→ (目标, 120)。"""
    m = re.search(r"\s*(\d+(?:\.\d+)?)\s*(小时|h|hr|hours?|分钟|min|m)\s*$", text or "", re.I)
    if not m:
        return (text or "").strip(), None
    n = float(m.group(1))
    mins = int(n * 60) if m.group(2).lower() in ("小时", "h", "hr", "hour", "hours") else int(n)
    return text[:m.start()].strip(), mins


# ---------------------------------------------------------------- 模型
def _llm():
    if os.getenv("STOA_MOCK_LLM") == "1":
        from .evals import MockLLM
        return MockLLM()
    from .agent import load_cfg
    from .providers import Router
    return Router(load_cfg().get("llm", {}))


EN_HINT = "\n\n（界面语言是英文：JSON 里给人看的文字——子问题 text/why、criteria、one_line、理由——一律用英文写；JSON 键名和 view 等枚举值保持原样。）"


def _chat(llm, prompt: str, lang: str | None = None) -> str:
    if lang == "en":
        prompt += EN_HINT
    msg = llm.chat([{"role": "user", "content": prompt}], None)
    return msg.get("content") or ""


def _known() -> dict:
    from .agent import load_cfg
    return {w["symbol"]: w.get("name", "") for w in load_cfg().get("watchlist") or []}


def plan(st: dict, mem: Memory, llm) -> None:
    from . import debate, graph, skills
    sym = debate.resolve_symbol(st["goal"], _known())
    ctx = ""
    if sym:
        try:
            r = graph.recall(sym, mem)
            ctx = json.dumps(r, ensure_ascii=False, default=str)[:2500]
        except Exception:
            ctx = ""
    prompt = (f"[INQ:PLAN]\n你是 Stoa 的研究主管。用户给了一个长程研究目标，请把它拆成可以逐个查证的子问题。\n\n"
              f"研究目标：{st['goal']}\n时长预算：{st['budget']['minutes']} 分钟（每个子问题约 2–3 分钟，含多次工具调用；时间用不完会继续追加深挖问题）\n\n"
              f"用户画像与笔记：\n{mem.snapshot()[:2500]}\n\n记忆图谱里与目标相关的内容：\n{ctx or '（无）'}\n\n{skills.index()}\n\n"
              "要求：\n- 先识别标的（Yahoo 格式代码：600519.SS、0700.HK、AAPL）；主题类目标可以没有标的或有多只。\n"
              "- 写一句「成功标准」：回答到什么程度算研究完成。\n" f"- 子问题 {_plan_n(st)} 个，" "按先后顺序：先事实与数据，再驱动因素和分歧，再估值与风险，最后才是结论；"
              "每个子问题要具体、可查证，注明为什么要问、建议用哪些工具或技能。\n"
              '只输出 JSON：{"symbols":["..."],"criteria":"...","questions":[{"text":"...","why":"...","tools":"..."}]}')
    j = _json(_chat(llm, prompt, st.get("lang")), {})
    qs = [q for q in (j.get("questions") or []) if isinstance(q, dict) and str(q.get("text") or "").strip()]
    if not qs:  # 模型没给出可用计划：用通用骨架，保证能往下跑
        qs = [{"text": f"{st['goal']}：先把相关公司/行业的基本事实和最新数据查清楚", "why": "打底"},
              {"text": f"{st['goal']}：核心驱动因素和市场分歧是什么", "why": "找到关键变量"},
              {"text": f"{st['goal']}：估值与风险怎么看", "why": "量化"},
              {"text": f"{st['goal']}：最新公告、新闻、监管动向有没有改变判断", "why": "查证时效"}]
    syms = [s for s in (j.get("symbols") or []) if isinstance(s, str)][:6] or ([sym] if sym else [])
    st["symbols"] = syms
    st["criteria"] = str(j.get("criteria") or "能给出带证据的结论、关键数字、主要风险和跟踪指标")[:300]
    for q in qs[:12]:
        _add_q(st, q, depth=0)
    _log(st, f"拆成 {len(st['questions'])} 个子问题")


def _plan_n(st: dict) -> str:
    m = st["budget"]["minutes"]
    lo = max(4, min(8, m // 10))
    return f"{lo}–{min(12, lo + 4)}"


def _transient(e: Exception) -> bool:
    t = f"{type(e).__name__} {e}".lower()
    return any(k in t for k in ("429", "rate", "timeout", "timed out", "connection", "temporarily", "502", "503", "504", "overload"))


class SawRepeated(Exception):
    """同一个 SAW（看到的失败现象）连续出现两次：再试也是同样的结果，停下上报。"""

    def __init__(self, fb: dict):
        super().__init__(fb.get("saw"))
        self.fb = fb


def _saw(e: Exception) -> str:
    """失败现象归一：去掉数字、十六进制 id、多余空白，同一类错误得到同一个 SAW。"""
    t = f"{type(e).__name__}: {e}"
    t = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", t, flags=re.I)
    t = re.sub(r"\d+(?:\.\d+)?", "#", t)
    return re.sub(r"\s+", " ", t).strip()[:160]


def _feedback(st: dict, q: dict, attempt: int, e: Exception) -> tuple[dict, bool]:
    """重试前把失败写成结构化反馈 EXPECTED / SAW / DEAD ENDS 进状态；返回 (反馈, 是否与上一次失败同一个 SAW)。"""
    saw = _saw(e)
    hist = st.setdefault("feedback", [])
    dead = list(hist[-1]["dead_ends"]) if hist and hist[-1]["q"] == q["id"] else []
    dead.append(f"{q['id']} 第 {attempt + 1} 次：{'按原问题直接查' if attempt == 0 else '等待后按原问题重试'} → {saw[:60]}")
    fb = {"q": q["id"], "attempt": attempt + 1, "at": _now(),
          "expected": f"{q['id']} 产出一份带出处的研究笔记（{q['text'][:80]}）", "saw": saw, "dead_ends": dead[-6:]}
    repeated = st.get("last_saw") == saw
    st["last_saw"] = saw
    hist.append(fb)
    st["feedback"] = hist[-30:]
    q["feedback"] = fb
    return fb, repeated


def _feedback_text(fb: dict | None) -> str:
    if not fb:
        return ""
    return ("\n\n上次尝试失败的反馈（换一个做法，别重复死路）：\nEXPECTED：" + fb["expected"] + "\nSAW：" + fb["saw"] +
            "\nDEAD ENDS：" + "；".join(fb["dead_ends"]))


def _add_q(st: dict, q: dict, depth: int, parent: str | None = None) -> bool:
    text = str(q.get("text") or "").strip()[:300]
    if not text:
        return False
    norm = re.sub(r"\W", "", text)
    for o in st["questions"]:
        a = re.sub(r"\W", "", o["text"])
        if norm == a or (len(norm) > 8 and (norm in a or a in norm)):
            return False
    if sum(x["status"] != "skipped" for x in st["questions"]) >= st["budget"]["max_questions"]:
        return False
    st["questions"].append({"id": f"q{len(st['questions']) + 1}", "text": text, "why": str(q.get("why") or "")[:200],
                            "tools": str(q.get("tools") or "")[:200], "depth": depth, "parent": parent, "status": "todo",
                            "answer": None, "strength": None, "sources": [], "steps": 0, "started": None, "finished": None, "secs": 0})
    return True


def _notes(st: dict, limit: int = 6000) -> str:
    out = []
    for q in st["questions"]:
        if q["status"] != "done":
            continue
        from .cite import strip_sources
        body = strip_sources(q["answer"] or "")
        body = re.sub(r"\[\[(?:cards|debate):[^\]]+\]\]", "", body).strip()
        out.append(f"### {q['id']} {q['text']}（证据{q.get('strength') or '中'}）\n{body}")
    s = "\n\n".join(out)
    return s if len(s) <= limit else _compact_notes(st, limit)


def _compact_notes(st: dict, limit: int) -> str:
    """笔记太长：每条只留开头一段（结论），总长不超过 limit。"""
    from .cite import strip_sources
    done = [q for q in st["questions"] if q["status"] == "done"]
    per = max(200, limit // max(1, len(done)))
    return "\n\n".join(f"### {q['id']} {q['text']}（证据{q.get('strength') or '中'}）\n{strip_sources(q['answer'] or '')[:per]}" for q in done)


def _agent(mem: Memory, session: str, stop: threading.Event | None, lang: str | None = None):
    from .agent import Agent
    ag = Agent(mem, channel="inquiry", session=session)
    ag.lang = lang or "zh"
    ag._stop = stop
    ag._no_debate = True  # 目标里有「该不该买」也不要被直接转去 Agora，按子问题查
    return ag


def research_step(st: dict, q: dict, mem: Memory, stop: threading.Event | None, on_event=None) -> None:
    prompt = (f"[长程研究课题 {st['id']}]\n总目标：{st['goal']}\n成功标准：{st['criteria']}\n"
              + (f"相关标的：{'、'.join(st['symbols'])}\n" if st["symbols"] else "")
              + f"\n已有研究笔记（前面子问题的结论，数字以其原始出处为准）：\n{_notes(st, 5000) or '（还没有）'}\n\n"
              f"本步子问题（{q['id']}）：{q['text']}\n为什么问：{q['why'] or '—'}" + (f"\n建议工具/技能：{q['tools']}" if q.get("tools") else "") + _feedback_text(q.get("feedback")) +
              "\n\n要求：只围绕本子问题查证，能用工具就用工具，有对口技能先 skill_view。写 300–900 字的研究笔记：先一句结论，再证据，"
              "最后写清楚还不确定的地方。数字带出处角标。本步不要调用 record_call、alert_set、skill_save（课题收尾时统一处理）。\n"
              "最后单独一行输出（系统会拿走，不给用户看）：\n"
              '<<<META {"strength":"强|中|弱","new_questions":[{"text":"值得追问的新问题","why":"..."}],"goal_met":false}>>>')
    ag = _agent(mem, f"inq-{st['id']}-{q['id']}", stop, st.get("lang"))
    ag.on_event = on_event
    t0 = time.time()
    reply = ag.ask(prompt)
    meta = _meta(getattr(ag, "last_tail", "") or reply)
    reply = META_RE.sub("", reply).strip()
    lm = getattr(ag, "last_meta", None) or {}
    if lm.get("stopped"):
        raise InterruptedError()
    q.update(answer=reply, strength=str(meta.get("strength") or "中")[:2], status="done", finished=_now(), secs=int(time.time() - t0),
             steps=len(lm.get("steps") or []),
             sources=[{k: s.get(k) for k in ("n", "name", "url", "as_of") if s.get(k) is not None} for s in (lm.get("sources") or [])][:30])
    st["used"]["agent_turns"] += 1
    if q["depth"] < MAX_DEPTH:
        added = sum(_add_q(st, nq, q["depth"] + 1, q["id"]) for nq in (meta.get("new_questions") or [])[:3] if isinstance(nq, dict))
        if added:
            _log(st, f"{q['id']} 追加 {added} 个新问题")
    if meta.get("goal_met"):
        st["_goal_met_hint"] = True


def deepen(st: dict, llm) -> int:
    """子问题做完了但时间还多：找证据弱的地方、反方观点、交叉验证，追加新问题。返回追加个数。"""
    left = max(0, int(st["budget"]["minutes"] - st["used"]["seconds"] / 60))
    weak = [q for q in st["questions"] if q["status"] == "done" and (q.get("strength") or "中") in ("弱", "中")]
    prompt = (f"[INQ:DEEPEN]\n研究目标：{st['goal']}\n成功标准：{st['criteria']}\n剩余时间约 {left} 分钟，还够再查几个问题。\n\n"
              f"已有笔记：\n{_notes(st, 5000)}\n\n证据偏弱的子问题：{'、'.join(q['id'] for q in weak) or '无'}\n\n"
              "现有子问题都做完了。请追加 2–4 个能让结论更可靠的新子问题，优先：① 补强证据弱或结论冲突的地方；② 反方视角——什么情况下结论会错；"
              "③ 用另一个数据源或同业对比交叉验证关键数字；④ 还没覆盖到的关键变量。不要重复已有问题。"
              '只输出 JSON：{"add":[{"text":"...","why":"..."}]}，确实没有值得查的就给空数组。')
    j = _json(_chat(llm, prompt, st.get("lang")), {})
    n = sum(_add_q(st, a, 1) for a in (j.get("add") or [])[:4] if isinstance(a, dict))
    _log(st, f"子问题做完、还剩约 {left} 分钟，追加 {n} 个深挖问题" if n else "没有值得追加的问题，开始收尾")
    return n


def reflect(st: dict, llm) -> None:
    """每做完几个子问题：看看剩下的问题还值不值得做、要不要换顺序、是否已经达标。"""
    todo = [q for q in st["questions"] if q["status"] == "todo"]
    if not todo:
        return
    prompt = (f"[INQ:REFLECT]\n研究目标：{st['goal']}\n成功标准：{st['criteria']}\n"
              f"剩余时间约 {max(0, int(st['budget']['minutes'] - st['used']['seconds'] / 60))} 分钟。\n\n已有笔记：\n{_notes(st, 4000)}\n\n"
              "待做的子问题：\n" + "\n".join(f"- {q['id']}：{q['text']}" for q in todo) +
              "\n\n请判断：哪些待做问题已被前面的笔记回答或价值不大（drop），剩下的最优顺序（order），是否还缺关键问题（add，最多 2 个），"
              '以及按成功标准是否已经可以收尾（goal_met）。只输出 JSON：{"order":["q3"],"drop":["q5"],"add":[{"text":"...","why":"..."}],"goal_met":false,"why":"一句话"}')
    j = _json(_chat(llm, prompt, st.get("lang")), {})
    ids = {q["id"]: q for q in todo}
    for d in j.get("drop") or []:
        if d in ids:
            ids[d]["status"] = "skipped"
    for a in (j.get("add") or [])[:2]:
        if isinstance(a, dict):
            _add_q(st, a, 1)
    order = [o for o in (j.get("order") or []) if o in ids and ids[o]["status"] == "todo"]
    if order:
        rank = {o: i for i, o in enumerate(order)}
        rest = [q for q in st["questions"] if q["status"] != "todo"]
        todo2 = sorted([q for q in st["questions"] if q["status"] == "todo"], key=lambda q: rank.get(q["id"], 999))
        st["questions"] = rest + todo2
    st["_goal_met"] = bool(j.get("goal_met"))
    _log(st, "复盘：" + str(j.get("why") or ("可以收尾" if st["_goal_met"] else "继续"))[:120])


def synthesize(st: dict, mem: Memory, llm, stop: threading.Event | None, on_event=None) -> None:
    from . import alerts
    done = [q for q in st["questions"] if q["status"] == "done"]
    open_q = [q["text"] for q in st["questions"] if q["status"] == "todo"]
    srcs = []
    for q in done:
        for s in q.get("sources") or []:
            srcs.append(f"{q['id']}：{s.get('name')}" + (f"（{s.get('as_of')}）" if s.get("as_of") else ""))
    prompt = (f"[长程研究课题 {st['id']} · 收尾]\n研究目标：{st['goal']}\n成功标准：{st['criteria']}\n\n"
              f"各子问题的研究笔记：\n{_notes(st, 12000)}\n\n笔记引用过的原始来源：\n" + "\n".join(srcs[:60]) +
              (f"\n\n没来得及做的问题：\n" + "\n".join(f"- {t}" for t in open_q) if open_q else "") +
              "\n\n请写最终研究报告（Markdown）：\n1. 一句话结论（直接回答研究目标）\n2. 核心论据（每条注明来自哪个子问题，如「见 q2」）\n"
              "3. 反方观点与主要风险\n4. 关键数字与估值（只用笔记里有出处的数字）\n5. 仍未解决的问题与数据缺口\n6. 跟踪指标与失效条件（具体到指标和阈值）\n"
              "如果结论有明确方向（看涨/看跌/震荡）和期限，调用 record_call 记账，reason 开头写「课题" + st["id"] + "：」。"
              "如果这套研究路径以后可以复用到别的标的，用 skill_save 保存或更新一个技能（只写步骤，不写具体数字）。不要调用 alert_set（系统会按失效条件统一设）。")
    ag = _agent(mem, f"inq-{st['id']}-final", stop, st.get("lang"))
    ag.on_event = on_event
    reply = ag.ask(prompt)
    if (getattr(ag, "last_meta", None) or {}).get("stopped"):
        raise InterruptedError()
    reply = META_RE.sub("", reply).strip()
    st["report"] = reply
    st["used"]["agent_turns"] += 1
    (_file(st["id"]).parent / "report.md").write_text(f"# 研究课题：{st['goal']}\n\n{reply}\n", encoding="utf-8")
    # 结构化抽取（不经过引用核对，免得 JSON 被打角标）
    from .cite import strip_sources
    j = _json(_chat(llm, "[INQ:EXTRACT]\n从下面的研究报告里抽取结构化信息。只输出 JSON："
                    '{"one_line":"一句话结论（40字内）","view":"看涨|看跌|震荡|无","horizon_days":90,'
                    '"tracking":[{"symbol":"代码或空","metric":"指标","condition":"什么情况下要警惕"}],'
                    '"price_alerts":[{"symbol":"Yahoo代码","kind":"below|above","value":数字,"note":"触发意味着什么"}]}\n'
                    "price_alerts 只收报告里明确写出的价格失效线（没有就空数组）。\n\n" + strip_sources(reply)[:8000], st.get("lang")), {})
    st["one_line"] = str(j.get("one_line") or "")[:120] or None
    st["view"] = j.get("view") if j.get("view") in ("看涨", "看跌", "震荡") else None
    st["tracking"] = [t for t in (j.get("tracking") or []) if isinstance(t, dict)][:10]
    for a in (j.get("price_alerts") or [])[:4]:
        try:
            if a.get("kind") in ("below", "above") and a.get("symbol") and float(a.get("value")) > 0:
                r = alerts.add(a["symbol"], a["kind"], float(a["value"]), "", note=f"课题{st['id']}：{str(a.get('note') or '失效条件')[:80]}")
                if (r.get("rule") or {}).get("id"):
                    st["alerts"].append(r["rule"]["id"])
        except Exception:
            pass
    if st["one_line"]:
        try:
            mem.add("memory", f"研究课题{st['id']}「{st['goal'][:40]}」结论：{st['one_line']}", source="agent")
        except Exception:
            pass


# ---------------------------------------------------------------- 主循环
def run(iid: str, mem: Memory | None = None, stop: threading.Event | None = None, on_event=None, log=print) -> dict:
    mem = mem or Memory()
    st = load(iid)
    if not st:
        raise ValueError(f"没有课题 {iid}")
    llm = _llm()
    stop = stop or threading.Event()
    from .providers import Router
    if isinstance(llm, Router):
        llm.stop, llm.on_event = stop, on_event
    st["_t0"] = time.time()
    st["error"] = None
    st["last_saw"] = None
    try:
        if not st["questions"]:
            st["status"] = "planning"
            save(st)
            plan(st, mem, llm)
        st["status"] = "running"
        save(st)
        since_reflect = 0
        errs, paused_err = 0, False
        while not stop.is_set():
            elapsed = st["used"]["seconds"] + time.time() - st["_t0"]
            if elapsed >= st["budget"]["minutes"] * 60:
                _log(st, "时长用完，开始收尾")
                break
            early = elapsed < st["budget"]["minutes"] * 60 * MIN_USE
            if st.get("_goal_met") and not early:
                _log(st, "已达成功标准，开始收尾")
                break
            q = next((x for x in st["questions"] if x["status"] == "todo"), None)
            if not q:
                if early or st["budget"]["minutes"] * 60 - elapsed > 300:
                    try:
                        if deepen(st, llm):
                            st.pop("_goal_met", None)
                            save(st)
                            continue
                    except Exception as e:
                        _log(st, f"追加问题失败：{str(e)[:80]}")
                break
            q["status"], q["started"] = "doing", _now()
            save(st)
            try:
                for attempt in range(3):
                    try:
                        research_step(st, q, mem, stop, on_event)
                        break
                    except InterruptedError:
                        raise
                    except Exception as e:
                        fb, repeated = _feedback(st, q, attempt, e)
                        save(st)
                        if repeated:  # 同一个 SAW 连续两次：再试也一样，停下上报
                            raise SawRepeated(fb)
                        if attempt == 2 or not _transient(e) or stop.is_set():
                            raise
                        _log(st, f"{q['id']} 暂时出错（{str(e)[:60]}），{30 * (attempt + 1)} 秒后重试")
                        stop.wait(30 * (attempt + 1))
                log(f"课题 {iid} {q['id']} 完成（{q['secs']}s）")
                errs = 0
                st["last_saw"] = None
                q.pop("feedback", None)
            except InterruptedError:
                q["status"] = "todo"
                break
            except SawRepeated as sr:
                fb = sr.fb
                q.update(status="todo", answer=None)
                for x in st["questions"]:
                    if x["status"] == "error":
                        x["status"] = "todo"
                st["stalled"] = fb
                st["error"] = ("同一个失败连续出现两次，已停止重试并上报。EXPECTED：" + fb["expected"] + "；SAW：" + fb["saw"] +
                               "；DEAD ENDS：" + "；".join(fb["dead_ends"]))
                _log(st, f"{q['id']} 同一失败连续两次（{fb['saw'][:60]}），停止重试")
                _announce(st, "研究暂停", f"同一个失败连续出现两次（{fb['saw'][:80]}），已停止重试，等你处理后可继续。")
                paused_err = True
                break
            except Exception as e:
                q.update(status="error", answer=f"出错：{str(e)[:200]}", finished=_now())
                _log(st, f"{q['id']} 出错：{str(e)[:120]}")
                errs += 1
                if errs >= MAX_ERRORS:
                    for x in st["questions"]:
                        if x["status"] == "error":
                            x["status"] = "todo"
                    st["error"] = f"连续 {errs} 个子问题出错，多半是网络或模型限流：{str(e)[:120]}。可以稍后继续。"
                    _log(st, "连续出错，已暂停")
                    paused_err = True
                    break
            since_reflect += 1
            st["used"]["seconds"] += time.time() - st["_t0"]
            st["_t0"] = time.time()
            if st.pop("_goal_met_hint", False) or since_reflect >= REFLECT_EVERY:
                since_reflect = 0
                try:
                    reflect(st, llm)
                except Exception as e:
                    _log(st, f"复盘失败：{str(e)[:80]}")
            save(st)
        if stop.is_set() or paused_err:
            st["status"] = "paused"
            if not paused_err:
                _log(st, "已暂停，可以继续")
        elif not any(q["status"] == "done" for q in st["questions"]):
            st["status"] = "error"
            st["error"] = "没有一个子问题查成功"
        else:
            st["status"] = "synthesizing"
            save(st)
            try:
                synthesize(st, mem, llm, stop, on_event)
                st["status"] = "done"
                st["last_run"] = _now()
                _schedule_next(st)
                _announce(st, "研究完成", st.get("one_line") or st["goal"])
            except InterruptedError:
                st["status"] = "paused"
    except InterruptedError:
        st["status"] = "paused"
        _log(st, "已暂停，可以继续")
    except Exception as e:
        st["status"] = "error"
        st["error"] = str(e)[:300]
        log("课题出错", iid, e)
    finally:
        if st.get("_t0"):
            st["used"]["seconds"] += time.time() - st.pop("_t0")
        for q in st["questions"]:
            if q["status"] == "doing":
                q["status"] = "todo"
        st.pop("_goal_met", None)
        save(st)
    return st


def _schedule_next(st: dict):
    d = (st.get("watch") or {}).get("every_days") or 0
    st["next_check"] = (datetime.now(CN_TZ) + timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S") if d and st["watch"].get("on") else None


def _announce(st: dict, head: str, text: str, push: bool = True):
    from . import alerts, notify
    alerts.add_events([{"time": _now(), "kind": "inquiry", "inquiry": st["id"], "title": f"{head}：{text}"[:200]}])
    if push:
        try:
            notify.push(f"Stoa 研究课题 · {head}", f"**{st['goal']}**\n\n{text}\n\n仅供参考，不构成投资建议。")
        except Exception:
            pass


# ---------------------------------------------------------------- 续研
def triggers(st: dict, today: datetime | None = None) -> list[str]:
    """该不该续研：失效价位触发 / 相关标的出了新财报研报 / 到了复查周期。"""
    if st.get("status") != "done" or not (st.get("watch") or {}).get("on"):
        return []
    now = today or datetime.now(CN_TZ)
    last = st.get("last_run") or st["created"]
    out = []
    try:
        from . import alerts
        for e in alerts.load()["events"]:
            if e.get("kind") == "price" and str(e.get("note") or "").startswith(f"课题{st['id']}") and e.get("time", "") > last:
                out.append(f"失效条件触发：{e['title']}")
    except Exception:
        pass
    try:
        from . import earnings
        rep = earnings.load().get("reported") or {}
        for k in rep:
            sym, _, d = k.partition("|")
            if sym in (st.get("symbols") or []) and d > last[:10]:
                out.append(f"{sym} 出了新财报（{d}）")
    except Exception:
        pass
    if st.get("next_check") and st["next_check"] <= now.strftime("%Y-%m-%d %H:%M:%S"):
        out.append("到了定期复查时间")
    return out


def followup(iid: str, reasons: list[str] | None = None, mem: Memory | None = None, stop: threading.Event | None = None, on_event=None) -> dict:
    mem = mem or Memory()
    st = load(iid)
    if not st:
        raise ValueError(f"没有课题 {iid}")
    reasons = reasons or ["用户手动续研"]
    prev = st.get("report") or ""
    from .cite import strip_sources
    track = "\n".join(f"- {t.get('symbol') or ''} {t.get('metric')}：{t.get('condition')}" for t in st.get("tracking") or [])
    last = (st.get("followups") or [{}])[-1]
    prompt = (f"[研究课题 {st['id']} · 续研]\n研究目标：{st['goal']}\n上次结论（{st.get('last_run')}）：{st.get('one_line') or '—'}"
              + (f"；上次续研判断：{last.get('verdict')}，{last.get('one_line')}" if last.get("verdict") else "") +
              f"\n触发原因：{'；'.join(reasons)}\n跟踪指标与失效条件：\n{track or '（无）'}\n\n上次报告摘要：\n{strip_sources(prev)[:3000]}\n\n"
              f"请查自上次以来的新数据（行情、公告、财报、新闻、一致预期），逐条对照跟踪指标和失效条件，判断原结论：维持 / 加强 / 削弱 / 推翻。"
              "写 300–800 字：先一句判断，再逐条对照（数字带出处），最后说接下来盯什么。原结论被推翻且有方向判断时，调用 record_call 记一笔新的判断（reason 开头写「课题"
              + st["id"] + "续研：」）。\n最后单独一行：<<<META {\"verdict\":\"维持|加强|削弱|推翻\",\"one_line\":\"40字内\"}>>>")
    ag = _agent(mem, f"inq-{st['id']}-fu{len(st.get('followups') or []) + 1}", stop, st.get("lang"))
    ag.on_event = on_event
    t0 = time.time()
    reply = ag.ask(prompt)
    if (stop and stop.is_set()) or (getattr(ag, "last_meta", None) or {}).get("stopped") or (getattr(ag, "last_meta", None) or {}).get("timeout"):
        st["status"] = "paused"
        save(st)
        return st
    meta = _meta(getattr(ag, "last_tail", "") or reply)
    reply = META_RE.sub("", reply).strip()
    verdict = meta.get("verdict") if meta.get("verdict") in ("维持", "加强", "削弱", "推翻") else "维持"
    fu = {"ts": _now(), "reasons": reasons, "verdict": verdict, "one_line": str(meta.get("one_line") or "")[:120], "text": reply,
          "secs": int(time.time() - t0), "sources": [{k: s.get(k) for k in ("n", "name", "url", "as_of") if s.get(k) is not None}
                                                    for s in ((getattr(ag, "last_meta", None) or {}).get("sources") or [])][:30]}
    st = load(iid) or st
    st.setdefault("followups", []).append(fu)
    st["last_run"] = _now()
    st["used"]["agent_turns"] += 1
    _schedule_next(st)
    save(st)
    _announce(st, f"续研 · {verdict}", fu["one_line"] or "；".join(reasons), push=verdict in ("削弱", "推翻"))
    if verdict in ("削弱", "推翻"):
        try:
            mem.add("memory", f"研究课题{st['id']}续研：结论被{verdict}——{fu['one_line']}", source="agent")
        except Exception:
            pass
    return st


# ---------------------------------------------------------------- 后台执行（一次只跑一个，免得撞上模型每分钟请求上限）
class Runner:
    def __init__(self):
        self.q: list[tuple[str, str, list]] = []
        self.cur: str | None = None
        self.stop = threading.Event()
        self.cv = threading.Condition()
        self.th: threading.Thread | None = None
        self.log = print
        self.mem: Memory | None = None

    def submit(self, iid: str, kind: str = "run", reasons: list | None = None):
        with self.cv:
            if iid == self.cur or any(x[0] == iid for x in self.q):
                return False
            self.q.append((iid, kind, reasons or []))
            self.cv.notify()
        self._ensure()
        return True

    def cancel(self, iid: str) -> bool:
        with self.cv:
            n = len(self.q)
            self.q = [x for x in self.q if x[0] != iid]
            if n != len(self.q):
                st = load(iid)
                if st and st["status"] == "queued":
                    st["status"] = "paused"
                    save(st)
                return True
        if self.cur == iid:
            self.stop.set()
            return True
        return False

    def _ensure(self):
        if self.th and self.th.is_alive():
            return
        self.th = threading.Thread(target=self._loop, daemon=True, name="inquiry")
        self.th.start()

    def _loop(self):
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                iid, kind, reasons = self.q.pop(0)
                self.cur = iid
                self.stop = threading.Event()
            try:
                if kind == "followup":
                    followup(iid, reasons, self.mem, self.stop)
                else:
                    run(iid, self.mem, self.stop, log=self.log)
            except Exception as e:
                self.log("课题执行出错", iid, e)
            finally:
                self.cur = None


RUNNER = Runner()


def start(goal: str, minutes: int | None = None, every_days: int | None = None, mem: Memory | None = None, lang: str | None = None) -> dict:
    st = create(goal, minutes, every_days, lang=lang)
    if mem is not None:
        RUNNER.mem = mem
    RUNNER.submit(st["id"])
    return st


def resume(iid: str) -> bool:
    st = load(iid)
    if not st or st["status"] in ("done",):
        return False
    st["status"] = "queued"
    save(st)
    return RUNNER.submit(iid)


def set_watch(iid: str, on: bool, every_days: int | None = None) -> dict | None:
    st = load(iid)
    if not st:
        return None
    st.setdefault("watch", {})["on"] = bool(on)
    if every_days is not None:
        st["watch"]["every_days"] = max(0, int(every_days))
    _schedule_next(st)
    save(st)
    return st


def delete(iid: str) -> bool:
    import shutil
    RUNNER.cancel(iid)
    d = _file(iid).parent
    if d.exists():
        from . import alerts
        st = load(iid) or {}
        for rid in st.get("alerts") or []:
            try:
                alerts.remove(rid)
            except Exception:
                pass
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def loop(log=print, mem: Memory | None = None):
    """常驻：程序重启时接着跑没跑完的课题；每 10 分钟看一次哪些课题该续研。"""
    RUNNER.log, RUNNER.mem = log, mem
    time.sleep(20)
    for s in listing():
        if s["status"] in ("queued", "planning", "running", "synthesizing"):
            log("接着跑课题", s["id"], s["goal"][:30])
            RUNNER.submit(s["id"])
    while True:
        try:
            for s in listing():
                st = load(s["id"])
                why = triggers(st) if st else []
                if why and RUNNER.submit(st["id"], "followup", why):
                    log("课题续研", st["id"], "；".join(why)[:120])
        except Exception as e:
            log("课题续研检查出错", e)
        time.sleep(int(os.getenv("INQUIRY_CHECK_S", "600")))


def for_agent(action: str, args: dict, mem: Memory) -> dict:
    """对话里的工具入口：start_inquiry / inquiry_status。"""
    if action == "start":
        goal, mins = parse_minutes(str(args.get("goal") or ""))
        st = start(goal, args.get("minutes") or mins, args.get("every_days"), mem)
        return {"id": st["id"], "goal": st["goal"], "minutes": st["budget"]["minutes"],
                "msg": f"已开课题 {st['id']}，预算 {st['budget']['minutes']} 分钟，后台进行；进度在 Ledger → 研究课题，或输入 /research 查看。完成后结论会写进记忆和判断账本，并推送。"}
    if action == "resume":
        iid = str(args.get("id") or "").strip()
        if not iid:
            cand = [s for s in listing() if s["status"] in ("paused", "error", "queued")]
            if not cand:
                run = [s for s in listing() if s["status"] in ("planning", "running", "synthesizing")]
                return {"msg": f"课题 {run[0]['id']} 正在跑，不用续" if run else "没有可以继续的课题（都已完成）；要再深挖可以开新课题或对已完成课题续研。",
                        "inquiries": listing()[:5]}
            iid = cand[0]["id"]
        st = load(iid)
        if not st:
            return {"error": f"没有课题 {iid}"}
        if st["status"] == "done":
            RUNNER.mem = mem
            ok = RUNNER.submit(iid, "followup", ["用户在对话里要求继续"])
            return {"id": iid, "ok": ok, "msg": f"课题 {iid} 已完成，已安排续研"}
        extra = int(args.get("extra_minutes") or 0)
        if extra > 0:
            st["budget"]["minutes"] = min(MAX_MIN, st["budget"]["minutes"] + extra)
            st["budget"]["max_questions"] = max(st["budget"]["max_questions"], min(60, st["budget"]["minutes"] // 3))
            save(st)
        elif st["used"]["seconds"] >= st["budget"]["minutes"] * 60 - 60:
            st["budget"]["minutes"] = min(MAX_MIN, st["budget"]["minutes"] + 30)
            save(st)
        RUNNER.mem = mem
        ok = resume(iid)
        return {"id": iid, "ok": ok, "minutes": st["budget"]["minutes"], "msg": f"课题 {iid} 已接着跑（预算 {st['budget']['minutes']} 分钟）"}
    iid = str(args.get("id") or "").strip()
    if iid:
        st = load(iid)
        if not st:
            return {"error": f"没有课题 {iid}"}
        return {**summary(st), "questions": [{k: q[k] for k in ("id", "text", "status", "strength")} for q in st["questions"]],
                "report": (st.get("report") or "")[:3000], "followups": [{k: f[k] for k in ("ts", "verdict", "one_line")} for f in st.get("followups") or []]}
    return {"inquiries": listing()[:10]}


def main(argv=None):
    """python -m stock_agent research "目标" [--minutes 60] | list | show <id> | resume <id> | followup <id>"""
    import argparse
    from . import settings
    settings.load_env_file()
    argv = list(argv or [])
    if argv and argv[0] == "list":
        for s in listing():
            print(f"{s['id']}  {s['status']:<12} {s['done']}/{s['total']}  {s['goal'][:40]}  {s.get('one_line') or ''}")
        return
    if argv and argv[0] == "show" and len(argv) > 1:
        st = load(argv[1])
        print(json.dumps(summary(st), ensure_ascii=False, indent=1) if st else "没有这个课题")
        if st and st.get("report"):
            print("\n" + st["report"])
        return
    if argv and argv[0] in ("resume", "followup") and len(argv) > 1:
        st = run(argv[1]) if argv[0] == "resume" else followup(argv[1])
        print(json.dumps(summary(st), ensure_ascii=False, indent=1))
        return
    p = argparse.ArgumentParser(prog="stock_agent research")
    p.add_argument("goal")
    p.add_argument("--minutes", type=int, default=DEFAULT_MIN)
    p.add_argument("--every-days", type=int, default=7)
    a = p.parse_args(argv)
    st = create(a.goal, a.minutes, a.every_days)
    print(f"课题 {st['id']}：{st['goal']}（预算 {st['budget']['minutes']} 分钟）")
    st = run(st["id"], log=lambda *x: print(*x, flush=True))
    print(json.dumps(summary(st), ensure_ascii=False, indent=1))
    if st.get("report"):
        print("\n" + st["report"])
