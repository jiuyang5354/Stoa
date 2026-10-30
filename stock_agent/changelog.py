"""更新日志：随包带一份 changelog.json（由 GitHub Releases 生成），界面里 Fasti 浮层读它。

- parse(tag, name, body, published)：把一条 Release 的正文拆成 {version, date, title, items:[{type, text, head?, group?}]}
- load()：本地 changelog.json + 已检查到的更新版本（只读 updater 的缓存，不额外联网）
- 生成/刷新：python -m stock_agent.changelog releases.json  （releases.json 是 GitHub releases 列表接口的原始返回）
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

FILE = Path(__file__).resolve().parent / "changelog.json"
HKT = timezone(timedelta(hours=8))
_SKIP = re.compile(r"^(下载|Windows 用户|Windows 版|仅供参考)|构建完成后自动上传")
_FIX = re.compile(r"^(修复|修正|修好|修|fix)", re.I)
_ADD = re.compile(r"^(新增|新加|加入|加上|增加|支持|内置|接入|打通|首次|新：|add)", re.I)
_CHG = re.compile(r"^(改|换|重写|统一|移|默认|收|去掉|拆|升级|调整|优化|简化|精简|change)", re.I)


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3]) or (0,)


def _kind(text: str, version: str) -> str:
    t = text.strip()
    if _FIX.search(t) or "不再" in t[:24]:
        return "fixed"
    if _ADD.search(t):
        return "added"
    if _CHG.search(t):
        return "changed"
    v = _ver(version)
    return "fixed" if len(v) >= 3 and v[2] > 0 else "added"


def _clean(s: str) -> str:
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.replace("**", "").replace("__", "").strip()


def parse(tag: str, name: str, body: str, published: str = "") -> dict:
    version = (tag or "").lstrip("v")
    title = ""
    if name and "·" in name:
        title = name.split("·", 1)[1].strip()
    date = ""
    if published:
        try:
            date = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(HKT).strftime("%Y-%m-%d")
        except ValueError:
            date = published[:10]
    items, group = [], ""
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _SKIP.search(line):
            continue
        m = re.fullmatch(r"\*\*(.+?)\*\*[：:]?", line)
        if m:                                   # 单独一行的粗体 = 小节标题
            group = _clean(m.group(1))
            continue
        line = re.sub(r"^([-*+•]|\d+[.)、])\s*", "", line)
        if _SKIP.search(line):
            continue
        head = ""
        hm = re.match(r"\*\*(.+?)\*\*\s*[：:]\s*", line)
        if hm:
            head, line = _clean(hm.group(1)), line[hm.end():]
        text = _clean(line)
        if not text:
            continue
        it = {"type": "fixed" if head.startswith("修") else _kind(head or text, version), "text": text}
        if head and head not in ("修复", "新增", "改动", "修正"):
            it["head"] = head
        if group:
            it["group"] = group
        items.append(it)
    return {"version": version, "date": date, "title": title, "items": items}


def build(releases: list) -> list:
    out = [parse(r.get("tag_name", ""), r.get("name", ""), r.get("body", ""), r.get("published_at", ""))
           for r in releases if not r.get("draft")]
    out = [r for r in out if r["version"]]
    out.sort(key=lambda r: _ver(r["version"]), reverse=True)
    return out


def load(update: dict | None = None) -> dict:
    from . import __version__
    try:
        rel = json.loads(FILE.read_text("utf-8"))
    except (OSError, ValueError):
        rel = []
    u = update or {}
    if u.get("available") and u.get("latest") and not any(r["version"] == u["latest"] for r in rel):
        top = parse("v" + u["latest"], u.get("name", ""), u.get("notes", ""), u.get("published", ""))
        top["upcoming"] = True
        top["url"] = u.get("url")
        rel = [top] + rel
    return {"current": __version__, "releases": rel}


if __name__ == "__main__":
    src = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    data = build(src if isinstance(src, list) else src.get("data", []))
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(f"{len(data)} releases → {FILE}")
