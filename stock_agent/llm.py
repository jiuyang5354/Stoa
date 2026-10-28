import json
import os

from .providers import Router

SYSTEM = (
    "你是一名严谨的股市分析师。只根据用户给的行情数据写中文点评，不编造数据和新闻。"
    "先一句话总结三个市场整体，再分 A股/港股/美股 各一两句，最后点评自选股中有信号的个股。"
    "不给买卖建议，结尾写「仅供参考，不构成投资建议」。300 字以内。"
)


def comment(snapshot: dict, cfg: dict, memory: str = "") -> str | None:
    llm = Router(cfg)
    if not llm.ready:
        return None
    try:
        msg = llm.chat([
            {"role": "system", "content": SYSTEM + ("\n\n以下是你对用户的长期记忆，点评时结合用户的持仓和偏好：\n" + memory if memory else "")},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)},
        ], timeout=60)
        return (msg.get("content") or "").strip()
    except Exception as e:  # 点评失败不影响报告
        return f"（AI 点评生成失败：{e}）"
