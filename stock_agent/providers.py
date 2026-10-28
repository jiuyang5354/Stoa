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
import uuid

import requests

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
        if name not in PRESETS:
            name = "openai"
        proto, base, dmodel, keyvar = PRESETS[name]
        self.name, self.proto = name, proto
        self.base = (base_env or base).rstrip("/")
        self.model = model or env("LLM_MODEL") or (None if explicit and explicit != cfg.get("provider") else cfg.get("model")) or dmodel
        self.key = env("LLM_API_KEY") or os.getenv(keyvar) or ("local" if name in LOCAL else None)
        self.api_version = os.getenv("AZURE_API_VERSION", "2024-10-21")

    @property
    def ready(self) -> bool:
        return bool(self.key and self.base and self.model)

    def __repr__(self):
        return f"{self.name}:{self.model}"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3, timeout: int = 120) -> dict:
        fn = {"openai": self._openai, "azure": self._openai, "anthropic": self._anthropic, "gemini": self._gemini}[self.proto]
        return fn(messages, tools or [], temperature, timeout)

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
        msg = r.json()["choices"][0]["message"]
        return {"role": "assistant", "content": msg.get("content"), "tool_calls": msg.get("tool_calls") or None}

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
        text, calls = [], []
        for b in r.json().get("content", []):
            if b["type"] == "text":
                text.append(b["text"])
            elif b["type"] == "tool_use":
                calls.append({"id": b["id"], "type": "function",
                              "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
        return {"role": "assistant", "content": "".join(text) or None, "tool_calls": calls or None}

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
        parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        calls = [{"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                  "function": {"name": p["functionCall"]["name"],
                               "arguments": json.dumps(p["functionCall"].get("args") or {}, ensure_ascii=False)}}
                 for p in parts if "functionCall" in p]
        return {"role": "assistant", "content": text or None, "tool_calls": calls or None}


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
