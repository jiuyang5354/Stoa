"""v0.35 辩论分角色模型：分析师 / 裁决各自可配模型；没配时与以前完全一致；请求次数不变。全部离线。"""
import pytest

from stock_agent import cite
from stock_agent import debate as db
from stock_agent import evals as ev
from stock_agent import providers as pv


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    for k in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_FALLBACK",
              "DEBATE_ANALYST_MODEL", "DEBATE_JUDGE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(db, "_today", lambda: "2026-10-30")


FX = ev.load_cases()["fixtures"]


class Tagged(db.MockRoleLLM):
    """记录每个角色被哪个模型回答。"""

    def __init__(self, tag, log):
        super().__init__()
        self.tag, self.log = tag, log

    def __repr__(self):
        return self.tag

    def chat(self, messages, tools=None, temperature=0.3, timeout=120):
        import re
        role = re.search(r"\[ROLE:(\w+)\]", messages[0]["content"]).group(1)
        self.log.append((role, self.tag))
        return super().chat(messages, tools, temperature, timeout)


def _debate(llm, **kw):
    src = cite.Sources()
    return db.Debate(llm, "0700.HK", "腾讯", "深度分析", tools=lambda n, a: src.add(n, a, ev.fixture_tool(FX, n, a)),
                     budget=db.Budget(14, 60000, 10), sources=src, **kw)


def test_unconfigured_uses_one_model_and_same_calls():
    log = []
    base = Tagged("base", log)
    rec = _debate(base).run(save=False)
    assert {t for _, t in log} == {"base"}
    assert "models" not in rec
    n_plain = rec["budget"]["calls"]
    # 配了分角色模型后，调用次数与单模型完全相同
    log2 = []
    b2, a2, j2 = Tagged("base", log2), Tagged("cheap", log2), Tagged("strong", log2)
    rec2 = _debate(b2, analyst_llm=a2, judge_llm=j2).run(save=False)
    assert rec2["budget"]["calls"] == n_plain == len(log2)
    assert rec2["models"] == {"analyst": "cheap", "judge": "strong"}


def test_roles_routed_to_their_model():
    log = []
    base, a, j = Tagged("base", log), Tagged("cheap", log), Tagged("strong", log)
    _debate(base, analyst_llm=a, judge_llm=j, rounds=1).run(save=False)
    by = {}
    for role, tag in log:
        by.setdefault(role, set()).add(tag)
    for r in ("technical", "news", "macro", "bull", "bear"):
        if r in by:
            assert by[r] == {"cheap"}, (r, by[r])
    assert by["pm"] == {"strong"} and by.get("risk", {"strong"}) == {"strong"}
    assert "base" not in {t for _, t in log}


def test_only_judge_configured_analysts_fall_back():
    log = []
    base, j = Tagged("base", log), Tagged("strong", log)
    d = _debate(base, judge_llm=j)
    d.run(save=False)
    assert {t for r, t in log if r in ("pm", "risk")} == {"strong"}
    assert {t for r, t in log if r not in ("pm", "risk", "editor")} == {"base"}


def test_cfg_values_and_env(monkeypatch):
    cv = db.cfg_values({"debate": {"analyst_model": "gpt-mini", "judge_model": "  "}})
    assert cv["analyst_model"] == "gpt-mini" and cv["judge_model"] == ""
    monkeypatch.setenv("DEBATE_JUDGE_MODEL", "big-model   # 注释")
    assert db.cfg_values({})["judge_model"] == "big-model"
    assert db.cfg_values({})["analyst_model"] == ""


def test_parse_model_spec():
    assert pv.parse_model_spec("") == (None, "")
    assert pv.parse_model_spec("gpt-6.1-terra") == (None, "gpt-6.1-terra")
    assert pv.parse_model_spec("deepseek:deepseek-chat") == ("deepseek", "deepseek-chat")
    assert pv.parse_model_spec("qwen2.5:7b") == (None, "qwen2.5:7b")  # 前缀不是服务商：整串当模型名


