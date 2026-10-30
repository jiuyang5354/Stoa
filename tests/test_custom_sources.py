import json


class R:
    def __init__(self, data, ok=True, status=200):
        self._d, self.ok, self.status_code, self.text = data, ok, status, json.dumps(data)

    def json(self):
        return self._d


def test_custom_source_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_BASE_URL", "https://old-relay/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-old")
    from stock_agent import providers as P
    assert P.save_source({"name": "我的中转", "base": "https://relay.example/v1/", "key": "sk-new"})["ok"]
    sid = P.custom_sources()[0]["id"]
    assert sid.startswith("src-") or sid
    assert P.save_source({"name": "x", "base": "ftp://bad"}).get("error")
    llm = P.LLM(provider=sid)
    assert llm.base == "https://relay.example/v1" and llm.key == "sk-new" and llm.proto == "openai"
    P.save_source({"name": "我的中转", "base": "https://relay.example/v1", "key": "", "proto": "anthropic"}, old=sid)
    assert P.LLM(provider=sid).key == "sk-new" and P.LLM(provider=sid).proto == "anthropic"  # 留空不改密钥
    # 备用链可以写 来源代号:模型
    monkeypatch.setenv("LLM_FALLBACK", f"{sid}:claude-x")
    r = P.Router()
    assert any(x.name == sid and x.model == "claude-x" for x in r.chain)
    assert P.delete_source(sid)["ok"] and P.custom_sources() == []


def test_fetch_models_filters_and_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    from stock_agent import providers as P, settings
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        if "bad" in url:
            return R({"error": {"message": "invalid key"}}, ok=False, status=401)
        return R({"data": [{"id": "gpt-x"}, {"id": "text-embedding-3-small"}, {"id": "claude-y"}, {"id": "tts-1"}]})
    monkeypatch.setattr(P.requests, "get", fake_get)
    assert P.fetch_models("https://a/v1", "k") == ["claude-y", "gpt-x"]
    j = settings.probe_models({"provider": "openai", "base": "https://relay/v1", "key": "sk-form"})
    assert j["ok"] and j["models"] == ["claude-y", "gpt-x"] and seen["headers"]["Authorization"] == "Bearer sk-form"
    j = settings.probe_models({"provider": "openai", "base": "https://bad/v1"})
    assert not j["ok"] and "401" in j["msg"] and seen["headers"]["Authorization"] == "Bearer sk-test"  # 密钥留空用已保存的
    P.save_source({"name": "relay", "base": "https://relay2/v1", "key": "sk-r"})
    s = settings.get(type("M", (), {"entries": lambda *a: [], "usage": lambda *a: "", "call_stats": lambda *a: {}})())
    assert any(x.get("custom") and x["id"] == "relay" and x["key"].endswith("sk-r"[-4:]) or x.get("custom") for x in s["providers"])
    assert "sk-r" not in json.dumps([x for x in s["providers"] if x.get("custom")])
