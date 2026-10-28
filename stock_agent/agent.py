"""对话式股市 Agent：工具调用 + Hermes 式记忆与学习闭环。"""
import json
import os
import uuid

import requests
import yaml

from . import data, indicators
from . import sandbox
from .memory import Memory, config_path, now

NUDGE_EVERY = 6  # 每隔几轮提醒自己是否该存记忆

SYSTEM = """你是「股市 Agent」，用户的长期股市助手，覆盖 A股、港股、美股。用中文回答，简洁、具体、有数据。

原则：
- 行情数字必须来自工具结果，不编造。不知道就说不知道。
- 可以给出你的判断和理由，但每次给出明确的方向性判断（看涨/看跌/区间）时，调用 record_call 记账，方便日后复盘。
- 不替用户下单，不承诺收益。重要结论后提醒「仅供参考，不构成投资建议」。

记忆（像 Hermes Agent 一样，你自己负责维护）：
- USER.md 存用户画像：持仓与成本、风险偏好、投资风格、关注板块、不喜欢什么、沟通偏好。
- MEMORY.md 存你自己的笔记：复盘得出的经验、对某只股票的长期观察、用户纠正过你的地方、数据源的坑。
- 用户透露了以后还用得上的信息，当场用 memory 工具保存；信息变了就 replace，过时就 remove。
- 不要存：一次性的行情数字、闲聊、能随时再查到的东西。条目要短、可独立理解，带日期。
- 用户提到「上次」「之前说过」时，先用 session_search 查历史对话。

当前时间：{now}（北京时间）

{memory}

{reviews}"""

TOOLS = [
    {"name": "get_quotes", "description": "查一只或多只股票/指数的最新行情和技术指标（收盘、涨跌幅、5日/20日、RSI、均线、信号）。代码用 Yahoo 格式：600519.SS、000858.SZ、0700.HK、AAPL、^GSPC。",
     "parameters": {"type": "object", "properties": {"symbols": {"type": "array", "items": {"type": "string"}}}, "required": ["symbols"]}},
    {"name": "market_overview", "description": "A股、港股、美股主要指数和自选股的一览。", "parameters": {"type": "object", "properties": {}}},
    {"name": "get_news", "description": "某只股票最近的新闻标题。", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "memory", "description": "维护长期记忆。target: user(用户画像) 或 memory(你的笔记)。action: add / replace / remove。replace 和 remove 用 old_text 定位唯一一条。",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["add", "replace", "remove"]},
         "target": {"type": "string", "enum": ["user", "memory"]},
         "content": {"type": "string"}, "old_text": {"type": "string"}}, "required": ["action", "target"]}},
    {"name": "session_search", "description": "全文检索过去所有对话。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "watchlist", "description": "管理自选股。action: list / add / remove。",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "add", "remove"]},
                                                     "symbol": {"type": "string"}, "name": {"type": "string"}}, "required": ["action"]}},
    {"name": "record_call", "description": "记录一次方向性判断，到期自动复盘。view 写 看涨/看跌/震荡。",
     "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "name": {"type": "string"},
                                                     "view": {"type": "string", "enum": ["看涨", "看跌", "震荡"]},
                                                     "horizon_days": {"type": "integer"}, "reason": {"type": "string"}},
                    "required": ["symbol", "view", "horizon_days", "reason"]}},
    {"name": "run_python", "description": "在隔离沙箱里运行一段 Python 做自定义分析（已装 pandas、yfinance、numpy）。用 print 输出结果；可以在当前目录读写文件，文件会保留。超时 60 秒。",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "call_stats", "description": "查看历史判断的胜率统计。", "parameters": {"type": "object", "properties": {}}},
]


def load_cfg() -> dict:
    return yaml.safe_load(config_path().read_text(encoding="utf-8"))


