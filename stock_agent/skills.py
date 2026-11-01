"""程序性记忆：Stoa 自己写的技能（参考 Hermes Agent 的 skills）。

做完一件多步骤、以后还会再做的事（比如「港股财报季体检」「放量突破回测」），
Agent 把做法写成 skills/<name>.md；以后遇到同类任务先读技能再动手，越用越熟。
系统提示里只放技能索引（名字 + 一句话），需要时再用 skill_view 读全文。

v0.39 生命周期（仿 Hermes curator）：skills/.usage.json 记每个技能的使用次数、最近使用、来源（created_by）、
状态（active → stale → archived）、是否钉住、在哪些模型上验证过（models）。归档的移到 skills/.archive/，可恢复。
来源是后台复盘（review）或离线进化（evolve）的技能只在验证过的模型上进索引——换了模型（尤其更小的）不自动套用。
"""
import json
import re
import shutil
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .memory import home

MAX_SKILLS = 60
MAX_LEN = 4000
MAX_INDEX = 20          # 系统提示里最多完整列出这么多个自己的技能（其余只列名字），防止技能堆积占上下文
EVOLVED = ("review", "evolve")  # 自进化来源：按模型分开记
CURATED = ("agent", "review", "evolve")  # curator 管得着的来源（user / 内置不动）
_ULOCK = threading.RLock()


def _dir():
    d = home() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


BUILTIN = Path(__file__).with_name("builtin_skills")  # 随程序发布的内置技能（投研流程）；用户/Agent 用同名 skill_save 覆盖后以数据目录里的为准


def _builtin():
    return {p.stem: p for p in sorted(BUILTIN.glob("*.md"))} if BUILTIN.is_dir() else {}


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def model_key(model) -> str:
    """模型名归一（同一个模型走不同中转算同一个）：去掉「服务商:」前缀、小写。"""
    m = str(model or "").strip().lower()
    return m.split(":", 1)[1] if ":" in m and not re.match(r"^[\w.-]+:\d", m) else m


def _usage_file() -> Path:
    return _dir() / ".usage.json"


def usage_all() -> dict:
    with _ULOCK:
        p = _usage_file()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}


def _usage_save(u: dict):
    with _ULOCK:
        p = _usage_file()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)


def meta(name: str) -> dict:
    """一个技能的使用与生命周期记录（没有记录的旧技能按 user 来源、active 处理）。"""
    e = usage_all().get(slug(name)) or {}
    return {"use_count": 0, "last_used_at": "", "created_at": "", "created_by": "user", "state": "active", "pinned": False,
            "models": [], "version": 0, **e}


def set_meta(name: str, **kw) -> dict:
    with _ULOCK:
        u = usage_all()
        e = {**meta(name), **(u.get(slug(name)) or {}), **kw}
        u[slug(name)] = e
        _usage_save(u)
        return e


def bump(name: str):
    """记一次使用（skill_view / 本轮自动附上）。内置技能也记，curator 不动它们。"""
    try:
        with _ULOCK:
            e = meta(name)
            set_meta(name, use_count=int(e.get("use_count") or 0) + 1, last_used_at=_now(),
                     **({"state": "active"} if e.get("state") == "stale" else {}))
    except Exception:
        pass


def applies(name: str, model=None, m: dict | None = None) -> bool:
    """自进化来源的技能只在验证过的模型上生效；model=None（不分模型的场合，如设置页）一律算。"""
    m = m or meta(name)
    if m.get("created_by") not in EVOLVED or model is None:
        return True
    return model_key(model) in {model_key(x) for x in m.get("models") or []}


NOTE_HEAD = "## 纠正与坑（复盘，只在验证过的模型上附上）"


def notes_for(name: str, model=None) -> list[dict]:
    """自进化给已有技能（内置 / 用户 / Agent 的）追加的做法：按模型分开记，不改原文件。"""
    return [n for n in meta(name).get("notes") or [] if model is None or model_key(model) in {model_key(x) for x in n.get("models") or []}]


def add_note(name: str, item: dict):
    with _ULOCK:
        m = meta(name)
        ns = [n for n in m.get("notes") or [] if n.get("id") != item.get("id")] + [item]
        set_meta(name, notes=ns)


def remove_note(name: str, nid: str):
    with _ULOCK:
        m = meta(name)
        set_meta(name, notes=[n for n in m.get("notes") or [] if n.get("id") != nid])


def _with_notes(name: str, body: str, model) -> str:
    ns = notes_for(name, model) if model is not None else []
    if not ns:
        return body
    return body.rstrip() + "\n\n" + NOTE_HEAD + "\n" + "\n".join(f"- {n['text']}" for n in ns)


def _path(name: str):
    p = _dir() / f"{slug(name)}.md"
    return p if p.exists() else _builtin().get(slug(name)) or p


def slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name.strip().lower()).strip("-")
    return s[:48] or "skill"


