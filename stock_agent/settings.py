"""设置页后端：读写数据目录里的 .env 和 config.yaml，改完立即生效（写进 os.environ）。

分栏参考 2026 年主流 agent 客户端（ChatGPT / Claude 桌面版、Cherry Studio、Hermes Dashboard）：
通用 · 模型 · 记忆 · 自选与行情 · 推送渠道 · 定时任务 · 工具与接口 · 数据与关于。
密钥只回传末四位；空值表示「不改」，null 表示「清除」。
"""
import io
import os
import re
import zipfile

from . import __version__
from .agent import load_cfg, save_watchlist
from .memory import home
from . import skills, alerts
from .providers import PRESETS, LOCAL, custom_sources

# key: (所属分栏, 中文名, 是否密钥, 说明)
FIELDS = {
    "LLM_PROVIDER": ("model", "服务商", False, ""),
    "LLM_API_KEY": ("model", "API 密钥", True, "本地模型可不填"),
    "LLM_MODEL": ("model", "模型", False, "留空用服务商默认"),
    "LLM_BASE_URL": ("model", "接口地址", False, "中转 / 自建网关时填"),
    "LLM_FALLBACK": ("model", "备用模型", False, "主模型出错时依次换用，如 qwen,ollama:qwen2.5:7b"),
    "DATA_PROVIDERS": ("data", "行情源顺序", False, "tencent eastmoney akshare yahoo tushare finnhub alphavantage polygon twelvedata tiingo fmp custom"),
    "TUSHARE_TOKEN": ("data", "Tushare", True, ""),
    "FINNHUB_API_KEY": ("data", "Finnhub", True, ""),
    "ALPHAVANTAGE_API_KEY": ("data", "Alpha Vantage", True, ""),
    "POLYGON_API_KEY": ("data", "Polygon", True, ""),
    "TWELVEDATA_API_KEY": ("data", "Twelve Data", True, ""),
    "TIINGO_API_KEY": ("data", "Tiingo", True, ""),
    "FMP_API_KEY": ("data", "FMP", True, ""),
    "DATA_CUSTOM_URL": ("data", "自建接口", False, "{symbol} {days} 占位"),
    "SERVERCHAN_KEY": ("push", "Server酱", True, "推到微信"),
    "PUSHPLUS_TOKEN": ("push", "PushPlus", True, "推到微信"),
    "WECOM_WEBHOOK": ("push", "企业微信", True, "群机器人 Webhook"),
    "FEISHU_WEBHOOK": ("push", "飞书", True, "群机器人 Webhook"),
    "DINGTALK_WEBHOOK": ("push", "钉钉", True, "关键词加「股市」"),
    "TELEGRAM_BOT_TOKEN": ("push", "Telegram Bot", True, "也能双向对话"),
    "TELEGRAM_CHAT_ID": ("push", "Telegram Chat ID", False, "只回复这些 id，逗号分隔"),
    "BARK_KEY": ("push", "Bark", True, "iPhone 通知"),
    "NTFY_TOPIC": ("push", "ntfy 主题", False, "安卓 / iOS 通知"),
    "DISCORD_WEBHOOK": ("push", "Discord", True, ""),
    "SLACK_WEBHOOK": ("push", "Slack", True, ""),
    "WEBHOOK_URL": ("push", "通用 Webhook", True, "POST {title, markdown}"),
    "REPORT_TIMES": ("tasks", "报告时间", False, ""),
    "ALERT_INTERVAL": ("tasks", "提醒检查间隔（秒）", False, "默认 300，最小 60"),
    "ANN_WATCH": ("tasks", "自选股公告提醒", False, "1 开 / 0 关，默认开"),
    "TAVILY_API_KEY": ("tools", "Tavily 搜索", True, "可选，不填用 DuckDuckGo / Bing"),
    "BRAVE_API_KEY": ("tools", "Brave 搜索", True, "可选"),
    "ALLOW_CODE": ("tools", "允许写代码分析", False, ""),
    "WEB_TOKEN": ("tools", "访问口令", True, "放到公网时必须设"),
    "CORS_ORIGIN": ("tools", "跨域来源", False, ""),
}


def env_path():
    return home() / ".env"


def mask(v: str) -> str:
    return ("•••• " + v[-4:]) if len(v) > 8 else ("••••" if v else "")


def load_env_file():
    """常驻模式启动时读数据目录的 .env（不覆盖已有环境变量），让设置页保存的值重启后还在。"""
    p = env_path()
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1) not in os.environ:
            v = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip('"').strip("'")
            if v:
                os.environ[m.group(1)] = v


