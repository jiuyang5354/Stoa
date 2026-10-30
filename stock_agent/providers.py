"""大模型接入层：一个 chat() 接口，兼容 OpenAI 协议的各家 + Anthropic、Gemini、Azure 原生协议。

环境变量：
  LLM_PROVIDER   预设名（见 PRESETS），不填则按 LLM_BASE_URL 自动判断，再不行当作 OpenAI 兼容
  LLM_API_KEY    密钥（不填时会找各家自己的变量名，如 DEEPSEEK_API_KEY、ANTHROPIC_API_KEY）
  LLM_BASE_URL   覆盖预设地址（自建网关、中转、vLLM、Ollama 等）
  LLM_MODEL      覆盖预设模型
  LLM_FALLBACK   主模型出错时依次尝试，如 "qwen,ollama:qwen2.5:7b"（预设名[:模型]）
所有返回统一成 OpenAI 格式的 message：{"role": "assistant", "content": ..., "tool_calls": [...]}
"""
import json
import os
import random
import threading
import time
import uuid

import requests

# 真实用量（接口返回的 usage）：回测/评测用它报 token，没有 usage 的接口记 calls_no_usage
USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "calls_no_usage": 0, "errors": 0, "retries": 0}
RETRY_STATUS = {429, 500, 502, 503, 504}  # 中转/网关常见的限流与临时故障：退避重试（LLM_RETRIES，默认 3 次）
_USAGE_LOCK = threading.Lock()


def record_usage(u: dict | None = None, error: bool = False):
    with _USAGE_LOCK:
        if error:
            USAGE["errors"] += 1
            return
        USAGE["calls"] += 1
        if not u:
            USAGE["calls_no_usage"] += 1
            return
        USAGE["prompt_tokens"] += int(u.get("prompt_tokens") or u.get("input_tokens") or u.get("promptTokenCount") or 0)
        USAGE["completion_tokens"] += int(u.get("completion_tokens") or u.get("output_tokens") or u.get("candidatesTokenCount") or 0)


def _backoff(resp, attempt: int) -> float:
    """Retry-After（秒）优先，否则 2、4、8… 秒加抖动，最多 30 秒。"""
    try:
        ra = float((resp.headers or {}).get("Retry-After") or 0)
    except (TypeError, ValueError, AttributeError):
        ra = 0
    base = float(os.getenv("LLM_RETRY_BASE", "2"))
    return min(30.0, ra or base * (2 ** attempt) * (1 + random.random() * 0.25))


def usage_since(base: dict | None = None) -> dict:
    """当前累计用量（给了 base 就返回差值）。"""
    with _USAGE_LOCK:
        cur = dict(USAGE)
    if base:
        cur = {k: cur[k] - base.get(k, 0) for k in cur}
    cur["total_tokens"] = cur["prompt_tokens"] + cur["completion_tokens"]
    return cur