def _parse(text: str) -> dict:
    meta, body = {}, text
    m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = m.group(2)
    meta["body"] = body.strip()
    return meta


def list_skills() -> list[dict]:
    out, own = [], {p.stem for p in _dir().glob("*.md")}
    for name, p in _builtin().items():
        if name not in own:
            m = _parse(p.read_text(encoding="utf-8"))
            out.append({"name": name, "description": m.get("description", ""), "updated": "", "uses": int(m.get("uses", 0) or 0), "builtin": True})
    u = usage_all()
    for p in sorted(_dir().glob("*.md")):
        m = _parse(p.read_text(encoding="utf-8"))
        e = {**meta(p.stem), **(u.get(p.stem) or {})}
        out.append({"name": p.stem, "description": m.get("description", ""), "updated": m.get("updated", ""),
                    "uses": max(int(m.get("uses", 0) or 0), int(e.get("use_count") or 0)),
                    "builtin": False, "overrides": p.stem in _builtin(), "state": e.get("state", "active"), "created_by": e.get("created_by", "user"),
                    "models": e.get("models") or [], "pinned": bool(e.get("pinned")), "last_used": e.get("last_used_at", ""), "version": e.get("version", 0)})
    return out


def archived() -> list[dict]:
    d = _dir() / ".archive"
    out = []
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        m = _parse(p.read_text(encoding="utf-8"))
        e = meta(p.stem)
        out.append({"name": p.stem, "description": m.get("description", ""), "state": "archived", "created_by": e.get("created_by"),
                    "archived_at": e.get("archived_at", ""), "absorbed_into": e.get("absorbed_into", ""), "uses": e.get("use_count", 0)})
    return out


def index(model=None) -> str:
    sk = [x for x in list_skills() if x.get("builtin") or applies(x["name"], model, {"created_by": x.get("created_by"), "models": x.get("models")})]
    if not sk:
        return "## 你的技能\n（还没有。做完一件多步骤、以后还会再做的分析后，用 skill_save 把做法存下来。）"
    # 内置流程只给名字和半句说明（全文 skill_view 再读）；用户/Agent 自己存的技能给完整一句话
    def brief(d):
        d = re.split(r"[：:，,（(]", d or "", 1)[0]
        return d[:18]
    own = [s for s in sk if not s.get("builtin")]
    bi = [s for s in sk if s.get("builtin")]
    act = sorted([s for s in own if s.get("state") != "stale"], key=lambda s: (bool(s.get("pinned")), s.get("last_used") or "", s.get("uses") or 0), reverse=True)
    full, rest = act[:MAX_INDEX], act[MAX_INDEX:] + [s for s in own if s.get("state") == "stale"]
    out = "## 技能（同类任务先 skill_view 再动手；标 * 的是内置投研流程）\n"
    if bi:
        out += "内置：" + "；".join(f"{s['name']}*（{brief(s['description'])}）" for s in bi) + "\n"
    out += "\n".join(f"- {s['name']}：{s['description']}" for s in sorted(full, key=lambda s: s["name"]))
    if rest:
        out += "\n不常用（要用先 skill_view）：" + "、".join(s["name"] for s in rest)
    return out


def auto_load(text: str, limit: int = 2, model=None) -> list[dict]:
    """内置技能 front matter 的 auto: 正则命中提问时，返回技能全文（本轮附在提问后，不进每轮的系统提示）。
    用户/Agent 同名覆盖过的，用覆盖版的正文。"""
    out = []
    for name, p in _builtin().items():
        m = _parse(p.read_text(encoding="utf-8"))
        pat = m.get("auto")
        if not pat:
            continue
        try:
            hit = re.search(pat, text or "")
        except re.error:
            continue
        if hit:
            own = _dir() / f"{name}.md"
            body = _parse(own.read_text(encoding="utf-8"))["body"] if own.exists() else m["body"]
            out.append({"name": name, "description": m.get("description", ""), "content": _with_notes(name, body, model), "hit": hit.group(0)})
            bump(name)
            if len(out) >= limit:
                break
    # v0.39.1：复盘新建的技能带 auto 触发词（只收本轮提问里出现过的词），同类提问也自动附上；只在验证过的模型上、未归档/未过时的
    if len(out) < limit and model is not None:
        bi = _builtin()
        for p in sorted(_dir().glob("*.md")):
            if p.stem in bi or len(out) >= limit:
                continue
            e = meta(p.stem)
            if e.get("created_by") not in CURATED or e.get("state") in ("archived",) or not applies(p.stem, model, e):
                continue
            m = _parse(p.read_text(encoding="utf-8"))
            pat = m.get("auto")
            if not pat:
                continue
            try:
                hit = re.search(pat, text or "")
            except re.error:
                continue
            if hit:
                out.append({"name": p.stem, "description": m.get("description", ""), "content": _with_notes(p.stem, m["body"], model), "hit": hit.group(0)})
                bump(p.stem)
    return out


