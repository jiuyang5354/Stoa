"""对话自动起标题：第一轮问答结束后在后台让模型给一个 4–16 字的短标题；模型不可用（或测试的假模型）就按规则从提问里截。
用户自己改过的名字不覆盖。"""
import os
import re

PROMPT = ("给下面这段对话起一个简短标题：中文 4–14 个字（用户用英文就用英文 2–6 个词），概括主题，不要标点、引号、书名号，"
          "不要「关于」「请问」这类虚词。只输出标题本身。\n\n用户：{q}\n\n回答（节选）：{a}")
FILLER = re.compile(r"^(请问|请|帮我|麻烦|你好|您好|hi|hello|hey|能不能|可以|我想|我想知道|想问一下|问一下|告诉我)[，,。\s]*", re.I)


def heuristic(question: str) -> str:
    q = re.sub(r"\[\[[^\]]*\]\]|<<<.*?>>>", "", question or "", flags=re.S)
    q = re.sub(r"^\[定时任务[^\]]*\]\s*", "", q.strip())
    q = " ".join(q.split())
    for _ in range(3):
        q = FILLER.sub("", q)
    q = re.sub(r"[？?。！!，,；;：:\s]+$", "", q)
    if not q:
        return ""
    if re.search(r"[\u4e00-\u9fff]", q):
        return q[:16] + ("…" if len(q) > 16 else "")
    words = q.split()
    return " ".join(words[:7]) + ("…" if len(words) > 7 else "")


def clean(t: str) -> str:
    t = (t or "").strip().splitlines()[0] if (t or "").strip() else ""
    t = re.sub(r"^(标题|title)\s*[:：]\s*", "", t, flags=re.I)
    t = t.strip(" \"'“”‘’「」《》#*`。.")
    return t[:30]


def make(question: str, answer: str = "", llm=None) -> str:
    if llm is not None and os.getenv("STOA_MOCK_LLM") != "1":
        try:
            msg = llm.chat([{"role": "user", "content": PROMPT.format(q=(question or "")[:600], a=re.sub(r"\[\[[^\]]*\]\]", "", answer or "")[:600])}],
                           None, temperature=0.2, timeout=25)
            t = clean(msg.get("content") or "")
            if 2 <= len(t) <= 30:
                return t
        except Exception:
            pass
    return heuristic(question)


def auto_title(mem, session: str, question: str, answer: str, llm=None) -> str | None:
    """还没有标题（用户没改过）时写入自动标题，返回标题；已有标题返回 None。"""
    if mem.has_title(session):
        return None
    t = make(question, answer, llm)
    if t and not mem.has_title(session):
        mem.rename_session(session, title=t, auto=True)
        return t
    return None
