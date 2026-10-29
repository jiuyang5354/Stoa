"""Hermes 式记忆：
- MEMORY.md：Agent 自己的笔记（市场规律、踩过的坑、复盘出的经验）
- USER.md：用户画像（持仓、风险偏好、关注方向、习惯）
  两者都有字数上限，满了必须合并精简，每次会话自动注入。
- sessions.db：所有对话存 SQLite + FTS5，可跨会话全文检索。
- calls 表：Agent 给出的判断记账，到期自动复盘，对错沉淀进 MEMORY.md。
"""
import json
import re
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
SEP = "\n§\n"
LIMITS = {"memory": 2200, "user": 1375}
FILES = {"memory": "MEMORY.md", "user": "USER.md"}
ROOT = Path(__file__).resolve().parent.parent


def home() -> Path:
    return Path(os.getenv("STOCK_AGENT_HOME") or ROOT)


def config_path() -> Path:
    """配置放在数据目录里（容器里是 /data/config.yaml），首次运行从仓库默认配置复制。"""
    p = home() / "config.yaml"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return p


def now() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")


class Memory:
    def __init__(self, home_dir: str | Path | None = None):
        self.home = Path(home_dir) if home_dir else home()
        self.mem_dir = self.home / "memory"
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        (self.home / "data").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.home / "data" / "sessions.db", check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session TEXT, ts TEXT, role TEXT, content TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, session UNINDEXED, ts UNINDEXED, role UNINDEXED, tokenize='trigram');
            CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, name TEXT, view TEXT,
                price REAL, horizon_days INTEGER, reason TEXT, due TEXT, status TEXT DEFAULT 'open',
                result_price REAL, result_pct REAL, verdict TEXT);
            """
        )

    # ---------- 记忆文件 ----------
    def _path(self, target: str) -> Path:
        return self.mem_dir / FILES[target]

    def entries(self, target: str) -> list[str]:
        p = self._path(target)
        if not p.exists():
            return []
        return [e.strip() for e in p.read_text(encoding="utf-8").split(SEP) if e.strip()]

    def _write(self, target: str, entries: list[str]) -> str | None:
        text = SEP.join(entries)
        if len(text) > LIMITS[target]:
            return (f"{FILES[target]} 超出上限 {LIMITS[target]} 字（现在 {len(text)} 字）。"
                    "先用 replace 合并或 remove 删掉过时条目，再写入。")
        self._path(target).write_text(text + ("\n" if text else ""), encoding="utf-8")
        return None

    @staticmethod
    def dated(content: str) -> str:
        """每条记忆带日期，方便判断新旧、过时就清理。"""
        content = " ".join(content.split())
        if re.match(r"^\[?\d{4}-\d{2}-\d{2}", content):
            return content
        return f"[{now()[:10]}] {content}"

    def add(self, target: str, content: str) -> str:
        content = self.dated(content)
        if any(e.split("] ", 1)[-1] == content.split("] ", 1)[-1] for e in self.entries(target)):
            return "已存在，未重复写入。"
        es = self.entries(target)
        if content in es:
            return "已存在，未重复写入。"
        err = self._write(target, es + [content])
        return err or f"已写入 {FILES[target]}（{self.usage(target)}）。"

    def replace(self, target: str, old_text: str, content: str) -> str:
        es = self.entries(target)
        hits = [i for i, e in enumerate(es) if old_text in e]
        if len(hits) != 1:
            return f"old_text 匹配到 {len(hits)} 条，需要恰好 1 条。"
        es[hits[0]] = self.dated(content)
        err = self._write(target, es)
        return err or f"已更新（{self.usage(target)}）。"

    def remove(self, target: str, old_text: str) -> str:
        es = self.entries(target)
        hits = [i for i, e in enumerate(es) if old_text in e]
        if len(hits) != 1:
            return f"old_text 匹配到 {len(hits)} 条，需要恰好 1 条。"
        es.pop(hits[0])
        self._write(target, es)
        return f"已删除（{self.usage(target)}）。"

    def usage(self, target: str) -> str:
        n = len(SEP.join(self.entries(target)))
        return f"{n}/{LIMITS[target]} 字"

    def fill(self, target: str) -> float:
        return len(SEP.join(self.entries(target))) / LIMITS[target]

    def overwrite(self, target: str, entries: list[str]) -> str | None:
        """整理记忆时整体改写（先备份到 memory/backup/）。"""
        p = self._path(target)
        if p.exists():
            b = self.mem_dir / "backup"
            b.mkdir(exist_ok=True)
            (b / f"{FILES[target]}.{now()[:10]}.bak").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        return self._write(target, [self.dated(e) for e in entries if e.strip()])

    def snapshot(self) -> str:
        parts = []
        for t, title in (("user", "关于用户（USER.md）"), ("memory", "你的笔记（MEMORY.md）")):
            es = self.entries(t)
            body = "\n".join(f"- {e}" for e in es) if es else "（空）"
            warn = "（快满了：先合并相近条目、删掉过时的）" if self.fill(t) > .8 else ""
            parts.append(f"## {title} · {self.usage(t)}{warn}\n{body}")
        return "\n\n".join(parts)

    # ---------- 会话检索 ----------
    def log(self, session: str, role: str, content: str):
        if not content:
            return
        ts = now()
        self.db.execute("INSERT INTO messages(session,ts,role,content) VALUES(?,?,?,?)", (session, ts, role, content))
        self.db.execute("INSERT INTO messages_fts(content,session,ts,role) VALUES(?,?,?,?)", (content, session, ts, role))
        self.db.commit()

    def search(self, query: str, limit: int = 8) -> list[dict]:
        q = query.strip()
        rows = []
        if len(q) >= 3:
            try:
                rows = self.db.execute(
                    "SELECT ts, role, snippet(messages_fts,0,'[',']','…',24) FROM messages_fts "
                    "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?", (json.dumps(q, ensure_ascii=False), limit)
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:  # 短词或无结果时退回 LIKE
            rows = self.db.execute(
                "SELECT ts, role, substr(content,1,200) FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
        return [{"time": t, "role": r, "text": s} for t, r, s in rows]

    # ---------- 判断记账与复盘 ----------
    def record_call(self, symbol, name, view, price, horizon_days, reason) -> int:
        due = (datetime.now(CN_TZ) + timedelta(days=int(horizon_days))).strftime("%Y-%m-%d")
        cur = self.db.execute(
            "INSERT INTO calls(ts,symbol,name,view,price,horizon_days,reason,due) VALUES(?,?,?,?,?,?,?,?)",
            (now(), symbol, name, view, price, horizon_days, reason, due),
        )
        self.db.commit()
        return cur.lastrowid

    def due_calls(self) -> list[dict]:
        today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
        cols = ["id", "ts", "symbol", "name", "view", "price", "horizon_days", "reason", "due"]
        rows = self.db.execute(
            f"SELECT {','.join(cols)} FROM calls WHERE status='open' AND due<=?", (today,)
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def close_call(self, cid: int, result_price: float, result_pct: float, verdict: str):
        self.db.execute(
            "UPDATE calls SET status='closed', result_price=?, result_pct=?, verdict=? WHERE id=?",
            (result_price, result_pct, verdict, cid),
        )
        self.db.commit()

    def recent_calls(self, limit: int = 120) -> list[dict]:
        cols = ["id", "ts", "symbol", "name", "view", "price", "horizon_days", "reason", "due", "status", "result_pct", "verdict"]
        rows = self.db.execute(f"SELECT {','.join(cols)} FROM calls ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(cols, r)) for r in rows][::-1]

    def call_stats(self) -> dict:
        """判断账本：总体准确率，并按方向、市场、周期拆开，找出自己哪里常错。"""
        open_n = self.db.execute("SELECT COUNT(*) FROM calls WHERE status='open'").fetchone()[0]
        rows = self.db.execute("SELECT ts, symbol, view, horizon_days, result_pct, verdict FROM calls WHERE status='closed' ORDER BY ts").fetchall()
        if not rows:
            return {"open": open_n, "closed": 0}

        def mk(sym):
            s = sym.upper()
            return "A股" if s.endswith((".SS", ".SZ", ".BJ")) else "港股" if s.endswith(".HK") else "美股"

        def hz(d):
            return "≤5天" if d <= 5 else "6-20天" if d <= 20 else ">20天"

        def agg(rs):
            n = len(rs)
            right = sum(r[5] == "对" for r in rs)
            return {"n": n, "对": right, "错": n - right, "准确率%": round(right / n * 100, 1), "平均涨跌%": round(sum(r[4] or 0 for r in rs) / n, 2)}

        def group(key):
            g = {}
            for r in rows:
                g.setdefault(key(r), []).append(r)
            return {k: agg(v) for k, v in g.items()}

        last = rows[-10:]
        streak, sv = 0, rows[-1][5]
        for r in reversed(rows):
            if r[5] != sv:
                break
            streak += 1
        out = {"open": open_n, "closed": len(rows), **agg(rows),
               "按方向": group(lambda r: r[2]), "按市场": group(lambda r: mk(r[1])), "按周期": group(lambda r: hz(r[3] or 0)),
               "最近10次": "".join("✓" if r[5] == "对" else "✗" for r in last), "当前连续": f"{streak}次{sv}"}
        weak = [f"{k}={v['准确率%']}%" for grp in ("按方向", "按市场", "按周期") for k, v in out[grp].items() if v["n"] >= 5 and v["准确率%"] < 45]
        if weak:
            out["短板"] = "、".join(weak)
        return out
