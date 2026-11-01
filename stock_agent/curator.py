"""Curator · 技能生命周期（v0.39，仿 Hermes Agent curator）。

只管 Agent / 复盘 / 进化写的技能（created_by ∈ agent、review、evolve）；用户自己的、内置的不动，钉住的不动。
  · 不用了：最近一次使用（没用过按创建时间）距今 ≥ stale_after_days（默认 14）→ stale（索引里只列名字）；
            ≥ archive_after_days（默认 30）→ 移到 skills/.archive/（不删，可恢复；归档进版本记录，可一键回滚）。
            stale 的技能再被用一次自动回到 active。
  · 重复：两两比较说明 + 正文的字符三元组相似度 ≥ merge_threshold（默认 0.6）→ 用得多的那个吸收另一个独有的行，另一个归档（absorbed_into）。
    合并后超长、或合并会碰到红线（redline）就不合并，只在报告里提一句。
  · 报告：data/evolve/curator/<时间>.md + .json；界面「学到的东西」显示最近一次。
确定性规则，不调模型（Hermes 的模型合并是可选项，默认关；这里不做）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN = timezone(timedelta(hours=8))


def _cfg() -> dict:
    from .evolve import cfg
    return cfg()["curator"]


def _dir() -> Path:
    from .evolve import _dir as ed
    d = ed() / "curator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=CN)
        except Exception:
            return None


def _grams(s: str) -> set:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def similarity(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def due(now: datetime | None = None) -> bool:
    c = _cfg()
    from .evolve import _on
    if not _on(c.get("enabled")):
        return False
    p = _dir() / "state.json"
    try:
        last = _parse_ts(json.loads(p.read_text(encoding="utf-8")).get("last_run_at", ""))
    except Exception:
        last = None
    now = now or datetime.now(CN)
    if last is None:  # 第一次只记下时间，满一个周期才真跑（给用户留出钉住的时间）
        p.write_text(json.dumps({"last_run_at": now.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        return False
    return now - last >= timedelta(hours=float(c.get("interval_hours") or 24))


def maybe_run():
    try:
        if due():
            return run()
    except Exception:
        return None


def run(dry_run: bool = False, now: datetime | None = None, force: bool = False) -> dict:
    from . import skills, evolve, redline
    c = _cfg()
    now = now or datetime.now(CN)
    stale_d, arch_d = float(c.get("stale_after_days") or 14), float(c.get("archive_after_days") or 30)
    thr = float(c.get("merge_threshold") or 0.6)
    rep = {"at": now.strftime("%Y-%m-%d %H:%M:%S"), "dry_run": dry_run, "stale": [], "archived": [], "reactivated": [], "merged": [], "skipped": [],
           "managed": 0, "kept": 0}
    sk = [s for s in skills.list_skills() if not s.get("builtin")]
    managed = [s for s in sk if s.get("created_by") in skills.CURATED and not s.get("pinned")]
    rep["managed"] = len(managed)
    alive = []
    for s in managed:
        m = skills.meta(s["name"])
        last = _parse_ts(m.get("last_used_at") or "") or _parse_ts(m.get("created_at") or "") or _mtime(s["name"])
        idle = (now - last).total_seconds() / 86400 if last else 0
        if idle >= arch_d:
            rep["archived"].append({"name": s["name"], "idle_days": round(idle, 1), "uses": m.get("use_count", 0)})
            if not dry_run:
                evolve.propose_change("skill", s["name"], None, "curator", "", f"归档 {s['name']}（{int(idle)} 天没用）")
            continue
        if idle >= stale_d:
            if m.get("state") != "stale":
                rep["stale"].append({"name": s["name"], "idle_days": round(idle, 1)})
                if not dry_run:
                    skills.set_meta(s["name"], state="stale")
        elif m.get("state") == "stale":
            rep["reactivated"].append(s["name"])
            if not dry_run:
                skills.set_meta(s["name"], state="active")
        alive.append(s)
    # 合并重复
    done = set()
    texts = {}
    for s in alive:
        v = skills.view(s["name"], count=False)
        texts[s["name"]] = (v.get("description", ""), v.get("content", ""))
    for i, a in enumerate(alive):
        for b in alive[i + 1:]:
            if a["name"] in done or b["name"] in done:
                continue
            sim = similarity(" ".join(texts[a["name"]]), " ".join(texts[b["name"]]))
            if sim < thr:
                continue
            keep, drop = (a, b) if (a.get("uses") or 0, a.get("last_used") or "") >= (b.get("uses") or 0, b.get("last_used") or "") else (b, a)
            kb, db = texts[keep["name"]][1], texts[drop["name"]][1]
            extra = [ln for ln in db.splitlines() if ln.strip() and ln.strip() not in kb]
            body = kb.rstrip() + (f"\n\n## 合并自 {drop['name']}\n" + "\n".join(extra) if extra else "")
            if len(body) > skills.MAX_LEN:
                rep["skipped"].append({"pair": [keep["name"], drop["name"]], "sim": round(sim, 2), "why": "合并后超长"})
                continue
            if redline.assess("skill", keep["name"], kb, body)["level"] != "none":
                rep["skipped"].append({"pair": [keep["name"], drop["name"]], "sim": round(sim, 2), "why": "合并会碰到红线规则，留给人看"})
                continue
            rep["merged"].append({"keep": keep["name"], "absorbed": drop["name"], "sim": round(sim, 2), "lines": len(extra)})
            done |= {keep["name"], drop["name"]}
            if not dry_run:
                if extra:
                    km = skills.meta(keep["name"])
                    evolve.propose_change("skill", keep["name"], evolve.render_skill(keep["name"], texts[keep["name"]][0], body, int(km.get("use_count") or 0)),
                                          "curator", "", f"合并：{drop['name']} → {keep['name']}")
                evolve.propose_change("skill", drop["name"], None, "curator", "", f"归档 {drop['name']}（并入 {keep['name']}）")
                skills.set_meta(drop["name"], absorbed_into=keep["name"])
    rep["kept"] = len(alive) - len(rep["merged"])
    rep["summary"] = (f"技能整理{'（预演）' if dry_run else ''}：管理 {rep['managed']} 个 · 变不常用 {len(rep['stale'])} · 归档 {len(rep['archived'])} · "
                      f"合并 {len(rep['merged'])} · 恢复常用 {len(rep['reactivated'])}" + (f" · 跳过 {len(rep['skipped'])}" if rep["skipped"] else ""))
    if not dry_run:
        stamp = now.strftime("%Y%m%d-%H%M%S")
        (_dir() / f"{stamp}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        md = [f"# {rep['summary']}", "", f"时间：{rep['at']}"]
        md += [f"- stale：{x['name']}（{x['idle_days']} 天没用）" for x in rep["stale"]]
        md += [f"- 归档：{x['name']}（{x['idle_days']} 天没用，用过 {x['uses']} 次）" for x in rep["archived"]]
        md += [f"- 合并：{x['absorbed']} → {x['keep']}（相似度 {x['sim']}，并入 {x['lines']} 行）" for x in rep["merged"]]
        md += [f"- 跳过合并：{' / '.join(x['pair'])}（{x['why']}）" for x in rep["skipped"]]
        (_dir() / f"{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        (_dir() / "state.json").write_text(json.dumps({"last_run_at": rep["at"]}), encoding="utf-8")
    return rep


def _mtime(name: str):
    from . import skills
    p = skills._dir() / f"{skills.slug(name)}.md"
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, CN)
    except Exception:
        return None


def last_report() -> dict | None:
    fs = sorted(_dir().glob("2*.json"))
    if not fs:
        return None
    try:
        return json.loads(fs[-1].read_text(encoding="utf-8"))
    except Exception:
        return None