def save_watchlist(wl: list[dict]):
    p = config_path()
    text = p.read_text(encoding="utf-8")
    head, _, rest = text.partition("\nwatchlist:")
    # 保留 watchlist 之后的其它配置块
    tail = rest[rest.find("\n# 提醒阈值"):] if "\n# 提醒阈值" in rest else ""
    block = "\nwatchlist:\n" + ("".join(f'  - {{symbol: "{w["symbol"]}", name: "{w.get("name", w["symbol"])}"}}\n' for w in wl) if wl else " []\n")
    p.write_text(head + block + tail, encoding="utf-8")


def quotes(symbols: list[str]) -> dict:
    cfg = load_cfg()
    closes = data.fetch_closes(symbols)
    return {s: indicators.analyze(closes[s], cfg.get("alerts", {})) if s in closes else "无数据" for s in symbols}


def review_due(mem: Memory) -> list[str]:
    """学习闭环：到期的判断自动对照现价打分，结果交给模型反思。"""
    notes = []
    for c in mem.due_calls():
        q = quotes([c["symbol"]]).get(c["symbol"])
        if not isinstance(q, dict) or not q:
            continue
        pct = indicators.pct(q["close"], c["price"]) or 0.0
        right = (c["view"] == "看涨" and pct > 1) or (c["view"] == "看跌" and pct < -1) or (c["view"] == "震荡" and abs(pct) <= 3)
        verdict = "对" if right else "错"
        mem.close_call(c["id"], q["close"], pct, verdict)
        notes.append(f"{c['ts'][:10]} 判断 {c.get('name') or c['symbol']} {c['view']}（{c['horizon_days']}天，理由：{c['reason']}），"
                     f"从 {c['price']} 到 {q['close']}，{pct:+.2f}%，判断{verdict}。")
    return notes