# name: (协议, 默认地址, 默认模型, 密钥变量名)
PRESETS = {
    "openai":      ("openai", "https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "deepseek":    ("openai", "https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "qwen":        ("openai", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "DASHSCOPE_API_KEY"),
    "kimi":        ("openai", "https://api.moonshot.cn/v1", "kimi-k2-turbo-preview", "MOONSHOT_API_KEY"),
    "zhipu":       ("openai", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "ZHIPU_API_KEY"),
    "doubao":      ("openai", "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-1-6-250615", "ARK_API_KEY"),
    "hunyuan":     ("openai", "https://api.hunyuan.cloud.tencent.com/v1", "hunyuan-turbos-latest", "HUNYUAN_API_KEY"),
    "minimax":     ("openai", "https://api.minimaxi.com/v1", "MiniMax-M1", "MINIMAX_API_KEY"),
    "stepfun":     ("openai", "https://api.stepfun.com/v1", "step-2-mini", "STEPFUN_API_KEY"),
    "baichuan":    ("openai", "https://api.baichuan-ai.com/v1", "Baichuan4-Turbo", "BAICHUAN_API_KEY"),
    "siliconflow": ("openai", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3", "SILICONFLOW_API_KEY"),
    "openrouter":  ("openai", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "OPENROUTER_API_KEY"),
    "groq":        ("openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "together":    ("openai", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY"),
    "fireworks":   ("openai", "https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/deepseek-v3", "FIREWORKS_API_KEY"),
    "mistral":     ("openai", "https://api.mistral.ai/v1", "mistral-small-latest", "MISTRAL_API_KEY"),
    "xai":         ("openai", "https://api.x.ai/v1", "grok-3-mini", "XAI_API_KEY"),
    "perplexity":  ("openai", "https://api.perplexity.ai", "sonar", "PERPLEXITY_API_KEY"),
    "ollama":      ("openai", "http://localhost:11434/v1", "qwen2.5:7b", "OLLAMA_API_KEY"),
    "lmstudio":    ("openai", "http://localhost:1234/v1", "local-model", "LMSTUDIO_API_KEY"),
    "vllm":        ("openai", "http://localhost:8000/v1", "default", "VLLM_API_KEY"),
    "anthropic":   ("anthropic", "https://api.anthropic.com/v1", "claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
    "gemini":      ("gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", "GEMINI_API_KEY"),
    "azure":       ("azure", "", "", "AZURE_OPENAI_API_KEY"),  # LLM_BASE_URL=https://<资源>.openai.azure.com/openai/deployments/<部署名>
}
LOCAL = {"ollama", "lmstudio", "vllm"}

# 上下文窗口（token）：长对话压缩按它定预算。没列的默认 32k；LLM_CONTEXT 可覆盖。
CONTEXT = {
    "openai": 128000, "deepseek": 64000, "qwen": 128000, "kimi": 128000, "zhipu": 128000, "doubao": 128000, "hunyuan": 32000,
    "minimax": 200000, "stepfun": 32000, "baichuan": 32000, "siliconflow": 64000, "openrouter": 128000, "groq": 128000,
    "together": 128000, "fireworks": 128000, "mistral": 32000, "xai": 128000, "perplexity": 128000,
    "ollama": 8192, "lmstudio": 8192, "vllm": 32000, "anthropic": 200000, "gemini": 1000000, "azure": 128000,
}
DEFAULT_CONTEXT = 32000

# 支持 OpenAI 兼容 /embeddings 的预设 → 默认嵌入模型（语义记忆用；EMBED_MODEL 可覆盖）。
# 没列的（deepseek、kimi、anthropic、gemini 原生协议等）没有 embeddings 接口，自动用本地 TF-IDF 向量。
EMBED_MODELS = {
    "openai": "text-embedding-3-small", "qwen": "text-embedding-v3", "zhipu": "embedding-3", "siliconflow": "BAAI/bge-m3",
    "mistral": "mistral-embed", "together": "BAAI/bge-base-en-v1.5", "fireworks": "nomic-ai/nomic-embed-text-v1.5",
    "ollama": "nomic-embed-text", "lmstudio": "text-embedding-nomic-embed-text-v1.5", "vllm": "default", "hunyuan": "hunyuan-embedding",
}


# ---------- 自定义模型来源（设置 → 模型 → 自定义来源）：数据目录 sources.json ----------
# [{"id": "relay", "name": "我的中转", "base": "https://…/v1", "key": "sk-…", "proto": "openai|anthropic|gemini", "model": "…"}]
# id 和预设名一样用：LLM_PROVIDER=relay、备用链 relay:gpt-4o。
PROTOS = ("openai", "anthropic", "gemini")
NOT_CHAT = r"embed|tts|whisper|dall-e|image|audio|moderation|rerank|transcribe|realtime|speech|vision-preview"


def _sources_path():
    from .memory import home
    return home() / "sources.json"


def custom_sources() -> list[dict]:
    p = _sources_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("id") and d.get("base")]


def custom_source(name: str | None) -> dict | None:
    n = (name or "").lower()
    return next((d for d in custom_sources() if d["id"] == n), None)


def source_id(name: str) -> str:
    import re
    sid = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")[:32]
    if not sid or not re.match(r"[a-z]", sid):
        sid = "src-" + (sid or uuid.uuid4().hex[:6])
    if sid in PRESETS:
        sid += "-custom"
    return sid


def save_source(src: dict, old: str | None = None) -> dict:
    """新增或修改一个自定义来源。key 留空 = 不改。"""
    name = str(src.get("name") or "").strip()[:40]
    base = str(src.get("base") or "").strip().rstrip("/")
    proto = src.get("proto") if src.get("proto") in PROTOS else "openai"
    if not name or not base.startswith(("http://", "https://")):
        return {"error": "名称和以 http(s):// 开头的接口地址都要填"}
    items = custom_sources()
    prev = next((d for d in items if d["id"] == (old or "")), None)
    sid = prev["id"] if prev else source_id(name)
    if not prev and any(d["id"] == sid for d in items):
        return {"error": f"已经有叫「{name}」的来源了"}
    key = str(src.get("key") or "").strip() or (prev or {}).get("key", "")
    item = {"id": sid, "name": name, "base": base, "key": key, "proto": proto, "model": str(src.get("model") or "").strip()[:120]}
    items = [item if d["id"] == sid else d for d in items] if prev else items + [item]
    p = _sources_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "id": sid, "msg": f"已保存来源「{name}」"}


def delete_source(sid: str) -> dict:
    items = custom_sources()
    left = [d for d in items if d["id"] != sid]
    if len(left) == len(items):
        return {"error": "没有这个来源"}
    _sources_path().write_text(json.dumps(left, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "msg": "已删除"}


def fetch_models(base: str, key: str | None, proto: str = "openai", timeout: float = 8) -> list[str]:
    """问服务商有哪些模型（OpenAI 兼容 /models、Anthropic /models、Gemini /models），只留聊天模型。失败抛异常。"""
    import re
    base = (base or "").rstrip("/")
    if proto == "anthropic":
        r = requests.get(f"{base}/models", headers={"x-api-key": key or "", "anthropic-version": "2023-06-01"}, params={"limit": 1000}, timeout=timeout)
        ids = [m.get("id") for m in (r.json().get("data") or [])] if r.ok else None
    elif proto == "gemini":
        r = requests.get(f"{base}/models", params={"key": key or "", "pageSize": 1000}, timeout=timeout)
        ids = [str(m.get("name", "")).removeprefix("models/") for m in (r.json().get("models") or [])
               if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])] if r.ok else None
    else:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"} if key and key != "local" else {}, timeout=timeout)
        ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)] if r.ok else None
    if ids is None:
        msg = ""
        try:
            e = r.json().get("error")
            msg = (e.get("message") if isinstance(e, dict) else e) or ""
        except Exception:
            msg = r.text[:120]
        raise RuntimeError(f"HTTP {r.status_code} {str(msg)[:100]}".strip())
    return sorted({i for i in ids if i and not re.search(NOT_CHAT, i, re.I)})


