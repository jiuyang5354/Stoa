"""v0.40 按次质量指标：每轮一行 data/quality/YYYY-MM.jsonl，不记原文；写失败不影响回答；点踩补记；近 7 天汇总。"""
import json
from datetime import datetime, timedelta

import pytest

from stock_agent import quality


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("EVOLVE_REVIEW", "off")
    monkeypatch.delenv("STOA_MOCK_LLM", raising=False)
    return tmp_path


MT = {"name": "贵州茅台", "date": "2026-10-28", "close": 1354.0, "chg_pct": -0.84, "chg_5d": -2.31, "chg_20d": -5.47, "ma20": 1398.6,
      "ma60": 1421.3, "rsi": 38.4, "high_52w": 1688.0, "low_52w": 1301.5, "signals": [], "source": "测试"}
SECRET_Q = "我的秘密持仓是茅台 777 股，茅台现在能买吗？"


class ScriptLLM:
    ready = True
    model = "script-1"

    def __init__(self, text):
        self.text, self.calls = text, 0

    def chat(self, messages, tools=None, **kw):
        from stock_agent import providers
        self.calls += 1
        providers.USAGE["prompt_tokens"] += 1000
        providers.USAGE["completion_tokens"] += 50
        providers.USAGE["cached_tokens"] += 600
        providers.USAGE["calls"] += 1
        if messages[-1]["role"] != "tool" and tools:
            return {"role": "assistant", "content": "先查行情。", "tool_calls": [
                {"id": f"c{self.calls}", "type": "function", "function": {"name": "get_quotes", "arguments": json.dumps({"symbols": ["600519.SS"]})}}]}
        return {"role": "assistant", "content": self.text}


def _agent(text):
    from stock_agent.evals import make_agent
    from stock_agent.memory import Memory
    a = make_agent({"quotes": {"600519.SS": MT}}, Memory(), ScriptLLM(text), [])
    a.channel = "web"  # 评测通道不记指标
    a.tools = [t for t in a.tools if t["name"] == "get_quotes"]
    return a


def _rows(home):
    files = list((home / "data" / "quality").glob("*.jsonl"))
    return [json.loads(x) for f in files for x in f.read_text(encoding="utf-8").splitlines()]


def test_one_line_per_turn_without_raw_text(home):
    a = _agent("贵州茅台现价 1354.0，短期偏多。")
    reply = a.ask(SECRET_Q)
    rows = _rows(home)
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "script-1" and r["session"] == a.session and r["mode"] == "quick" and r["channel"] == "web"
    assert r["prompt_tokens"] == 2000 and r["completion_tokens"] == 100 and r["cached_tokens"] == 1200 and r["llm_calls"] == 2
    assert r["tools"] == 1 and r["secs"] >= 0
    assert r["verify"]["numbers"] >= 1 and r["verify"]["unverified"] == 0 and r["verify"]["verified"] == r["verify"]["numbers"]
    assert "invalidation" in r["added"] and "disclaimer" in r["added"]  # answercheck 补了失效条件和免责
    assert r["q_len"] == len(SECRET_Q) and r["a_len"] == len(reply)
    raw = (home / "data" / "quality").joinpath(f"{quality._now():%Y-%m}.jsonl").read_text(encoding="utf-8")
    assert "777" not in raw and "秘密" not in raw and "1354" not in raw  # 不记提问和回答原文


def test_unverified_number_counted(home):
    a = _agent("贵州茅台现价 1354.0，市盈率 88.8 倍。")
    a.ask("茅台怎么样")
    v = _rows(home)[0]["verify"]
    assert v["unverified"] >= 1 and v["verified"] >= 1


