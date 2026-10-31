"""Private model transport process; credentials arrive on stdin, never on the command line."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stock_agent import providers


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    payload = json.load(sys.stdin)
    os.environ["LLM_RETRIES"] = payload["retries"]
    os.environ["LLM_RETRY_BASE"] = payload["retry_base"]
    llm = providers.LLM.__new__(providers.LLM)
    llm.__dict__.update(payload["llm"])
    llm.on_retry = lambda event: emit({"kind": "event", "event": event})
    try:
        message = llm.chat(payload["messages"], payload["tools"], payload["temperature"], payload["timeout"])
        emit({"kind": "result", "message": message, "usage": providers.usage_since()})
    except Exception as exc:
        emit({"kind": "error", "status": getattr(getattr(exc, "response", None), "status_code", None),
              "error": type(exc).__name__ + "：模型请求失败", "usage": providers.usage_since()})


if __name__ == "__main__":
    main()
