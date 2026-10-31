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
import threading
from functools import wraps
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
        # 先写临时文件再改名：首次启动时几个线程同时读配置，不能读到写了一半的空文件
        tmp = p.with_name(f"config.yaml.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        try:
            os.replace(tmp, p)
        except OSError:
            tmp.unlink(missing_ok=True)
    return p


UNDO_SECONDS = 60  # 删除对话后可撤销的时间


def now() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")


def position_state(text: str) -> str:
    if "【待确认】" in text:
        return "pending"
    if re.search(r"【计划】|假如|假设|如果|计划|打算|准备买|想买|考虑买", text):
        return "planned"
    if re.search(r"【历史持仓】|曾经|以前持有|已经清仓|已清仓|已卖出|不再持有|未持有|没有持有", text):
        return "historical"
    return "current" if re.search(r"持有|持仓|仓位|成本|买了|买入", text) else "none"


def memory_edit(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._memory_lock:
            return method(self, *args, **kwargs)
    return locked


class Memory:
    def __init__(self, home_dir: str | Path | None = None):
        self.home = Path(home_dir) if home_dir else home()
        self._memory_lock = threading.RLock()
        self.mem_dir = self.home / "memory"
        self.mem_dir.mkdir(parents=True, exist_ok=True)
        (self.home / "data").mkdir(exist_ok=True)
        self._db_state = threading.local()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session TEXT, ts TEXT, role TEXT, content TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, session UNINDEXED, ts UNINDEXED, role UNINDEXED, tokenize='trigram');
            CREATE TABLE IF NOT EXISTS session_meta(session TEXT PRIMARY KEY, title TEXT, pinned INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS messages_session ON messages(session, id);
            CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, name TEXT, view TEXT,
                price REAL, horizon_days INTEGER, reason TEXT, due TEXT, status TEXT DEFAULT 'open',
                result_price REAL, result_pct REAL, verdict TEXT);
            CREATE TABLE IF NOT EXISTS session_digest(session TEXT PRIMARY KEY, upto_id INTEGER, text TEXT, facts TEXT, ts TEXT, n_msgs INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS memory_changes(id INTEGER PRIMARY KEY, target TEXT, ts TEXT, before_text TEXT, after_text TEXT, source TEXT);
            """
        )
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}
        for c, t in (("confidence", "INTEGER"), ("bench", "TEXT"), ("bench_pct", "REAL"), ("alpha_pct", "REAL"),
                     ("mfe_pct", "REAL"), ("mae_pct", "REAL"), ("edge_pct", "REAL"), ("end_date", "TEXT"),
                     # v0.34 早报判断：来源、翻转条件（文字 + 上下两个价位）、失效时间、早报日期、晚报复盘结果
                     ("source", "TEXT"), ("flip", "TEXT"), ("flip_up", "REAL"), ("flip_down", "REAL"), ("expires", "TEXT"),
                     ("report_date", "TEXT"), ("review", "TEXT"), ("review_note", "TEXT"), ("review_at", "TEXT"), ("review_price", "REAL"), ("price_date", "TEXT")):
            if c not in cols:
                self.db.execute(f"ALTER TABLE calls ADD COLUMN {c} {t}")
        mcols = {r[1] for r in self.db.execute("PRAGMA table_info(session_meta)")}
        for c, t in (("archived", "INTEGER DEFAULT 0"), ("deleted_at", "REAL"), ("auto_title", "INTEGER DEFAULT 0")):
            if c not in mcols:  # 归档 / 删除可撤销（软删除，过一会儿才真删）/ 标题是自动起的
                self.db.execute(f"ALTER TABLE session_meta ADD COLUMN {c} {t}")
        self.db.commit()
        try:
            from . import vectors
            vectors.ensure(self.db)
        except Exception:
            pass

    @property
    def db(self):
        # HTTP、问答与后台索引各用自己的连接，避免并发复用游标和事务。
        connection = getattr(self._db_state, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.home / "data" / "sessions.db")
            self._db_state.connection = connection
        return connection

    # ---------- 记忆文件 ----------
    def _path(self, target: str) -> Path:
        return self.mem_dir / FILES[target]

    def entries(self, target: str) -> list[str]:
        with self._memory_lock:
            p = self._path(target)
            if not p.exists():
                return []
            return [e.strip() for e in p.read_text(encoding="utf-8").split(SEP) if e.strip()]

    def _write(self, target: str, entries: list[str], source: str = "user") -> str | None:
        text = SEP.join(entries)
        if len(text) > LIMITS[target]:
            return (f"{FILES[target]} 超出上限 {LIMITS[target]} 字（现在 {len(text)} 字）。"
                    "先用 replace 合并或 remove 删掉过时条目，再写入。")
        with self._memory_lock:
            path = self._path(target)
            before = path.read_text(encoding="utf-8") if path.exists() else ""
            after = text + ("\n" if text else "")
            if before == after:
                return None
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(after, encoding="utf-8")
            try:
                with self.db:
                    self.db.execute("INSERT INTO memory_changes(target,ts,before_text,after_text,source) VALUES(?,?,?,?,?)",
                                    (target, now(), before, after, source))
                    os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return None

    def changes(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT id,target,ts,source,before_text,after_text FROM memory_changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "target": r[1], "time": r[2], "source": r[3],
                 "summary": next((x for x in r[5].split(SEP) if x not in r[4].split(SEP)), "删除条目").strip()[:120]} for r in rows]

    def undo_change(self, change_id: int) -> str:
        with self._memory_lock:
            row = self.db.execute("SELECT target,before_text,after_text FROM memory_changes WHERE id=?", (change_id,)).fetchone()
            if not row:
                return "变更不存在。"
            path = self._path(row[0])
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != row[2]:
                return "记忆已有后续修改，请先撤销该分类最近一次变更。"
            entries = [e.strip() for e in row[1].split(SEP) if e.strip()]
            error = self._write(row[0], entries, source="undo")
            return error or "已撤销这次记忆变更。"

    def _label_position(self, target: str, content: str, source: str) -> str:
        if target != "user":
            return content
        state = position_state(content)
        if state == "none":
            return content
        label = {"planned": "计划", "historical": "历史持仓", "pending": "待确认", "current": "已确认持仓"}[state]
        if source == "agent" and state == "current":
            label = "待确认"
        content = re.sub(r"【(?:计划|历史持仓|待确认|已确认持仓)】", "", content).strip()
        date = re.match(r"^(\[20\d\d-\d\d-\d\d\]\s*)", content)
        prefix = date.group() if date else ""
        return f"{prefix}【{label}】{content[len(prefix):]}"

    @staticmethod
    def dated(content: str) -> str:
        """每条记忆带日期，方便判断新旧、过时就清理。"""
        content = " ".join(content.split())
        if re.match(r"^\[?\d{4}-\d{2}-\d{2}", content):
            return content
        return f"[{now()[:10]}] {content}"

    @memory_edit
    def add(self, target: str, content: str, source: str = "user") -> str:
        content = self._label_position(target, content, source)
        content = self.dated(content)
        if any(e.split("] ", 1)[-1] == content.split("] ", 1)[-1] for e in self.entries(target)):
            return "已存在，未重复写入。"
        es = self.entries(target)
        if content in es:
            return "已存在，未重复写入。"
        err = self._write(target, es + [content], source=source)
        return err or f"已写入 {FILES[target]}（{self.usage(target)}）。"

    @memory_edit
    def replace(self, target: str, old_text: str, content: str, source: str = "user") -> str:
        es = self.entries(target)
        hits = [i for i, e in enumerate(es) if old_text in e]
        if len(hits) != 1:
            return f"old_text 匹配到 {len(hits)} 条，需要恰好 1 条。"
        es[hits[0]] = self.dated(self._label_position(target, content, source))
        err = self._write(target, es, source=source)
        return err or f"已更新（{self.usage(target)}）。"

    @memory_edit
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

    @memory_edit
    def overwrite(self, target: str, entries: list[str]) -> str | None:
        """整理记忆时整体改写（先备份到 memory/backup/）。"""
        p = self._path(target)
        if p.exists():
            b = self.mem_dir / "backup"
            b.mkdir(exist_ok=True)
            (b / f"{FILES[target]}.{now()[:10]}.bak").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        old = self.entries(target)
        return self._write(target, [self.dated(e if e in old else self._label_position(target, e, "agent")) for e in entries if e.strip()], source="agent")

    def snapshot(self) -> str:
        parts = []
        for t, title in (("user", "关于用户（USER.md）"), ("memory", "你的笔记（MEMORY.md）")):
            es = self.entries(t)
            body = "\n".join(f"- {e}" for e in es) if es else "（空）"
            warn = "（快满了：先合并相近条目、删掉过时的）" if self.fill(t) > .8 else ""
            parts.append(f"## {title} · {self.usage(t)}{warn}\n{body}")
        return "\n\n".join(parts)

    # ---------- 会话检索 ----------
    def log(self, session: str, role: str, content: str) -> int | None:
        if not content:
            return None
        ts = now()
        mid = self.db.execute("INSERT INTO messages(session,ts,role,content) VALUES(?,?,?,?)", (session, ts, role, content)).lastrowid
        self.db.execute("INSERT INTO messages_fts(content,session,ts,role) VALUES(?,?,?,?)", (content, session, ts, role))
        self.db.commit()
        return mid

    def search(self, query: str, limit: int = 8, exclude_ids=()) -> list[dict]:
        """对话检索（v0.17 起为混合检索：BM25 关键词 + 向量语义，RRF 融合；会话纪要也算）。索引不可用时退回 FTS。"""
        try:
            from . import vectors
            hits = vectors.search(self, query, kinds=("msg", "digest"), limit=limit, exclude_refs=exclude_ids)
            out = []
            for h in hits:
                role = (h.get("meta") or {}).get("role") or ("纪要" if h["kind"] == "digest" else "")
                out.append({"time": h["ts"], "role": role, "text": h["text"][:300], "session": h["session"], "via": h["via"], "kind": h["label"]})
            if out:
                return out
        except Exception:
            pass
        return self.search_fts(query, limit)

    def search_fts(self, query: str, limit: int = 8) -> list[dict]:
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

    # ---------- 多会话 ----------
    def sessions(self, prefix: str = "web-", query: str = "", limit: int = 100, archived: bool | None = False) -> list[dict]:
        """对话列表：标题（改过的名字，或第一句提问）、开始/最后时间、条数。query 按全文搜索。
        archived=False 只列未归档的，True 只列已归档的，None 全部；删除待撤销的不列。"""
        self.purge_deleted()
        args, where = [prefix + "%"], "m.session LIKE ? AND s.deleted_at IS NULL"
        if archived is not None:
            where += " AND COALESCE(s.archived,0)=" + ("1" if archived else "0")
        found = {}
        if query.strip():
            for r in self.search_sessions(query, prefix):
                found.setdefault(r["session"], r)
            hit = set(found)
            if not hit:
                return []
            where += f" AND m.session IN ({','.join('?' * len(hit))})"
            args += list(hit)
        rows = self.db.execute(
            f"""SELECT m.session, MIN(m.ts), MAX(m.ts), COUNT(*),
                   (SELECT content FROM messages f WHERE f.session=m.session AND f.role='user' ORDER BY f.id LIMIT 1),
                   (SELECT content FROM messages l WHERE l.session=m.session AND l.role='assistant' ORDER BY l.id DESC LIMIT 1),
                   s.title, COALESCE(s.pinned,0), MAX(m.id), COALESCE(s.archived,0), COALESCE(s.auto_title,0)
                FROM messages m LEFT JOIN session_meta s ON s.session=m.session
                WHERE {where} GROUP BY m.session ORDER BY COALESCE(s.pinned,0) DESC, MAX(m.id) DESC LIMIT ?""", args + [limit]).fetchall()
        out = []
        for sid, t0, t1, n, first, last, title, pinned, _, arch, auto in rows:
            out.append({"id": sid[len(prefix):], "title": title or " ".join(re.sub(r"\[\[[^\]]*\]\]", "", first or "").split())[:40] or "（无标题）",
                        "started": t0, "updated": t1, "count": n, "pinned": bool(pinned), "archived": bool(arch), "auto_title": bool(auto),
                        "preview": " ".join((last or "").split())[:80]})
            if sid in found:  # 搜索：给出命中的那句话（关键词前后各 ~36 字）和消息 id，网页点开后跳到那里
                f = found[sid]
                out[-1]["match"] = f.get("via")
                mid, snip = self._snippet(sid, query)
                if snip:
                    out[-1].update(hit=snip, hit_id=mid)
                elif f.get("text"):
                    out[-1]["hit"] = " ".join(f["text"].split())[:90]
        return out

    def _snippet(self, session: str, query: str, pad: int = 36) -> tuple[int | None, str]:
        q = query.strip()
        terms = [q] + [w for w in q.split() if len(w) >= 2 and w != q]
        for t in terms:
            r = self.db.execute("SELECT id, content FROM messages WHERE session=? AND role IN ('user','assistant') AND instr(lower(content), lower(?))>0 "
                                "ORDER BY id LIMIT 1", (session, t)).fetchone()
            if r:
                text = " ".join(re.sub(r"\[\[[^\]]*\]\]", "", r[1]).split())
                i = text.lower().find(t.lower())
                if i < 0:
                    continue
                a, b = max(0, i - pad), min(len(text), i + len(t) + pad)
                return r[0], ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")
        return None, ""

    def search_sessions(self, query: str, prefix: str = "web-") -> list[dict]:
        q = query.strip()
        rows = []
        if len(q) >= 3:
            try:
                rows = self.db.execute("SELECT session FROM messages_fts WHERE messages_fts MATCH ? AND session LIKE ? LIMIT 200",
                                       (json.dumps(q, ensure_ascii=False), prefix + "%")).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            rows = self.db.execute("SELECT DISTINCT session FROM messages WHERE content LIKE ? AND session LIKE ? LIMIT 200",
                                   (f"%{q}%", prefix + "%")).fetchall()
            rows += self.db.execute("SELECT session FROM session_meta WHERE title LIKE ? AND session LIKE ?", (f"%{q}%", prefix + "%")).fetchall()
        out = [{"session": r[0], "via": "关键词"} for r in rows]
        try:  # 语义补充：换个说法也能搜到那段对话
            from . import vectors
            have = {r["session"] for r in out}
            for h in vectors.search(self, q, kinds=("msg", "digest"), limit=20):
                if h["session"].startswith(prefix) and h["session"] not in have and h["via"] != "关键词":
                    have.add(h["session"])
                    out.append({"session": h["session"], "via": "语义", "text": h["text"][:120]})
        except Exception:
            pass
        return out

    def history(self, session: str, limit: int = 400, after_id: int = 0) -> list[dict]:
        rows = self.db.execute("SELECT id, ts, role, content FROM messages WHERE session=? AND id>? ORDER BY id DESC LIMIT ?",
                               (session, after_id, limit)).fetchall()
        return [{"id": i, "time": t, "role": r, "text": c} for i, t, r, c in rows[::-1]]

    # ---------- 会话纪要（长对话压缩） ----------
    def digest(self, session: str) -> dict | None:
        r = self.db.execute("SELECT upto_id, text, facts, ts, n_msgs FROM session_digest WHERE session=?", (session,)).fetchone()
        if not r:
            return None
        return {"session": session, "upto_id": r[0], "text": r[1], "facts": json.loads(r[2] or "[]"), "ts": r[3], "n_msgs": r[4] or 0}

    def save_digest(self, session: str, upto_id: int, text: str, facts: list[str], n_msgs: int):
        self.db.execute("INSERT OR REPLACE INTO session_digest(session,upto_id,text,facts,ts,n_msgs) VALUES(?,?,?,?,?,?)",
                        (session, int(upto_id), text, json.dumps(facts, ensure_ascii=False), now(), int(n_msgs)))
        self.db.commit()

    def rename_session(self, session: str, title: str = None, pinned: bool = None, archived: bool = None, auto: bool = False):
        self.db.execute("INSERT OR IGNORE INTO session_meta(session) VALUES(?)", (session,))
        if title is not None:
            self.db.execute("UPDATE session_meta SET title=?, auto_title=? WHERE session=?", (" ".join(title.split())[:60] or None, 1 if auto else 0, session))
        if archived is not None:  # 归档时顺手取消置顶
            self.db.execute("UPDATE session_meta SET archived=?" + (", pinned=0" if archived else "") + " WHERE session=?", (1 if archived else 0, session))
        if pinned is not None:
            self.db.execute("UPDATE session_meta SET pinned=? WHERE session=?", (1 if pinned else 0, session))
        self.db.commit()

    def truncate_turn(self, session: str, turn: int) -> dict | None:
        """改问题 / 重新生成：删掉第 turn 个提问（从 1 数）及其后的全部消息。纪要盖到被删部分时一并删掉纪要（之后按原文重建）。
        返回被删提问 {"id", "text"}；turn 越界返回 None。"""
        ids = [r[0] for r in self.db.execute("SELECT id FROM messages WHERE session=? AND role='user' ORDER BY id", (session,)).fetchall()]
        if turn < 1 or turn > len(ids):
            return None
        cut = ids[turn - 1]
        old = self.db.execute("SELECT content FROM messages WHERE id=?", (cut,)).fetchone()[0]
        self.db.execute("DELETE FROM messages WHERE session=? AND id>=?", (session, cut))
        # FTS 表和 messages 的行号不一一对应：整段重建这一会话的全文索引
        self.db.execute("DELETE FROM messages_fts WHERE session=?", (session,))
        self.db.executemany("INSERT INTO messages_fts(content,session,ts,role) VALUES(?,?,?,?)",
                            self.db.execute("SELECT content, session, ts, role FROM messages WHERE session=? ORDER BY id", (session,)).fetchall())
        d = self.digest(session)
        if d and d["upto_id"] >= cut:
            self.db.execute("DELETE FROM session_digest WHERE session=?", (session,))
        self.db.commit()
        return {"id": cut, "text": old}

    def has_title(self, session: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM session_meta WHERE session=? AND title IS NOT NULL", (session,)).fetchone())

    def soft_delete(self, session: str) -> bool:
        """删除先打标记（列表里立刻消失），UNDO_SECONDS 内可撤销，之后列表刷新或后台清理时真删。"""
        if not self.db.execute("SELECT 1 FROM messages WHERE session=? LIMIT 1", (session,)).fetchone():
            return False
        import time
        self.db.execute("INSERT OR IGNORE INTO session_meta(session) VALUES(?)", (session,))
        self.db.execute("UPDATE session_meta SET deleted_at=? WHERE session=?", (time.time(), session))
        self.db.commit()
        return True

    def restore_session(self, session: str) -> bool:
        n = self.db.execute("UPDATE session_meta SET deleted_at=NULL WHERE session=? AND deleted_at IS NOT NULL", (session,)).rowcount
        self.db.commit()
        return bool(n)

    def purge_deleted(self, older_than: float | None = None) -> int:
        import time
        cut = time.time() - (UNDO_SECONDS if older_than is None else older_than)
        gone = [r[0] for r in self.db.execute("SELECT session FROM session_meta WHERE deleted_at IS NOT NULL AND deleted_at<?", (cut,)).fetchall()]
        for sid in gone:
            self.delete_session(sid)
        return len(gone)

    def delete_session(self, session: str) -> int:
        n = self.db.execute("DELETE FROM messages WHERE session=?", (session,)).rowcount
        self.db.execute("DELETE FROM messages_fts WHERE session=?", (session,))
        self.db.execute("DELETE FROM session_meta WHERE session=?", (session,))
        self.db.execute("DELETE FROM session_digest WHERE session=?", (session,))
        self.db.commit()
        return n

    # ---------- 判断记账与复盘 ----------
    def record_call(self, symbol, name, view, price, horizon_days, reason, confidence=None, due=None, **extra) -> int:
        """extra：source / flip / flip_up / flip_down / expires / report_date（早报判断用）。"""
        due = due or (datetime.now(CN_TZ) + timedelta(days=int(horizon_days))).strftime("%Y-%m-%d")
        try:
            confidence = max(0, min(100, int(confidence))) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence = None
        ok = ("source", "flip", "flip_up", "flip_down", "expires", "report_date", "price_date")
        ex = {k: v for k, v in extra.items() if k in ok and v is not None}
        cols = ["ts", "symbol", "name", "view", "price", "horizon_days", "reason", "due", "confidence", *ex]
        cur = self.db.execute(
            f"INSERT INTO calls({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            (now(), symbol, name, view, price, horizon_days, reason, due, confidence, *ex.values()),
        )
        self.db.commit()
        return cur.lastrowid

    def calls_where(self, where: str, params=()) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM calls WHERE {where} ORDER BY id", params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def set_review(self, cid: int, review: str, note: str, price: float | None, at: str | None = None):
        self.db.execute("UPDATE calls SET review=?, review_note=?, review_price=?, review_at=? WHERE id=?",
                        (review, note, price, at or now(), cid))
        self.db.commit()

    def due_calls(self) -> list[dict]:
        today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
        cols = ["id", "ts", "symbol", "name", "view", "price", "horizon_days", "reason", "due"]
        rows = self.db.execute(
            f"SELECT {','.join(cols)} FROM calls WHERE status='open' AND due<=?", (today,)
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def close_call(self, cid: int, result_price: float, result_pct: float, verdict: str, score: dict | None = None):
        sc = score or {}
        self.db.execute(
            "UPDATE calls SET status='closed', result_price=?, result_pct=?, verdict=?, bench=?, bench_pct=?, alpha_pct=?,"
            " mfe_pct=?, mae_pct=?, edge_pct=?, end_date=? WHERE id=?",
            (result_price, result_pct, verdict, sc.get("bench"), sc.get("bench_pct"), sc.get("alpha"), sc.get("mfe"),
             sc.get("mae"), sc.get("edge"), sc.get("end_date"), cid),
        )
        self.db.commit()

    def recent_calls(self, limit: int = 120) -> list[dict]:
        cols = ["id", "ts", "symbol", "name", "view", "price", "horizon_days", "reason", "due", "status", "result_pct", "verdict",
                "confidence", "bench", "bench_pct", "alpha_pct", "mfe_pct", "mae_pct", "edge_pct", "end_date"]
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
        from . import probatio
        out = {"open": open_n, "closed": len(rows), **agg(rows),
               "区间95%": probatio.wilson(sum(r[5] == "对" for r in rows), len(rows)),
               "按方向": group(lambda r: r[2]), "按市场": group(lambda r: mk(r[1])), "按周期": group(lambda r: hz(r[3] or 0)),
               "最近10次": "".join("✓" if r[5] == "对" else "✗" for r in last), "当前连续": f"{streak}次{sv}"}
        from . import scorecard
        full = self.db.execute("SELECT view, alpha_pct, edge_pct, confidence FROM calls WHERE status='closed'").fetchall()
        sc = scorecard.stats([{"view": v, "alpha": a, "edge": e, "confidence": c} for v, a, e, c in full])
        if sc:
            out["超额口径"] = sc
        weak = [f"{k}={v['准确率%']}%" for grp in ("按方向", "按市场", "按周期") for k, v in out[grp].items() if v["n"] >= 5 and v["准确率%"] < 45]
        if weak:
            out["短板"] = "、".join(weak)
        return out
