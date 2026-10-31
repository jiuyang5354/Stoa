"""语义记忆（v0.17）：向量 + 关键词混合检索。

覆盖：历史对话消息、会话纪要、USER.md / MEMORY.md 条目、判断账本的 reason、Agora 辩论纪要摘要。

嵌入后端（可插拔）：
  remote  已配置大模型服务商的 OpenAI 兼容 /embeddings（providers.EMBED_MODELS 里列了哪些预设支持），
          或单独设 EMBED_PROVIDER / EMBED_MODEL / EMBED_BASE_URL / EMBED_API_KEY。
  local   纯 numpy：中文按字 + 双字、英文按词，哈希到 1024 维的对数词频向量，查询时乘以语料 IDF（真正的 TF-IDF 余弦）；
          另外加「概念词」：股票名字/代码归一到同一个符号（腾讯 = 腾讯控股 = 0700.HK），图谱主题词表（芯片 ≈ 半导体），
          以及一张金融同义词表（止损 ≈ 离场价 ≈ 割肉、成本 ≈ 买入价、偏好 ≈ 风格 ≈ 态度…）。不需要 torch，Windows 包不变大。
  EMBEDDINGS=auto（默认：有支持 embeddings 的 key 就用远端，否则本地）| local | remote | off（只用关键词）。
  本地向量总会建（便宜），远端向量是额外的一份；远端失败时自动退回本地，10 分钟内不再重试。

存储：sessions.db 的 mem_items（条目）+ mem_vecs（float32 blob，按后端分别存）。
检索：BM25（同一套切词）+ 向量余弦，各取前 50，RRF（k=60）融合。
增量：每次检索前 sync() 只补新消息 / 变化的条目；serve 里还有后台线程定时补；`python -m stock_agent memory reindex` 全量重建。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter

import numpy as np

DIM = 1024
RRF_K = 60
LOCK = threading.RLock()
KINDS = {"msg": "对话", "digest": "会话纪要", "user": "关于用户", "memory": "笔记", "call": "判断", "debate": "辩论纪要"}

# 金融同义词表：组内任一词出现就加一个概念词，让「换个说法」也能召回。
SYNONYMS = {
    "持仓": ["持仓", "持有", "仓位", "我买了", "买入了", "买进", "手里", "拿着", "重仓", "建仓", "股数"],
    "成本": ["成本", "买入价", "成本价", "均价", "进价", "建仓价", "买的价", "买进", "多少钱买", "摊薄"],
    "止损": ["止损", "离场", "割肉", "止损位", "止损价", "跌破就卖", "清仓线", "防守位", "stop loss"],
    "止盈": ["止盈", "目标价", "卖出目标", "获利了结", "落袋", "take profit"],
    "偏好": ["偏好", "风格", "态度", "喜欢", "不喜欢", "不碰", "倾向", "习惯", "原则", "规矩"],
    "风险": ["风险偏好", "保守", "稳健", "激进", "杠杆", "回撤", "波动"],
    "估值高": ["高估值", "估值高", "太贵", "很贵", "贵的", "溢价", "泡沫", "市盈率高", "pe 高"],
    "估值低": ["低估", "便宜", "估值低", "安全边际", "破净"],
    "看多": ["看多", "看涨", "看好", "乐观", "上涨", "做多", "bullish", "加仓"],
    "看空": ["看空", "看跌", "悲观", "下跌", "做空", "bearish", "减仓"],
    "决定": ["决定", "打算", "计划", "定了", "就这么办", "我会", "准备"],
    "财报": ["财报", "业绩", "季报", "年报", "中报", "三季报", "earnings", "eps", "营收", "净利"],
    "分红": ["分红", "股息", "派息", "红利", "dividend"],
    "回购": ["回购", "购回", "buyback"],
    "港股": ["港股", "香港", "恒生", ".hk"],
    "美股": ["美股", "纳斯达克", "标普", "美国股票"],
    "A股": ["a股", "沪深", "上证", "深证", "创业板"],
    "互联网": ["互联网", "平台公司", "社交", "游戏", "电商"],
}
CJK_STOP = set("的了是我你他她它们在吗呢吧啊也就都和与及或而但又很再还把被给对从向到这那个之以其")


def _h(tok: str) -> int:
    return int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=4).digest(), "little") % DIM


def tokens(text: str, unigrams: bool = True) -> list[str]:
    """切分：中文单字 + 双字，英文/数字按词（代码 0700.hk 保留点）。本地向量用全部；BM25 用 unigrams=False（单字太噪，只留双字）。"""
    text = re.sub(r"【(?:计划|历史持仓|待确认|已确认持仓)】", "", text or "")
    out = []
    for m in re.finditer(r"[\u3400-\u9fff]+|[a-z0-9$^][a-z0-9.\-]*", (text or "").lower()):
        s = m.group().strip(".-")
        if not s:
            continue
        if "\u3400" <= s[0] <= "\u9fff":
            if unigrams or len(s) == 1:
                out += [c for c in s if c not in CJK_STOP]
            out += [s[i:i + 2] for i in range(len(s) - 1)]
        elif len(s) >= 2 or s.isdigit():
            out.append(s)
    return out


class Concepts:
    """概念词：股票名字/代码 → 符号，主题词表，金融同义词组。"""

    def __init__(self, mem=None):
        self.linker = None
        try:
            from . import graph
            self.linker = graph.Linker(graph._known(mem)) if mem is not None else None
            self.graph = graph
        except Exception:
            self.graph = None

    def of(self, text: str) -> list[str]:
        low = (text or "").lower()
        out = [f"§{k}" for k, ws in SYNONYMS.items() if any(w in low for w in ws)]
        if self.linker is not None:
            try:
                out += [f"§sym:{s.lower()}" for s in self.linker.symbols(text)]
                out += [f"§thm:{t}" for t in self.linker.themes(text)]
            except Exception:
                pass
        return out


def local_tf(text: str, concepts: Concepts | None = None) -> np.ndarray:
    """对数词频哈希向量（未乘 IDF、未归一化）；概念词权重 ×2。"""
    v = np.zeros(DIM, dtype=np.float32)
    c = Counter(tokens(text))
    for t, n in c.items():
        v[_h(t)] += 1.0 + math.log(n)
    for t in set(concepts.of(text) if concepts else []):
        v[_h(t)] += 2.0
    return v


# ---------------------------------------------------------------- 远端嵌入
_REMOTE_FAIL = {"until": 0.0, "error": ""}


def remote_config() -> dict | None:
    """有可用的 OpenAI 兼容 embeddings 就返回 {base, key, model, name}，否则 None。"""
    mode = os.getenv("EMBEDDINGS", "auto").lower()
    if mode in ("local", "off", "0", "false"):
        return None
    from .providers import EMBED_MODELS, LLM
    if os.getenv("EMBED_BASE_URL") or os.getenv("EMBED_PROVIDER"):
        name = (os.getenv("EMBED_PROVIDER") or "openai").lower()
        llm = LLM(name, primary=False) if name in EMBED_MODELS or not os.getenv("EMBED_BASE_URL") else None
        base = (os.getenv("EMBED_BASE_URL") or (llm.base if llm else "")).rstrip("/")
        key = os.getenv("EMBED_API_KEY") or (llm.key if llm else None) or os.getenv("LLM_API_KEY")
        model = os.getenv("EMBED_MODEL") or EMBED_MODELS.get(name)
        return {"base": base, "key": key or "none", "model": model, "name": f"{name}:{model}"} if base and model else None
    try:
        from .agent import load_cfg
        llm = LLM(cfg=load_cfg().get("llm", {}))
    except Exception:
        return None
    model = os.getenv("EMBED_MODEL") or EMBED_MODELS.get(llm.name)
    if not model or not llm.key or llm.proto not in ("openai",):
        return None
    return {"base": llm.base, "key": llm.key, "model": model, "name": f"{llm.name}:{model}"}


def remote_embed(texts: list[str], cfg: dict, timeout: int = 30) -> np.ndarray:
    out = []
    for i in range(0, len(texts), 64):
        batch = [t[:2000] or " " for t in texts[i:i + 64]]
        import requests
        r = requests.post(f"{cfg['base']}/embeddings", json={"model": cfg["model"], "input": batch},
                          headers={"Authorization": f"Bearer {cfg['key']}"}, timeout=timeout)
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        out += [d["embedding"] for d in data]
    m = np.asarray(out, dtype=np.float32)
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    return m


def remote_ready() -> dict | None:
    if time.time() < _REMOTE_FAIL["until"]:
        return None
    return remote_config()


def _remote_failed(e):
    _REMOTE_FAIL.update(until=time.time() + 600, error=str(e)[:120])


def backend_status() -> dict:
    rc = remote_config()
    return {"mode": os.getenv("EMBEDDINGS", "auto").lower(), "remote": rc["name"] if rc else None,
            "remote_paused": time.time() < _REMOTE_FAIL["until"], "remote_error": _REMOTE_FAIL["error"] or None, "local": f"tfidf-hash-{DIM}"}


# ---------------------------------------------------------------- 索引
def ensure(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mem_items(id INTEGER PRIMARY KEY, kind TEXT, ref TEXT, session TEXT, ts TEXT, text TEXT, meta TEXT, hash TEXT,
            UNIQUE(kind, ref));
        CREATE TABLE IF NOT EXISTS mem_vecs(item INTEGER, backend TEXT, dim INTEGER, v BLOB, PRIMARY KEY(item, backend));
        CREATE TABLE IF NOT EXISTS mem_index_state(key TEXT PRIMARY KEY, value TEXT);
    """)