def test_role_llm_same_provider_swaps_model_only(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-6.1-terra")
    base = pv.Router({})
    assert pv.role_llm(base, "") is base and pv.role_llm(base, None) is base
    assert pv.role_llm(base, "gpt-6.1-terra") is base  # 和默认一样就不包一层
    r = pv.role_llm(base, "gpt-mini")
    assert isinstance(r, pv.RoleRouter)
    head = r.chain[0]
    assert head.model == "gpt-mini" and head.base == "https://relay.example/v1" and head.key == "sk-test"
    assert base.chain[0].model == "gpt-6.1-terra"  # 原路由没被改
    # 运行时属性（取消信号、截止时间）转读原路由
    base.stop = "STOP"
    assert r.stop == "STOP"
    assert getattr(r, "nothing_here", None) is None


def test_role_llm_keeps_fallback_chain_and_does_not_add_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "main")
    monkeypatch.setenv("LLM_FALLBACK", "ollama:qwen2.5:7b")
    base = pv.Router({})
    r = pv.role_llm(base, "mini")
    assert [c.model for c in r.chain] == ["mini"] + [c.model for c in base.chain[1:]]
    assert "main" not in [c.model for c in r.chain]  # 角色模型失败不回退到默认模型（不多发请求）


def test_role_llm_non_router_passthrough():
    m = db.MockRoleLLM()
    assert pv.role_llm(m, "anything") is m


def test_role_llm_called_once_per_request(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "main")
    base = pv.Router({})
    r = pv.role_llm(base, "mini")
    seen = []
    monkeypatch.setattr(pv.LLM, "chat", lambda self, *a, **k: seen.append(self.model) or {"content": "ok"})
    r.chat([{"role": "user", "content": "hi"}])
    assert seen == ["mini"]


def test_run_for_agent_reads_config(monkeypatch):
    monkeypatch.setenv("DEBATE_ANALYST_MODEL", "mini")
    got = {}
    real = db.Debate.__init__

    def spy(self, *a, **k):
        got.update(k)
        real(self, *a, **k)
    monkeypatch.setattr(db.Debate, "__init__", spy)
    monkeypatch.setattr(db.Debate, "run", lambda self, record=True, save=True: {"id": "x", "verdict": {}})
    calls = []
    monkeypatch.setattr(db, "role_llms", lambda llm, cv=None: calls.append(cv) or ("A", "J"))

    class Ag:
        llm = db.MockRoleLLM()
        sources = cite.Sources()

        class mem:
            @staticmethod
            def entries(_):
                return []

        def _tool(self, n, a):
            return {}
    db.run_for_agent(Ag(), "0700.HK")
    assert calls and calls[0]["analyst_model"] == "mini"
    assert got["analyst_llm"] == "A" and got["judge_llm"] == "J"


def test_settings_fields_present():
    from stock_agent import settings
    assert settings.FIELDS["DEBATE_ANALYST_MODEL"][0] == "tools"
    assert settings.FIELDS["DEBATE_JUDGE_MODEL"][0] == "tools"


def test_backtest_cache_key_unchanged_without_roles(tmp_path):
    from stock_agent.backtest import LLMCache
    m = db.MockRoleLLM()
    ag = db.DebateBTAgent(llm=m, cache=LLMCache(tmp_path / "c.db"))

    class Snap:
        symbol, horizon = "X", 5
        import datetime as _d
        date = _d.datetime(2026, 1, 5)
    k1 = ag.key(Snap)
    ag2 = db.DebateBTAgent(llm=m, cache=LLMCache(tmp_path / "c2.db"), analyst_llm=db.MockRoleLLM(), judge_llm=None)
    assert ag2.key(Snap) != k1
    ag3 = db.DebateBTAgent(llm=m, cache=LLMCache(tmp_path / "c3.db"), analyst_llm=m, judge_llm=m)
    assert ag3.key(Snap) == k1  # 角色模型就是默认模型 = 没配