def _guess(base: str) -> str:
    b = base.lower()
    for key, name in (("anthropic", "anthropic"), ("generativelanguage", "gemini"), ("openai.azure.com", "azure"),
                      ("deepseek", "deepseek"), ("dashscope", "qwen"), ("moonshot", "kimi"), ("bigmodel", "zhipu"),
                      ("volces", "doubao"), (":11434", "ollama")):
        if key in b:
            return name
    return "openai"


class LLM:
    def __init__(self, provider: str | None = None, model: str | None = None, cfg: dict | None = None, primary: bool = True):
        cfg = cfg or {}
        env = (lambda k: os.getenv(k)) if primary else (lambda k: None)
        explicit = provider or env("LLM_PROVIDER")
        base_env = env("LLM_BASE_URL") or (None if explicit else cfg.get("base_url"))
        name = (explicit or cfg.get("provider") or (_guess(base_env) if base_env else "deepseek")).lower()
        src = custom_source(name) if name not in PRESETS else None
        if src:  # 自定义来源：地址和密钥都用来源自己的，不受 LLM_BASE_URL / LLM_API_KEY 影响
            self.name, self.proto, self.label = name, src.get("proto") or "openai", src.get("name") or name
            self.base = src["base"].rstrip("/")
            self.model = model or env("LLM_MODEL") or (None if explicit and explicit != cfg.get("provider") else cfg.get("model")) or src.get("model") or ""
            self.key = src.get("key") or "local"
            self.api_version = os.getenv("AZURE_API_VERSION", "2024-10-21")
            return
        if name not in PRESETS:
            name = "openai"
        proto, base, dmodel, keyvar = PRESETS[name]
        self.name, self.proto, self.label = name, proto, name
        self.base = (base_env or base).rstrip("/")
        self.model = model or env("LLM_MODEL") or (None if explicit and explicit != cfg.get("provider") else cfg.get("model")) or dmodel
        self.key = env("LLM_API_KEY") or os.getenv(keyvar) or ("local" if name in LOCAL else None)
        self.api_version = os.getenv("AZURE_API_VERSION", "2024-10-21")

    @property
    def ready(self) -> bool:
        return bool(self.key and self.base and self.model)

    @property
    def context(self) -> int:
        try:
            return int(os.getenv("LLM_CONTEXT") or 0) or CONTEXT.get(self.name, DEFAULT_CONTEXT)
        except ValueError:
            return CONTEXT.get(self.name, DEFAULT_CONTEXT)

    def __repr__(self):
        return f"{self.name}:{self.model}"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3, timeout: int = 120) -> dict:
        fn = {"openai": self._openai, "azure": self._openai, "anthropic": self._anthropic, "gemini": self._gemini}[self.proto]
        try:
            retries = max(0, int(os.getenv("LLM_RETRIES", "3")))
        except ValueError:
            retries = 3
        for attempt in range(retries + 1):
            try:
                return fn(messages, tools or [], temperature, timeout)
            except requests.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code not in RETRY_STATUS or attempt >= retries:
                    record_usage(error=True)
                    raise
                with _USAGE_LOCK:
                    USAGE["retries"] += 1
                time.sleep(_backoff(e.response, attempt))
            except Exception:
                record_usage(error=True)
                raise

    # ---- OpenAI / Azure ----
    def _openai(self, messages, tools, temperature, timeout):
        body = {"model": self.model, "temperature": temperature, "messages": messages}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if self.proto == "azure":
            url, headers = f"{self.base}/chat/completions?api-version={self.api_version}", {"api-key": self.key}
            body.pop("model")
        else:
            url, headers = f"{self.base}/chat/completions", {"Authorization": f"Bearer {self.key}"}
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        record_usage(j.get("usage"))
        msg = j["choices"][0]["message"]
        out = {"role": "assistant", "content": msg.get("content"), "tool_calls": msg.get("tool_calls") or None}
        think = _reasoning_text(msg.get("reasoning_content") or msg.get("reasoning") or msg.get("thinking"))
        if think:
            out["_reasoning"] = think  # 下划线开头：只给界面看，发回模型前会被剥掉（DeepSeek 等要求不回传）
        return out

    # ---- Anthropic Messages API ----
    def _anthropic(self, messages, tools, temperature, timeout):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        out: list[dict] = []

        def push(role, block):
            if out and out[-1]["role"] == role:
                out[-1]["content"].append(block)
            else:
                out.append({"role": role, "content": [block]})

        for m in messages:
            if m["role"] == "user":
                push("user", {"type": "text", "text": m["content"]})
            elif m["role"] == "assistant":
                if m.get("content"):
                    push("assistant", {"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    push("assistant", {"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                                       "input": json.loads(c["function"].get("arguments") or "{}")})
            elif m["role"] == "tool":
                push("user", {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]})
        body = {"model": self.model, "max_tokens": 4096, "temperature": temperature, "system": system, "messages": out}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        r = requests.post(f"{self.base}/messages", json=body, timeout=timeout,
                          headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        text, calls, think = [], [], []
        record_usage(r.json().get("usage"))
        for b in r.json().get("content", []):
            if b["type"] == "thinking":
                think.append(b.get("thinking") or "")
            elif b["type"] == "text":
                text.append(b["text"])
            elif b["type"] == "tool_use":
                calls.append({"id": b["id"], "type": "function",
                              "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
        out = {"role": "assistant", "content": "".join(text) or None, "tool_calls": calls or None}
        if "".join(think).strip():
            out["_reasoning"] = "\n".join(think).strip()
        return out

    # ---- Google Gemini generateContent ----
    def _gemini(self, messages, tools, temperature, timeout):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        names = {}  # tool_call_id -> 工具名（Gemini 的回传要名字不要 id）
        contents: list[dict] = []

        def push(role, part):
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": role, "parts": [part]})

        for m in messages:
            if m["role"] == "user":
                push("user", {"text": m["content"]})
            elif m["role"] == "assistant":
                if m.get("content"):
                    push("model", {"text": m["content"]})
                for c in m.get("tool_calls") or []:
                    names[c["id"]] = c["function"]["name"]
                    push("model", {"functionCall": {"name": c["function"]["name"],
                                                    "args": json.loads(c["function"].get("arguments") or "{}")}})
            elif m["role"] == "tool":
                push("user", {"functionResponse": {"name": names.get(m["tool_call_id"], "tool"),
                                                   "response": {"result": m["content"]}}})
        body = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [  # Gemini 不接受空 properties
                {k: v for k, v in t.items() if k != "parameters" or v.get("properties")} for t in tools]}]
        r = requests.post(f"{self.base}/models/{self.model}:generateContent", json=body, timeout=timeout,
                          headers={"x-goog-api-key": self.key})
        r.raise_for_status()
        record_usage(r.json().get("usageMetadata"))
        parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        think = "".join(p.get("text", "") for p in parts if p.get("thought")).strip()
        calls = [{"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                  "function": {"name": p["functionCall"]["name"],
                               "arguments": json.dumps(p["functionCall"].get("args") or {}, ensure_ascii=False)}}
                 for p in parts if "functionCall" in p]
        out = {"role": "assistant", "content": text or None, "tool_calls": calls or None}
        if think:
            out["_reasoning"] = think
        return out


