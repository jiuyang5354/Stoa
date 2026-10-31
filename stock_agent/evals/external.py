"""外部公开评测：Vals Finance Agent v2 公开题（27）与 FinSearchComp（T2/T3 抽样）。

同一个模型跑两遍：bare（不给工具，一次回答）和 stoa（完整 Stoa Agent），差值就是「框架加分」。
评分：
- Vals：逐条 rubric 让评委判过/不过；must_pass 全过才拿分，部分分 = 过的严重度之和 / 总严重度；all-pass = 全过。
  官方是三模型评委团 + 私有测试集，这里是单评委 + 公开题，数字只能做方向参考。
- FinSearchComp：原样用数据集自带的评委提示和模板，0/1。T1 实时题的标准答案是 2025 年当时的价格，跳过。

用法：python -m stock_agent.evals.external --suite vals|fsc --mode bare|stoa [--n 10] [--data DIR] [--out DIR]
结果逐题追加到 out/<suite>-<mode>.jsonl，已有的题自动跳过（可断点续跑）。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

VALS_URL = "https://raw.githubusercontent.com/vals-ai/finance-agent-v2/main/data/public.csv"
FSC_URL = "https://raw.githubusercontent.com/randomtutu/FinSearchComp/main/data/finsearchcomp_data.json"

VALS_SYS = """You are a financial agent. You will not be able to interact with the user or ask clarifications.
Today's date is March 1, 2026.
Give one conclusive answer for each thing the question asks for. Provide calculated answers to at least two decimal places and do not round intermediate steps.
SEC filings are the most authoritative source of financial data. Report figures in the same scale and units as the SEC filing unless otherwise specified.
Show your reasoning and calculations, then list your sources. Answer in English."""


def _get(url: str, path: Path) -> Path:
    if not path.exists():
        import requests
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def load_vals(data: Path) -> list[dict]:
    csv.field_size_limit(10 ** 9)
    rows = list(csv.DictReader(open(_get(VALS_URL, data / "vals_public.csv"), encoding="utf-8")))
    return [{"id": f"vals-{i:02d}", "type": r["Question Type"], "question": r["Question"],
             "rubric": json.loads(r["Rubric"])} for i, r in enumerate(rows)]


def load_fsc(data: Path, n: int = 10, seed: int = 7) -> list[dict]:
    d = json.load(open(_get(FSC_URL, data / "finsearchcomp.json"), encoding="utf-8"))
    out = []
    for task in ("(T2)", "(T3)"):
        for region in ("Global", "Greater China"):
            pool = [x for x in d if x["prompt_id"].startswith(task) and f"({region})" in x["label"]]
            random.Random(seed).shuffle(pool)
            for x in pool[:n]:
                out.append({"id": x["prompt_id"], "type": f"{task} {region}", "question": x["prompt"],
                            "reference": x["response_reference"], "judge_sys": x["judge_system_prompt"],
                            "judge_tpl": x["judge_prompt_template"]})
    return out


def _llm():
    from ..providers import Router
    from ..agent import load_cfg
    r = Router(load_cfg().get("llm", {}))
    if not r.ready:
        raise SystemExit("没有可用的大模型。")
    return r


def answer_bare(item: dict, suite: str, llm) -> tuple[str, dict]:
    sys_p = VALS_SYS if suite == "vals" else "你是金融分析师。不能联网，凭已有知识尽力回答，给出明确的最终答案。"
    t = llm.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": item["question"]}], None, temperature=0, timeout=300)
    return t.get("content") or "", {"tools": []}


def answer_stoa(item: dict, suite: str, base_home: Path) -> tuple[str, dict]:
    from ..memory import Memory
    from ..agent import Agent
    tmp = Path(tempfile.mkdtemp(prefix="stoa-ext-"))
    for f in ("config.yaml", ".env"):
        if (base_home / f).exists():
            shutil.copy(base_home / f, tmp / f)
    old = os.environ.get("STOCK_AGENT_HOME")
    os.environ["STOCK_AGENT_HOME"] = str(tmp)
    tools = []
    try:
        a = Agent(mem=Memory(tmp), channel="bench")
        q = item["question"] if suite != "vals" else VALS_SYS + "\n\nQuestion:\n" + item["question"]
        ans = a.ask(q, on_tool=lambda name, args, *x: tools.append(name))
        meta = getattr(a, "last_meta", {}) or {}
        steps = [s.get("tool") or s.get("name") for s in (meta.get("steps") or []) if isinstance(s, dict)]
        return ans, {"tools": tools or [s for s in steps if s]}
    finally:
        if old is None:
            os.environ.pop("STOCK_AGENT_HOME", None)
        else:
            os.environ["STOCK_AGENT_HOME"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def judge_vals(item: dict, ans: str, llm) -> dict:
    crit = [c["criteria"] for c in item["rubric"]]
    prompt = ("You are grading a financial analyst's answer against a rubric. For each criterion, decide whether the answer "
              "clearly satisfies it (numbers must match within normal rounding; a criterion stated as something the answer must "
              "NOT do passes only if the answer avoids it). Output only JSON: {\"pass\": [true/false, ...]} with one entry per criterion in order.\n\n"
              f"Question:\n{item['question']}\n\nAnswer:\n{ans[:12000]}\n\nCriteria:\n" +
              "\n".join(f"{i+1}. {c}" for i, c in enumerate(crit)))
    for _ in range(3):
        try:
            t = llm.chat([{"role": "user", "content": prompt}], None, temperature=0, timeout=300).get("content") or ""
            p = json.loads(re.search(r"\{.*\}", t, re.S).group(0))["pass"]
            if len(p) == len(crit):
                break
        except Exception:
            p = None
    if not p or len(p) != len(crit):
        return {"error": "judge failed"}
    sev = [float((c.get("modifiers") or {}).get("severity", 1)) for c in item["rubric"]]
    must = [(c.get("modifiers") or {}).get("category") == "must_pass" for c in item["rubric"]]
    gate = all(ok for ok, m in zip(p, must) if m)
    partial = (sum(s for ok, s in zip(p, sev) if ok) / sum(sev)) if gate else 0.0
    return {"pass": p, "partial": round(partial, 4), "all_pass": all(p), "must_ok": gate}


def judge_fsc(item: dict, ans: str, llm) -> dict:
    user = item["judge_tpl"].format(prompt=item["question"], response_reference=item["reference"], response=ans[:8000])
    for _ in range(3):
        try:
            t = llm.chat([{"role": "system", "content": item["judge_sys"]}, {"role": "user", "content": user}], None, temperature=0, timeout=300).get("content") or ""
            m = re.findall(r'"(?:answer_)?score"\s*:\s*([01])', t)
            if m:
                return {"score": int(m[-1]), "why": t[-300:]}
        except Exception:
            pass
    return {"error": "judge failed"}


def summarize(path: Path, suite: str) -> dict:
    rows = [json.loads(x) for x in open(path, encoding="utf-8")] if path.exists() else []
    rows = list({r["id"]: r for r in rows if "error" not in r.get("grade", {})}.values())  # 同一题重跑过：按 id 去重，取最后一次
    if not rows:
        return {"n": 0}
    if suite == "vals":
        return {"n": len(rows), "partial%": round(100 * sum(r["grade"]["partial"] for r in rows) / len(rows), 1),
                "all_pass%": round(100 * sum(r["grade"]["all_pass"] for r in rows) / len(rows), 1)}
    by = {}
    for r in rows:
        by.setdefault(r["type"], []).append(r["grade"]["score"])
    return {"n": len(rows), "acc%": round(100 * sum(r["grade"]["score"] for r in rows) / len(rows), 1),
            **{k: f"{sum(v)}/{len(v)}" for k, v in sorted(by.items())}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["vals", "fsc"], required=True)
    ap.add_argument("--mode", choices=["bare", "stoa"], required=True)
    ap.add_argument("--n", type=int, default=10, help="FinSearchComp 每格（T2/T3 × 全球/大中华）抽几题")
    ap.add_argument("--data", default="data/external")
    ap.add_argument("--out", default="data/external")
    ap.add_argument("--limit", type=int, default=0, help="本次最多跑几题（续跑用）")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--tag", default="", help="输出文件名后缀，如 2 → vals-stoa2.jsonl（改进前后分开存）")
    a = ap.parse_args(argv)
    data, out = Path(a.data), Path(a.out)
    data.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{a.suite}-{a.mode}{a.tag}.jsonl"
    if a.summary:
        print(json.dumps(summarize(path, a.suite), ensure_ascii=False))
        return
    items = load_vals(data) if a.suite == "vals" else load_fsc(data, a.n)
    done = {json.loads(x)["id"] for x in open(path, encoding="utf-8")} if path.exists() else set()
    llm = _llm()
    base_home = Path(os.environ.get("STOCK_AGENT_HOME", "."))
    ran = 0
    for it in items:
        if it["id"] in done:
            continue
        if a.limit and ran >= a.limit:
            break
        t0 = time.time()
        try:
            ans, meta = answer_bare(it, a.suite, llm) if a.mode == "bare" else answer_stoa(it, a.suite, base_home)
        except Exception as e:
            ans, meta = "", {"error": str(e)[:200]}
        g = judge_vals(it, ans, llm) if a.suite == "vals" else judge_fsc(it, ans, llm)
        rec = {"id": it["id"], "type": it["type"], "secs": round(time.time() - t0), "tools": meta.get("tools", []),
               "answer": ans, "grade": g, **({"error": meta["error"]} if meta.get("error") else {})}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        ran += 1
        print(it["id"], rec["secs"], "s", json.dumps({k: v for k, v in g.items() if k not in ("pass", "why")}, ensure_ascii=False), flush=True)
    print(json.dumps(summarize(path, a.suite), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