class Agent:
    def __init__(self, mem: Memory | None = None, channel: str = "cli"):
        self.mem = mem or Memory()
        cfg = load_cfg().get("llm", {})
        self.key = os.getenv("LLM_API_KEY")
        self.base = os.getenv("LLM_BASE_URL", cfg.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        self.model = os.getenv("LLM_MODEL", cfg.get("model", "gpt-4o-mini"))
        self.session = f"{channel}-" + now().replace(" ", "_") + "-" + uuid.uuid4().hex[:4]
        self.tools = [t for t in TOOLS if t["name"] != "run_python" or sandbox.enabled()]
        self.turns = 0
        self.messages = [{"role": "system", "content": self._system()}]

    def review_due(self) -> list[str]:
        return review_due(self.mem)

    def _system(self) -> str:
        reviews = self.review_due()
        rv = ""
        if reviews:
            rv = ("## 刚到期的判断复盘\n" + "\n".join(f"- {r}" for r in reviews) +
                  "\n请在本次对话开头简要告诉用户复盘结果；判断错了的，想清楚错在哪，把可复用的教训用 memory 工具写进 MEMORY.md。")
        return SYSTEM.format(now=now(), memory=self.mem.snapshot(), reviews=rv)

    # ----- 工具 -----
    def run_tool(self, name: str, args: dict):
        m = self.mem
        if name == "get_quotes":
            return quotes(args["symbols"])
        if name == "market_overview":
            from .__main__ import build_snapshot
            return build_snapshot(load_cfg(), with_news=False)
        if name == "get_news":
            return data.fetch_news(args["symbol"], 5)
        if name == "memory":
            a, t = args["action"], args["target"]
            if a == "add":
                return m.add(t, args.get("content", ""))
            if a == "replace":
                return m.replace(t, args.get("old_text", ""), args.get("content", ""))
            return m.remove(t, args.get("old_text", ""))
        if name == "session_search":
            return m.search(args["query"])
        if name == "watchlist":
            wl = load_cfg().get("watchlist") or []
            if args["action"] == "add" and args.get("symbol"):
                if all(w["symbol"] != args["symbol"] for w in wl):
                    wl.append({"symbol": args["symbol"], "name": args.get("name") or args["symbol"]})
                    save_watchlist(wl)
            elif args["action"] == "remove":
                wl = [w for w in wl if w["symbol"] != args.get("symbol")]
                save_watchlist(wl)
            return wl
        if name == "record_call":
            q = quotes([args["symbol"]]).get(args["symbol"])
            if not isinstance(q, dict) or not q:
                return "取不到现价，未记录。"
            cid = m.record_call(args["symbol"], args.get("name", ""), args["view"], q["close"], args["horizon_days"], args["reason"])
            return f"已记账 #{cid}：{args['view']}，基准价 {q['close']}，{args['horizon_days']} 天后复盘。"
        if name == "call_stats":
            return m.call_stats()
        if name == "run_python":
            return sandbox.run_python(args["code"])
        return f"未知工具 {name}"

    # ----- 模型调用 -----
    def _chat(self, messages):
        r = requests.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": self.model, "temperature": 0.3, "messages": messages,
                  "tools": [{"type": "function", "function": t} for t in self.tools]},
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    def ask(self, text: str, on_tool=None) -> str:
        self.turns += 1
        self.mem.log(self.session, "user", text)
        content = text
        if self.turns % NUDGE_EVERY == 0:
            content += "\n\n[系统提醒：回顾最近几轮，有没有值得写进 USER.md 或 MEMORY.md 的长期信息？有就顺手存，没有就忽略这条提醒。]"
        self.messages.append({"role": "user", "content": content})
        for _ in range(8):
            msg = self._chat(self.messages)
            self.messages.append({k: v for k, v in msg.items() if v is not None})
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = msg.get("content") or ""
                self.mem.log(self.session, "assistant", reply)
                return reply
            for c in calls:
                name = c["function"]["name"]
                args = {}
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    result = self.run_tool(name, args)
                except Exception as e:
                    result = f"工具出错：{e}"
                if on_tool:
                    on_tool(name, args)
                self.messages.append({"role": "tool", "tool_call_id": c["id"],
                                      "content": json.dumps(result, ensure_ascii=False, default=str)[:6000]})
        return "（工具调用次数过多，已停止。）"

    def end_session(self):
        """会话结束时的后台复盘：让模型最后再检查一遍有没有该记住的。"""
        if self.turns == 0:
            return
        self.messages.append({"role": "user", "content":
            "[会话结束。回顾整段对话：把用户的长期信息更新进 USER.md，把你学到的经验更新进 MEMORY.md；"
            "已有条目过时就 replace/remove。没有值得存的就直接回复「无」。不要对用户说话。]"})
        for _ in range(4):
            msg = self._chat(self.messages)
            self.messages.append({k: v for k, v in msg.items() if v is not None})
            calls = msg.get("tool_calls") or []
            if not calls:
                break
            for c in calls:
                args = json.loads(c["function"].get("arguments") or "{}")
                res = self.run_tool(c["function"]["name"], args) if c["function"]["name"] == "memory" else "会话已结束，只允许 memory 工具。"
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(res, ensure_ascii=False)})


def repl():
    if not os.getenv("LLM_API_KEY"):
        print("需要先设置 LLM_API_KEY（任何 OpenAI 兼容接口，如 DeepSeek）。")
        return
    agent = Agent()
    print(f"股市 Agent · 已载入记忆（USER {agent.mem.usage('user')}，MEMORY {agent.mem.usage('memory')}）。输入 /exit 退出，/memory 查看记忆。\n")
    try:
        while True:
            text = input("你 › ").strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            if text == "/memory":
                print(agent.mem.snapshot(), "\n")
                continue
            try:
                reply = agent.ask(text, on_tool=lambda n, a: print(f"  · {n}"))
            except requests.RequestException as e:
                reply = f"（调用大模型失败：{e}）"
            print(f"\nAgent › {reply}\n")
    except (KeyboardInterrupt, EOFError):
        pass
    print("\n正在整理记忆…")
    try:
        agent.end_session()
    except requests.RequestException as e:
        print(f"（整理记忆失败：{e}）")
    print("已保存。下次见。")