def view(name: str, count: bool = True, model=None) -> dict:
    p = _path(name)
    if not p.exists():
        return {"error": f"没有叫 {name} 的技能", "skills": [s["name"] for s in list_skills()]}
    m = _parse(p.read_text(encoding="utf-8"))
    if count:  # 使用次数记在 .usage.json（不再改写技能文件本身）；内置技能也记，curator 不动它们
        bump(p.stem)
    return {"name": p.stem, "description": m.get("description", ""), "content": _with_notes(p.stem, m["body"], model)}


def _write(p, m):
    p.write_text(f"---\nname: {p.stem}\ndescription: {m.get('description', '')}\nupdated: {m.get('updated', '')}\n"
                 f"uses: {m.get('uses', 0)}\n---\n{m['body']}\n", encoding="utf-8")


def save(name: str, description: str, content: str, origin: str = "agent", model=None) -> str:
    content, description = content.strip(), " ".join(description.split())[:120]
    if not content or not description:
        return "技能需要 description 和 content。"
    if len(content) > MAX_LEN:
        return f"技能正文超过 {MAX_LEN} 字，请精简成步骤清单。"
    p = _dir() / f"{slug(name)}.md"
    if not p.exists() and len(list(_dir().glob("*.md"))) >= MAX_SKILLS:
        return f"技能已满 {MAX_SKILLS} 个，先删掉不用的。"
    old = _parse(p.read_text(encoding="utf-8")) if p.exists() else {}
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    _write(p, {"description": description, "updated": today, "uses": old.get("uses", 0), "body": content})
    m = meta(p.stem)
    kw = {"version": int(m.get("version") or 0) + 1, "state": "active", "last_patched_at": _now()}
    if not old:
        kw.update(created_at=_now(), created_by=origin, last_used_at=_now())
    if origin in EVOLVED and model:  # 自进化写的：记下在哪个模型上得来/验证
        kw["models"] = sorted({*(m.get("models") or []), model_key(model)}) if m.get("created_by") in EVOLVED or not old else [model_key(model)]
        if old and m.get("created_by") not in EVOLVED:
            kw["created_by"] = m.get("created_by")  # 改的是用户/Agent 自己的技能：来源不变，全模型生效
            kw.pop("models")
    set_meta(p.stem, **kw)
    return f"{'已更新' if old else '已保存'}技能 {p.stem}。"


def read_raw(name: str) -> str | None:
    """数据目录里这个技能文件的原文（没有返回 None；内置版本不算）。给版本记录 / 回滚用。"""
    p = _dir() / f"{slug(name)}.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def write_raw(name: str, text: str | None, usage: dict | None = None):
    """回滚：把技能文件恢复成 text（None = 删除），usage 一并恢复。"""
    p = _dir() / f"{slug(name)}.md"
    a = _dir() / ".archive" / f"{slug(name)}.md"
    if text is None:
        p.unlink(missing_ok=True)
    else:
        a.unlink(missing_ok=True)
        p.write_text(text, encoding="utf-8")
    with _ULOCK:
        u = usage_all()
        if usage is None:
            u.pop(slug(name), None)
        else:
            u[slug(name)] = usage
        _usage_save(u)


def archive(name: str, absorbed_into: str = "") -> bool:
    """移到 skills/.archive/（不删除，可恢复）。"""
    p = _dir() / f"{slug(name)}.md"
    if not p.exists():
        return False
    d = _dir() / ".archive"
    d.mkdir(exist_ok=True)
    shutil.move(str(p), str(d / p.name))
    set_meta(p.stem, state="archived", archived_at=_now(), absorbed_into=absorbed_into)
    return True


def restore(name: str) -> bool:
    a = _dir() / ".archive" / f"{slug(name)}.md"
    if not a.exists():
        return False
    shutil.move(str(a), str(_dir() / a.name))
    set_meta(a.stem, state="active", archived_at="", absorbed_into="", last_used_at=_now())
    return True


def pin(name: str, on: bool = True) -> dict:
    if not (_dir() / f"{slug(name)}.md").exists():
        return {"error": f"没有叫 {name} 的技能（内置技能不需要钉住）"}
    set_meta(name, pinned=bool(on), **({"state": "active"} if on else {}))
    return {"ok": True, "msg": f"{'已钉住' if on else '已取消钉住'} {slug(name)}"}


def delete(name: str) -> str:
    p = _dir() / f"{slug(name)}.md"
    if not p.exists():
        return f"{name} 是内置技能，不能删除（可以用 skill_save 同名覆盖成你的版本）。" if slug(name) in _builtin() else f"没有叫 {name} 的技能。"
    p.unlink()
    try:
        with _ULOCK:
            u = usage_all()
            if p.stem not in _builtin() and u.pop(p.stem, None) is not None:  # 内置技能的复盘笔记（notes）保留
                _usage_save(u)
    except Exception:
        pass
    if p.stem in _builtin():
        return f"已删除你的 {p.stem} 版本，恢复为内置版本。"
    return f"已删除技能 {p.stem}。"