def test_write_failure_never_breaks_answer(home, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(quality, "_write", boom)
    a = _agent("贵州茅台现价 1354.0。")
    assert "1354" in a.ask("茅台多少钱")


def test_eval_channel_not_recorded(home):
    from stock_agent.evals import make_agent
    from stock_agent.memory import Memory
    a = make_agent({"quotes": {"600519.SS": MT}}, Memory(), ScriptLLM("1354.0"), [])
    a.ask("茅台多少钱")
    assert _rows(home) == []


def test_downvote_marks_turn(home):
    from stock_agent import learn
    from stock_agent.memory import Memory
    a = _agent("贵州茅台现价 1354.0，可以上车。")
    a.ask("茅台能买吗")
    res = learn.feedback(Memory(), a.session, "说法不对", turn=1)
    assert not res.get("error")
    s = quality.summary(7)
    assert s["total"]["turns"] == 1 and s["total"]["downs"] == 1


def test_summary_by_model_rates_and_window(home):
    now = datetime(2026, 11, 2, 12, 0)
    rows = [
        {"ts": (now - timedelta(days=1)).isoformat(), "session": "s", "q_id": 1, "model": "m1", "secs": 10, "prompt_tokens": 1000, "completion_tokens": 100,
         "cached_tokens": 500, "tools": 2, "verify": {"numbers": 4, "verified": 3, "unverified": 1}, "added": [], "fallback": False, "timeout": False},
        {"ts": (now - timedelta(days=2)).isoformat(), "session": "s", "q_id": 2, "model": "m1", "secs": 30, "prompt_tokens": 3000, "completion_tokens": 300,
         "cached_tokens": 0, "tools": 4, "verify": {"numbers": 6, "verified": 6, "unverified": 0}, "added": ["disclaimer"], "fallback": True, "timeout": False},
        {"ts": (now - timedelta(days=3)).isoformat(), "session": "t", "q_id": 9, "model": "m2", "secs": 5, "prompt_tokens": 500, "completion_tokens": 50,
         "cached_tokens": 0, "tools": 0, "verify": {}, "added": [], "fallback": False, "timeout": True},
        {"ts": (now - timedelta(days=20)).isoformat(), "session": "old", "q_id": 3, "model": "m1", "secs": 999, "prompt_tokens": 9, "completion_tokens": 9},
        {"ev": "down", "ts": now.isoformat(), "session": "s", "q_id": 2},
    ]
    d = home / "data" / "quality"
    d.mkdir(parents=True)
    (d / "2026-10.jsonl").write_text(json.dumps(rows[3]) + "\nnot json\n", encoding="utf-8")
    (d / "2026-11.jsonl").write_text("\n".join(json.dumps(r) for r in rows[:3] + rows[4:]), encoding="utf-8")
    s = quality.summary(7, now=now)
    T = s["total"]
    assert T["turns"] == 3 and T["downs"] == 1 and T["timeouts"] == 1 and T["added"] == 1
    assert T["unverified_rate"] == 10.0 and T["fallback_rate"] == pytest.approx(66.7)
    assert T["avg_tokens"] == pytest.approx((1100 + 3300 + 550) / 3, rel=1e-3) and T["avg_secs"] == 15.0
    m1 = [m for m in s["models"] if m["model"] == "m1"][0]
    assert m1["turns"] == 2 and m1["avg_secs"] == 20.0 and m1["downs"] == 1 and s["models"][0]["model"] == "m1"
    assert [x["date"] for x in s["daily"]] == ["2026-10-30", "2026-10-31", "2026-11-01"]


def test_empty_summary(home):
    assert quality.summary(7)["total"] == {"turns": 0}


def test_settings_section_and_i18n():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "stock_agent"
    html = (root / "web.html").read_text(encoding="utf-8")
    assert "['usage','用量']" in html and "/api/quality?days=7" in html and "c: '/usage'" in html
    en = json.loads((root / "assets" / "i18n-en.json").read_text(encoding="utf-8"))
    for k in ("用量", "平均 token", "平均耗时", "未核实数字率", "兜底率", "Per exemplar · 按模型", "Dies · 每天轮数"):
        assert k in en
