"""本地日线库（SQLite）：把用过的日线落到 `data/bars.db`，供回测、因子、区间统计反复查询。

**不引入新依赖**：全仓其它模块都用 `sqlite3`；Parquet/DuckDB 那条路要同花顺的全市场导出
（需要 API Key），本期不做。取数仍走既有链路：`edgar._daily_rows` →
A 股配了 `HITHINK_API_KEY` 就用同花顺官方日线，否则 Yahoo，再不行走原来的腾讯/SEC 备用。

命令行：

    python -m stock_agent.marketdb --sync 600519.SS,0700.HK,AAPL --days 400
    python -m stock_agent.marketdb --stats
    python -m stock_agent.marketdb --sql "select symbol, count(*) n, min(date) a, max(date) b from bars group by 1"

写库只发生在 `--sync`；`--stats` / `--sql` 只读。重复同步是幂等的（按 symbol+date 覆盖）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta

DB_NAME = "bars.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL, high REAL, low REAL, close REAL,
    volume      REAL, turnover REAL,
    source      TEXT, currency TEXT, fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS bars_date ON bars(date);
CREATE TABLE IF NOT EXISTS sync_state (
    symbol      TEXT PRIMARY KEY,
    first_date  TEXT, last_date TEXT, rows INTEGER,
    source      TEXT, updated_at TEXT NOT NULL
);
"""


def db_path(home=None):
    from .memory import home as _home
    return (home or _home()) / "data" / DB_NAME


def connect(home=None, path=None) -> sqlite3.Connection:
    p = path or db_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return c


def upsert(conn: sqlite3.Connection, symbol: str, rows: list[dict], source: str = "", currency: str = "") -> int:
    """写入/覆盖一段日线，返回实际写入的行数（同日重复同步不会膨胀）。"""
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    payload = [(symbol, str(r.get("date"))[:10], r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                r.get("volume"), r.get("turnover"), source, currency, now)
               for r in rows if r.get("date")]
    conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?)", payload)
    first = min(p[1] for p in payload)
    last = max(p[1] for p in payload)
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?,?,?)",
                 (symbol, first, last, len(payload), source, now))
    conn.commit()
    return len(payload)


def sync(symbols, days: int = 400, end: str | None = None, home=None, fetcher=None) -> dict:
    """按符号补日线。fetcher(symbol, d0, d1, raw) 默认用 edgar._daily_rows，测试可注入。"""
    from . import edgar
    fetch = fetcher or edgar._daily_rows
    end_d = (date.fromisoformat(end) if end else date.today())
    start_d = end_d - timedelta(days=int(max(1, days) * 1.6) + 10)
    got = {}
    conn = connect(home)
    try:
        for sym in [s for s in (symbols or []) if s]:
            try:
                rows, meta = fetch(sym, start_d.isoformat(), end_d.isoformat(), False)
                n = upsert(conn, sym, rows or [], str(meta.get("source") or ""), str(meta.get("currency") or ""))
                got[sym] = {"rows": n, "first": min((r["date"] for r in rows), default=None),
                            "last": max((r["date"] for r in rows), default=None),
                            "source": meta.get("source")}
            except Exception as e:  # 单只失败不影响其它标的
                got[sym] = {"rows": 0, "error": str(e)[:120]}
    finally:
        conn.close()
    return got


def bars(symbol: str, start: str | None = None, end: str | None = None, limit: int | None = None, home=None) -> list[dict]:
    """按日期升序取本地日线；库里没有就返回空列表（不联网）。"""
    conn = connect(home)
    try:
        sql = "SELECT date, open, high, low, close, volume, turnover, source FROM bars WHERE symbol=?"
        args: list = [symbol]
        if start:
            sql += " AND date>=?"; args.append(start)
        if end:
            sql += " AND date<=?"; args.append(end)
        sql += " ORDER BY date"
        if limit:
            sql += " DESC LIMIT ?"
            args.append(int(limit))
            return list(reversed([dict(r) for r in conn.execute(sql, args)]))
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def stats(home=None) -> dict:
    conn = connect(home)
    try:
        row = conn.execute("SELECT count(*) n, count(DISTINCT symbol) syms, min(date) a, max(date) b FROM bars").fetchone()
        top = [dict(r) for r in conn.execute(
            "SELECT symbol, count(*) rows, min(date) first, max(date) last, max(source) source "
            "FROM bars GROUP BY symbol ORDER BY rows DESC LIMIT 10")]
        return {"symbols": row["syms"], "rows": row["n"], "first": row["a"], "last": row["b"],
                "db": str(db_path(home)), "top": top}
    finally:
        conn.close()


def query(sql: str, home=None, params=()) -> list[dict]:
    conn = connect(home)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m stock_agent.marketdb", description="本地日线库（SQLite）")
    p.add_argument("--sync", help="要补的标的，逗号分隔（Yahoo 代码：600519.SS / 0700.HK / AAPL）")
    p.add_argument("--days", type=int, default=400, help="回补多少天，默认 400（自然日）")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD，默认今天")
    p.add_argument("--stats", action="store_true", help="看库里有什么")
    p.add_argument("--sql", help="只读查询，如 select * from bars limit 5")
    a = p.parse_args(argv)
    if a.sync:
        got = sync([s.strip() for s in a.sync.split(",") if s.strip()], days=a.days, end=a.end)
        print(json.dumps(got, ensure_ascii=False, indent=2))
    if a.stats or not (a.sync or a.sql):
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    if a.sql:
        try:
            rows = query(a.sql)
        except sqlite3.Error as e:
            print(f"SQL 出错：{e}")
            return 2
        print(json.dumps(rows[:200], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