def write_env(changes: dict):
    p = env_path()
    lines = p.read_text(encoding="utf-8-sig").splitlines() if p.exists() else ["# Stoa 设置（设置页会改写这里）"]
    left = dict(changes)
    for i, line in enumerate(lines):
        m = re.match(r"\s*#?\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in left:
            k = m.group(1)
            lines[i] = f"{k}={left.pop(k)}"
    lines += [f"{k}={v}" for k, v in left.items()]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get(mem) -> dict:
    cfg = load_cfg()
    env = {}
    for k, (sec, label, secret, hint) in FIELDS.items():
        v = os.getenv(k, "")
        env[k] = {"section": sec, "label": label, "secret": secret, "hint": hint,
                  "value": mask(v) if secret else v, "set": bool(v)}
    if not env["LLM_PROVIDER"]["value"]:
        env["LLM_PROVIDER"]["value"] = (cfg.get("llm") or {}).get("provider", "deepseek")
    if not env["REPORT_TIMES"]["value"]:
        env["REPORT_TIMES"]["value"] = "morning=08:30,evening=21:00"
    return {
        "version": __version__,
        "home": str(home()),
        "env": env,
        "providers": [{"id": n, "model": p[2], "base": p[1], "local": n in LOCAL} for n, p in PRESETS.items()]
                     + [{"id": d["id"], "name": d.get("name") or d["id"], "model": d.get("model", ""), "base": d["base"], "proto": d.get("proto", "openai"),
                         "custom": True, "key": mask(d.get("key", ""))} for d in custom_sources()],
        "watchlist": cfg.get("watchlist") or [],
        "alerts": cfg.get("alerts") or {},
        "pa": alerts.listing(),
        "memory": {"user": mem.entries("user"), "memory": mem.entries("memory"),
                   "usage": {"user": mem.usage("user"), "memory": mem.usage("memory")},
                   "skills": skills.list_skills(), "stats": mem.call_stats()},
    }


def save(body: dict) -> dict:
    changes = {}
    for k, v in (body.get("env") or {}).items():
        if k not in FIELDS:
            continue
        if v is None:  # 清除
            changes[k] = ""
            os.environ.pop(k, None)
        elif isinstance(v, str) and (v.strip() or not FIELDS[k][2]):  # 密钥留空 = 不改
            v = v.strip().replace("\n", "")
            changes[k] = v
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
    if changes:
        write_env(changes)
    if isinstance(body.get("watchlist"), list):
        wl = []
        for w in body["watchlist"]:
            s = re.sub(r"[^A-Za-z0-9.^=\-]", "", str(w.get("symbol", "")))[:20]
            if s:
                wl.append({"symbol": s.upper() if not s[0].isdigit() else s.upper(),
                           "name": re.sub(r'["\n{}]', "", str(w.get("name") or s))[:20]})
        save_watchlist(wl)
    return {"ok": True, "changed": sorted(changes)}


def probe_models(body: dict) -> dict:
    """自动检测模型：服务商 + 表单里填的地址/密钥（留空用已保存的），返回聊天模型列表。"""
    from .providers import LLM, fetch_models, custom_source
    prov = str(body.get("provider") or "").lower() or None
    try:
        cur = LLM(provider=prov, cfg=(load_cfg().get("llm") or {}))
    except Exception as e:
        return {"ok": False, "models": [], "msg": str(e)[:120]}
    src = custom_source(prov) if prov and prov not in PRESETS else None
    base = (str(body.get("base") or "").strip() or ("" if src else os.getenv("LLM_BASE_URL", "")) or cur.base).rstrip("/")
    key = str(body.get("key") or "").strip() or cur.key
    proto = body.get("proto") or ("openai" if cur.proto == "azure" else cur.proto)
    if not base:
        return {"ok": False, "models": [], "msg": "先填接口地址"}
    if not key and prov not in LOCAL:
        return {"ok": False, "models": [], "msg": "先填 API 密钥"}
    try:
        ms = fetch_models(base, key, proto)
    except Exception as e:
        return {"ok": False, "models": [], "msg": f"检测失败：{str(e)[:100]}"}
    return {"ok": True, "models": ms, "msg": f"检测到 {len(ms)} 个模型" if ms else "接口通了，但没有返回模型列表，手动填写即可"}


def test_llm() -> dict:
    from .providers import Router
    try:
        r = Router(load_cfg().get("llm", {}))
        if not r.ready:
            return {"ok": False, "msg": "还没填密钥"}
        m = r.chat([{"role": "user", "content": "只回复两个字：在线"}], timeout=30)
        return {"ok": True, "msg": f"{r} · {(m.get('content') or '').strip()[:40]}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:300]}


def test_push() -> dict:
    from .notify import push
    try:
        sent = push("Stoa 测试推送", "设置页发来的测试消息。看到这条说明推送通了。")
        return {"ok": bool(sent), "msg": "已推送：" + "、".join(sent) if sent else "没有配置任何推送渠道"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:300]}


def export_zip() -> bytes:
    """备份记忆、账本、配置和报告；不含 .env（里面是密钥）。"""
    buf = io.BytesIO()
    h = home()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in ("config.yaml", "memory", "skills", "data", "reports"):
            p = h / rel
            if p.is_file():
                z.write(p, rel)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file() and not f.name.endswith(("-wal", "-shm")):
                        z.write(f, f.relative_to(h).as_posix())
    return buf.getvalue()