def _reasoning_text(v) -> str:
    """各家中转的推理字段格式不一：字符串 / {"summary": [...]} / [{"text": ...}]。统一成纯文本。"""
    if not v:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return _reasoning_text(v.get("summary") or v.get("text") or v.get("content"))
    if isinstance(v, list):
        return "\n".join(t for t in (_reasoning_text(x) for x in v) if t).strip()
    return ""


class Router:
    """主模型 + 备用模型链。"""

    def __init__(self, cfg: dict | None = None):
        self.chain = [LLM(cfg=cfg)]
        for spec in filter(None, (s.strip() for s in os.getenv("LLM_FALLBACK", "").split(","))):
            prov, _, model = spec.partition(":")
            self.chain.append(LLM(prov, model or None, primary=False))
        self.chain = [c for c in self.chain if c.ready] or self.chain[:1]

    @property
    def ready(self) -> bool:
        return self.chain[0].ready

    @property
    def context(self) -> int:
        """备用模型可能窗口更小：按链里最小的算，切换后也放得下。"""
        return min(c.context for c in self.chain)

    def __repr__(self):
        return " → ".join(map(repr, self.chain))

    def chat(self, messages, tools=None, temperature=0.3, timeout=120) -> dict:
        err = None
        for llm in self.chain:
            try:
                return llm.chat(messages, tools, temperature, timeout)
            except Exception as e:
                err = e
        raise err
