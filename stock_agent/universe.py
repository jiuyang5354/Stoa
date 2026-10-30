"""股票池：沪深 300 历史成分（防幸存者偏差）+ 行业分类（行业中性 IC 用）。全部免 key。

- 成分：中证指数官网 000300cons.xls 只给「当前」成分；历史成分从 Internet Archive（web.archive.org）里找决策期开始之前
  最近的一份快照（文件里自带「日期Date」列，即成分生效的那个交易日）。找不到才退回当前成分，并在结果里注明
  survivorship=current（有幸存者偏差）。
- 抽样：按 md5(seed|代码) 排序取前 n 只，与行情、结果无关，可复现。
- 行业：东方财富 datacenter-web RPT_F10_ORG_BASICINFO 的 EM2016（取一级，如「食品饮料」）。这是当前分类，不是
  point-in-time 的（个股行业极少变动，残余偏差很小，结果里注明）。

缓存：<数据目录>/data/universe/000300cons_<YYYYMMDD>.xls、industry_em2016.json。
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pandas as pd
import requests

CONS_URL = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{code}cons.xls"
WAYBACK = "http://archive.org/wayback/available"
EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = {"User-Agent": "Mozilla/5.0 (Stoa research)"}


def udir() -> Path:
    from .memory import home
    d = home() / "data" / "universe"
    d.mkdir(parents=True, exist_ok=True)
    return d


def yahoo_symbol(code) -> str:
    c = str(code).strip().zfill(6)
    if c.startswith(("6", "9")):
        return f"{c}.SS"
    if c.startswith(("4", "8")):
        return f"{c}.BJ"
    return f"{c}.SZ"


def parse_cons(raw: bytes) -> tuple[str, list[dict]]:
    """000300cons.xls → (成分日期 YYYYMMDD, [{symbol, name}])。"""
    df = pd.read_excel(io.BytesIO(raw))
    dcol = next(c for c in df.columns if str(c).startswith("日期"))
    ccol = next(c for c in df.columns if "成份券代码" in str(c) or "Constituent Code" in str(c))
    ncol = next(c for c in df.columns if "成份券名称" in str(c) and "英文" not in str(c))
    date = str(int(df[dcol].iloc[0]))
    return date, [{"symbol": yahoo_symbol(c), "name": str(n)} for c, n in zip(df[ccol], df[ncol])]


def _cached(code: str) -> list[Path]:
    return sorted(udir().glob(f"{code}cons_*.xls"))


def members(asof: str, code: str = "000300", online: bool = True) -> dict:
    """决策期开始日 asof 时的成分。返回 {date, members, source, survivorship}。
    survivorship = "historical"（成分日期 ≤ asof）或 "current"（只拿到更晚的，存在幸存者偏差）。"""
    want = pd.Timestamp(asof).strftime("%Y%m%d")
    best = None
    for p in _cached(code):
        d = p.stem.split("_")[-1]
        if d <= want and (best is None or d > best[0]):
            best = (d, p)
    if best is None and online:
        try:
            r = requests.get(WAYBACK, params={"url": CONS_URL.format(code=code).split("://")[1], "timestamp": want}, timeout=20).json()
            snap = (r.get("archived_snapshots") or {}).get("closest") or {}
            if snap.get("available") and snap.get("timestamp", "99") <= want + "999999":
                raw = requests.get(snap["url"].replace(f"/{snap['timestamp']}/", f"/{snap['timestamp']}id_/"), timeout=60, headers=UA).content
                d, _ = parse_cons(raw)
                if d <= want:
                    p = udir() / f"{code}cons_{d}.xls"
                    p.write_bytes(raw)
                    best = (d, p)
        except Exception:
            pass
    if best:
        d, mem = parse_cons(best[1].read_bytes())
        return {"date": d, "members": mem, "source": f"中证指数 {code}cons.xls（{d} 生效，Internet Archive 快照）", "survivorship": "historical"}
    # 退回当前成分
    raw = None
    for p in reversed(_cached(code)):
        raw = p.read_bytes()
        break
    if online:
        try:
            raw = requests.get(CONS_URL.format(code=code), timeout=30, headers=UA).content
            d, _ = parse_cons(raw)
            (udir() / f"{code}cons_{d}.xls").write_bytes(raw)
        except Exception:
            pass
    if raw is None:
        raise RuntimeError("拿不到沪深 300 成分（中证指数官网与 Internet Archive 都不可达）")
    d, mem = parse_cons(raw)
    return {"date": d, "members": mem, "source": f"中证指数 {code}cons.xls（当前成分 {d}）", "survivorship": "current"}


def sample(mem: list[dict], n: int, seed: int = 7) -> list[dict]:
    """与行情无关的确定性抽样。"""
    return sorted(mem, key=lambda m: hashlib.md5(f"{seed}|{m['symbol']}".encode()).hexdigest())[:n]


def _em_secucode(sym: str) -> str:
    c, ex = sym.split(".")
    return f"{c}.{'SH' if ex == 'SS' else ex}"


def industries(symbols: list[str], online: bool = True) -> dict:
    """{symbol: 一级行业}；拿不到的不出现在结果里。"""
    p = udir() / "industry_em2016.json"
    cache = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    need = [s for s in symbols if s not in cache and s.endswith((".SS", ".SZ", ".BJ"))]
    if need and online:
        for i in range(0, len(need), 50):
            chunk = need[i:i + 50]
            flt = "(SECUCODE in (" + ",".join(f'"{_em_secucode(s)}"' for s in chunk) + "))"
            try:
                j = requests.get(EM_URL, params={"reportName": "RPT_F10_ORG_BASICINFO", "columns": "SECUCODE,SECURITY_NAME_ABBR,EM2016",
                                                 "filter": flt, "pageSize": 100}, timeout=20, headers=UA).json()
                for row in ((j.get("result") or {}).get("data") or []):
                    code, ex = row["SECUCODE"].split(".")
                    sym = f"{code}.{'SS' if ex == 'SH' else ex}"
                    if row.get("EM2016"):
                        cache[sym] = str(row["EM2016"]).split("-")[0]
            except Exception:
                continue
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return {s: cache[s] for s in symbols if s in cache}


def csi300_sample(asof: str, n: int = 100, seed: int = 7, online: bool = True) -> dict:
    m = members(asof, online=online)
    pick = sample(m["members"], n, seed)
    syms = [x["symbol"] for x in pick]
    ind = industries(syms, online=online)
    return {**{k: m[k] for k in ("date", "source", "survivorship")}, "n_members": len(m["members"]), "symbols": syms,
            "names": {x["symbol"]: x["name"] for x in pick}, "industries": ind,
            "industry_source": "东方财富 EM2016 一级行业（当前分类，非 point-in-time）" if ind else None}
