"""程序性记忆：Stoa 自己写的技能（参考 Hermes Agent 的 skills）。

做完一件多步骤、以后还会再做的事（比如「港股财报季体检」「放量突破回测」），
Agent 把做法写成 skills/<name>.md；以后遇到同类任务先读技能再动手，越用越熟。
系统提示里只放技能索引（名字 + 一句话），需要时再用 skill_view 读全文。
"""
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .memory import home

MAX_SKILLS = 60
MAX_LEN = 4000


def _dir():
    d = home() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


BUILTIN = Path(__file__).with_name("builtin_skills")  # 随程序发布的内置技能（投研流程）；用户/Agent 用同名 skill_save 覆盖后以数据目录里的为准


def _builtin():
    return {p.stem: p for p in sorted(BUILTIN.glob("*.md"))} if BUILTIN.is_dir() else {}


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
    for p in sorted(_dir().glob("*.md")):
        m = _parse(p.read_text(encoding="utf-8"))
        out.append({"name": p.stem, "description": m.get("description", ""), "updated": m.get("updated", ""), "uses": int(m.get("uses", 0) or 0),
                    "builtin": False, "overrides": p.stem in _builtin()})
    return out


def index() -> str:
    sk = list_skills()
    if not sk:
        return "## 你的技能\n（还没有。做完一件多步骤、以后还会再做的分析后，用 skill_save 把做法存下来。）"
    # 内置流程只给名字和半句说明（全文 skill_view 再读）；用户/Agent 自己存的技能给完整一句话
    def brief(d):
        d = re.split(r"[：:，,（(]", d or "", 1)[0]
        return d[:18]
    own = [s for s in sk if not s.get("builtin")]
    bi = [s for s in sk if s.get("builtin")]
    out = "## 技能（同类任务先 skill_view 再动手；标 * 的是内置投研流程）\n"
    if bi:
        out += "内置：" + "；".join(f"{s['name']}*（{brief(s['description'])}）" for s in bi) + "\n"
    return out + "\n".join(f"- {s['name']}：{s['description']}" for s in own)


def auto_load(text: str, limit: int = 2) -> list[dict]:
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
            out.append({"name": name, "description": m.get("description", ""), "content": body, "hit": hit.group(0)})
            if len(out) >= limit:
                break
    return out


def view(name: str, count: bool = True) -> dict:
    p = _path(name)
    if not p.exists():
        return {"error": f"没有叫 {name} 的技能", "skills": [s["name"] for s in list_skills()]}
    m = _parse(p.read_text(encoding="utf-8"))
    if count and p.parent != BUILTIN:  # 只统计 Agent 自己调用的次数；内置技能只读
        m["uses"] = int(m.get("uses", 0) or 0) + 1
        _write(p, m)
    return {"name": p.stem, "description": m.get("description", ""), "content": m["body"]}


def _write(p, m):
    p.write_text(f"---\nname: {p.stem}\ndescription: {m.get('description', '')}\nupdated: {m.get('updated', '')}\n"
                 f"uses: {m.get('uses', 0)}\n---\n{m['body']}\n", encoding="utf-8")


def save(name: str, description: str, content: str) -> str:
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
    return f"{'已更新' if old else '已保存'}技能 {p.stem}。"


def delete(name: str) -> str:
    p = _dir() / f"{slug(name)}.md"
    if not p.exists():
        return f"{name} 是内置技能，不能删除（可以用 skill_save 同名覆盖成你的版本）。" if slug(name) in _builtin() else f"没有叫 {name} 的技能。"
    p.unlink()
    if p.stem in _builtin():
        return f"已删除你的 {p.stem} 版本，恢复为内置版本。"
    return f"已删除技能 {p.stem}。"