def _clean(text: str) -> str:
    from .cite import strip_sources
    t = strip_sources(text or "")
    t = re.sub(r"\[\[(?:debate|cards):[^\]]*\]\]", "", t)
    return " ".join(t.split())[:1500]


def _hash(text: str, meta: str = "") -> str:
    return hashlib.sha1((text + "|" + meta).encode("utf-8")).hexdigest()[:16]


def _state(db, key, val=None):
    if val is None:
        r = db.execute("SELECT value FROM mem_index_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else None
    db.execute("INSERT OR REPLACE INTO mem_index_state(key,value) VALUES(?,?)", (key, str(val)))


def _upsert(db, kind, ref, text, ts="", session="", meta=None, concepts=None) -> int | None:
    """写一个条目 + 本地向量；内容没变就跳过。返回需要补远端向量的 item id。"""
    meta_s = json.dumps(meta or {}, ensure_ascii=False)
    h = _hash(text, meta_s)
    row = db.execute("SELECT id, hash FROM mem_items WHERE kind=? AND ref=?", (kind, str(ref))).fetchone()
    if row and row[1] == h:
        return None
    if row:
        iid = row[0]
        db.execute("UPDATE mem_items SET text=?, ts=?, session=?, meta=?, hash=? WHERE id=?", (text, ts, session, meta_s, h, iid))
        db.execute("DELETE FROM mem_vecs WHERE item=?", (iid,))
    else:
        iid = db.execute("INSERT INTO mem_items(kind,ref,session,ts,text,meta,hash) VALUES(?,?,?,?,?,?,?)",
                         (kind, str(ref), session, ts, text, meta_s, h)).lastrowid
    v = local_tf(text, concepts)
    db.execute("INSERT OR REPLACE INTO mem_vecs(item,backend,dim,v) VALUES(?,?,?,?)", (iid, "local", DIM, v.tobytes()))
    return iid


def _drop_missing(db, kind, keep_refs: set[str]):
    rows = db.execute("SELECT id, ref FROM mem_items WHERE kind=?", (kind,)).fetchall()
    gone = [i for i, r in rows if r not in keep_refs]
    for i in gone:
        db.execute("DELETE FROM mem_items WHERE id=?", (i,))
        db.execute("DELETE FROM mem_vecs WHERE item=?", (i,))
    return len(gone)


def sync(mem, full: bool = False, remote: bool = True) -> dict:
    """增量建索引。full=True 时清空重建。返回各类新增/更新条数。"""
    db = mem.db
    stats = Counter()
    with LOCK:
        ensure(db)
        if full:
            db.execute("DELETE FROM mem_items")
            db.execute("DELETE FROM mem_vecs")
            db.execute("DELETE FROM mem_index_state")
        concepts = Concepts(mem)
        todo = []
        # 1) 对话消息（只补 id 更大的）
        last = int(_state(db, "msg_last") or 0)
        rows = db.execute("SELECT id, session, ts, role, content FROM messages WHERE id>? AND role IN ('user','assistant') ORDER BY id LIMIT 5000",
                          (last,)).fetchall()
        for mid, sess, ts, role, content in rows:
            text = _clean(content)
            if len(text) >= 4:
                iid = _upsert(db, "msg", mid, text, ts, sess, {"role": role}, concepts)
                if iid:
                    todo.append(iid)
                    stats["msg"] += 1
            last = mid
        if rows:
            _state(db, "msg_last", last)
        # 删掉已删除会话的消息条目
        db.execute("DELETE FROM mem_vecs WHERE item IN (SELECT i.id FROM mem_items i LEFT JOIN messages m ON m.id=CAST(i.ref AS INTEGER) "
                   "WHERE i.kind='msg' AND m.id IS NULL)")
        stats["dropped"] += db.execute("DELETE FROM mem_items WHERE kind='msg' AND CAST(ref AS INTEGER) NOT IN (SELECT id FROM messages)").rowcount
        # 2) 记忆条目
        for target in ("user", "memory"):
            keep = set()
            for e in mem.entries(target):
                ref = _hash(e)
                keep.add(ref)
                m = re.match(r"^\[(\d{4}-\d{2}-\d{2})\]", e)
                iid = _upsert(db, target, ref, e, m.group(1) if m else "", "", {}, concepts)
                if iid:
                    todo.append(iid)
                    stats[target] += 1
            stats["dropped"] += _drop_missing(db, target, keep)
        # 3) 判断账本
        keep = set()
        for c in mem.recent_calls(5000):
            keep.add(str(c["id"]))
            text = f"{c.get('name') or ''} {c['symbol']} {c['view']} {c['horizon_days']}天：{c.get('reason') or ''}"
            meta = {"symbol": c["symbol"], "view": c["view"], "verdict": c.get("verdict") or ("未到期" if c.get("status") == "open" else ""),
                    "price": c.get("price"), "result_pct": c.get("result_pct")}
            iid = _upsert(db, "call", c["id"], text, str(c["ts"])[:16], "", meta, concepts)
            if iid:
                todo.append(iid)
                stats["call"] += 1
        stats["dropped"] += _drop_missing(db, "call", keep)
        # 4) 会话纪要
        keep = set()
        try:
            for sess, upto, text, ts in db.execute("SELECT session, upto_id, text, ts FROM session_digest").fetchall():
                keep.add(sess)
                iid = _upsert(db, "digest", sess, _clean(text), ts, sess, {"upto_id": upto}, concepts)
                if iid:
                    todo.append(iid)
                    stats["digest"] += 1
        except Exception:
            pass
        stats["dropped"] += _drop_missing(db, "digest", keep)
        # 5) 辩论纪要摘要
        try:
            from . import debate
            keep = set()
            for d in debate.listing(500):
                keep.add(d["id"])
                if db.execute("SELECT 1 FROM mem_items WHERE kind='debate' AND ref=?", (d["id"],)).fetchone() and not full:
                    continue
                rec = debate.load(d["id"]) or {}
                v = rec.get("verdict") or {}
                text = (f"{rec.get('name') or ''} {rec.get('symbol') or ''} Agora 辩论：{v.get('view', '')} 置信度 {v.get('confidence', '')}。"
                        f"{v.get('summary') or ''} 依据：{'；'.join(v.get('key_points') or [])} 失效条件：{v.get('invalidation') or ''}")
                iid = _upsert(db, "debate", d["id"], _clean(text), str(rec.get("created") or d.get("created") or "")[:16], "",
                              {"symbol": rec.get("symbol"), "view": v.get("view")}, concepts)
                if iid:
                    todo.append(iid)
                    stats["debate"] += 1
            stats["dropped"] += _drop_missing(db, "debate", keep)
        except Exception:
            pass
        if sum(stats.values()):
            _state(db, "rev", int(_state(db, "rev") or 0) + 1)
        db.commit()
    # 远端向量：把还没有远端向量的条目一次补上（失败就记下，暂停 10 分钟）
    rc = remote_ready() if remote else None
    if rc:
        try:
            stats["remote"] = _fill_remote(db, rc)
        except Exception as e:
            _remote_failed(e)
            stats["remote_error"] = 1
    if sum(stats.values()):
        _CACHE.clear()
    stats.pop("dropped", None) if not stats.get("dropped") else None
    return dict(stats)


def _fill_remote(db, rc, limit: int = 2000) -> int:
    with LOCK:
        rows = db.execute("SELECT i.id, i.text FROM mem_items i WHERE NOT EXISTS (SELECT 1 FROM mem_vecs v WHERE v.item=i.id AND v.backend=?) "
                          "ORDER BY i.id DESC LIMIT ?", (rc["name"], limit)).fetchall()
    if not rows:
        return 0
    m = remote_embed([t for _, t in rows], rc)
    with LOCK:
        for (iid, _), v in zip(rows, m):
            db.execute("INSERT OR REPLACE INTO mem_vecs(item,backend,dim,v) VALUES(?,?,?,?)", (iid, rc["name"], len(v), v.astype(np.float32).tobytes()))
        db.commit()
    return len(rows)


# ---------------------------------------------------------------- 检索
_CACHE: dict = {}


def _load(db, backend: str):
    """把某个后端的全部向量 + 条目读进内存（按条目表的版本号缓存）。"""
    with LOCK:
        ver = (db.execute("SELECT COUNT(*), COALESCE(MAX(id),0) FROM mem_items").fetchone(), _state(db, "rev"))
        nv = db.execute("SELECT COUNT(*) FROM mem_vecs WHERE backend=?", (backend,)).fetchone()[0]
        key = (id(db), backend)
        c = _CACHE.get(key)
        if c and c["ver"] == (ver, nv):
            return c
        rows = db.execute("SELECT i.id, i.kind, i.ref, i.session, i.ts, i.text, i.meta, v.v FROM mem_items i JOIN mem_vecs v ON v.item=i.id AND v.backend=? "
                          "ORDER BY i.id", (backend,)).fetchall()
    items = [{"id": r[0], "kind": r[1], "ref": r[2], "session": r[3] or "", "ts": r[4] or "", "text": r[5] or "", "meta": json.loads(r[6] or "{}")} for r in rows]
    mat = np.frombuffer(b"".join(r[7] for r in rows), dtype=np.float32).reshape(len(rows), -1) if rows else np.zeros((0, DIM), np.float32)
    toks = [Counter(tokens(it["text"], unigrams=False)) for it in items]
    c = {"ver": (ver, nv), "items": items, "mat": mat, "toks": toks}
    if backend == "local" and len(items):
        df = (mat > 0).sum(0)
        c["idf"] = np.log((1 + len(items)) / (1 + df)).astype(np.float32) + 1.0
        w = mat * c["idf"]
        c["norm"] = w / np.maximum(np.linalg.norm(w, axis=1, keepdims=True), 1e-9)
    _CACHE[key] = c
    return c


def bm25(query: str, docs: list[Counter], k1: float = 1.4, b: float = 0.75) -> np.ndarray:
    q = set(tokens(query, unigrams=False))
    n = len(docs)
    if not q or not n:
        return np.zeros(n)
    lens = np.array([sum(d.values()) for d in docs], dtype=np.float32)
    avg = float(lens.mean()) or 1.0
    df = Counter(t for d in docs for t in q if t in d)
    s = np.zeros(n, dtype=np.float32)
    for t in q:
        if not df[t]:
            continue
        idf = math.log(1 + (n - df[t] + .5) / (df[t] + .5))
        tf = np.array([d.get(t, 0) for d in docs], dtype=np.float32)
        s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lens / avg))
    return s


def rrf(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion：每个排序里第 r 名（从 1 起）贡献 1/(k+r)。"""
    out: dict[int, float] = {}
    for rk in rankings:
        for r, i in enumerate(rk, 1):
            out[i] = out.get(i, 0.0) + 1.0 / (k + r)
    return out


def search(mem, query: str, kinds=None, limit: int = 8, session: str | None = None, exclude_refs=(), do_sync: bool = True,
           min_sim: float | None = None) -> list[dict]:
    """混合检索。kinds 限定类别（msg/digest/user/memory/call/debate）；exclude_refs 排除某些消息 id（如刚写入的当前提问）。"""
    q = (query or "").strip()
    if not q:
        return []
    if do_sync:
        try:
            sync(mem)
        except Exception:
            pass
    db = mem.db
    with LOCK:
        ensure(db)
    loc = _load(db, "local")
    if not loc["items"]:
        return []
    items = loc["items"]
    kinds = set(kinds) if kinds else None
    excl = {str(x) for x in exclude_refs}
    mask = np.array([(not kinds or it["kind"] in kinds) and (session is None or it["session"] == session)
                     and not (it["kind"] == "msg" and it["ref"] in excl) for it in items], dtype=bool)
    if not mask.any():
        return []
    # 关键词
    kw = bm25(q, loc["toks"])
    kw[~mask] = 0
    kw_rank = [int(i) for i in np.argsort(-kw)[:50] if kw[i] > 0]
    # 向量：远端可用且索引齐就用远端，否则本地 TF-IDF
    sims, backend = None, "local"
    if os.getenv("EMBEDDINGS", "auto").lower() not in ("off", "0", "false"):
        rc = remote_ready()
        if rc:
            try:
                rem = _load(db, rc["name"])
                if len(rem["items"]) >= .9 * len(items):
                    qv = remote_embed([q], rc, timeout=15)[0]
                    rs = rem["mat"] @ qv
                    pos = {it["id"]: j for j, it in enumerate(rem["items"])}
                    sims = np.array([rs[pos[it["id"]]] if it["id"] in pos else 0.0 for it in items], dtype=np.float32)
                    backend = rc["name"]
            except Exception as e:
                _remote_failed(e)
                sims = None
        if sims is None:
            qv = local_tf(q, Concepts(mem)) * loc["idf"]
            qv /= max(float(np.linalg.norm(qv)), 1e-9)
            sims = loc["norm"] @ qv
    if sims is not None:
        thr = min_sim if min_sim is not None else (0.1 if backend == "local" else 0.3)
        sims = np.where(mask, sims, -1)
        vec_rank = [int(i) for i in np.argsort(-sims)[:50] if sims[i] >= thr]
    else:
        vec_rank, sims = [], np.zeros(len(items))
    fused = rrf([kw_rank, vec_rank])
    kws, vs = set(kw_rank), set(vec_rank)
    out = []
    for i, sc in sorted(fused.items(), key=lambda x: -x[1])[:limit]:
        it = items[i]
        via = "关键词+语义" if i in kws and i in vs else "关键词" if i in kws else "语义"
        out.append({**{k: it[k] for k in ("kind", "ref", "session", "ts", "text", "meta")}, "label": KINDS.get(it["kind"], it["kind"]),
                    "score": round(sc, 5), "sim": round(float(sims[i]), 3), "via": via, "backend": backend})
    return out


def similar_pairs(mem, kinds=("user", "memory", "call", "debate"), top: int = 2, min_sim: float = 0.1) -> list[tuple[dict, dict, float]]:
    """图谱用：条目之间的语义近邻（每个条目取最相近的 top 个，相似度 ≥ min_sim）。"""
    try:
        sync(mem, remote=False)
    except Exception:
        pass
    loc = _load(mem.db, "local")
    idx = [j for j, it in enumerate(loc["items"]) if it["kind"] in kinds]
    if len(idx) >= 2:  # 记忆条目开头的日期对相似度没意义：去掉日期再算
        cc = Concepts(mem)
        tf = np.stack([local_tf(re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", loc["items"][j]["text"]), cc) for j in idx]) * loc["idf"]
        m = tf / np.maximum(np.linalg.norm(tf, axis=1, keepdims=True), 1e-9)
    else:
        return []
    s = m @ m.T
    np.fill_diagonal(s, -1)
    pairs, seen = [], set()
    for a in range(len(idx)):
        for b in np.argsort(-s[a])[:top]:
            if s[a, b] < min_sim:
                break
            key = tuple(sorted((a, int(b))))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((loc["items"][idx[a]], loc["items"][idx[int(b)]], round(float(s[a, b]), 3)))
    return pairs


def stats(mem) -> dict:
    with LOCK:
        ensure(mem.db)
        rows = mem.db.execute("SELECT kind, COUNT(*) FROM mem_items GROUP BY kind").fetchall()
        bk = mem.db.execute("SELECT backend, COUNT(*) FROM mem_vecs GROUP BY backend").fetchall()
    return {"items": dict(rows), "vectors": dict(bk), "backend": backend_status()}


def indexer_loop(mem, log=print, every: int = 120):
    """serve 后台：定时补索引（新消息、记忆改动、新判断、新辩论）。"""
    time.sleep(20)
    while True:
        try:
            s = sync(mem)
            if sum(v for k, v in s.items() if k not in ("remote_error",)) and os.getenv("STOA_DEBUG"):
                log("语义索引", s)
        except Exception as e:
            log("语义索引出错", e)
        time.sleep(every)
