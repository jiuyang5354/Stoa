"""可选推送：设置对应环境变量就会推送，不设置就跳过。

v0.34：
- 幂等：push(..., key=…) 带键时，按「键 + 渠道」记已发（ledger.outbox），重试 / 补跑同一个键不会重复推；某个渠道失败下次只补那个渠道。
- 新渠道要确认：第一次往一个新开的渠道发消息前，挂一条待确认（ledger.approvals，重启后仍在），用户批准后补发这条并信任该渠道。
  升级到 v0.34 时已经配好的渠道视为已确认（不打断原有推送）。PUSH_CONFIRM_NEW=off 关掉这道闸。
"""
import os

import requests


def _channels() -> list[tuple[str, callable]]:
    """[(渠道名, send(title, markdown))]，只列已配置的。"""
    ch = []
    if key := os.getenv("SERVERCHAN_KEY"):  # Server酱 → 微信
        ch.append(("Server酱", lambda t, m: requests.post(f"https://sctapi.ftqq.com/{key}.send", data={"title": t, "desp": m}, timeout=30)))
    if hook := os.getenv("WECOM_WEBHOOK"):  # 企业微信群机器人
        ch.append(("企业微信", lambda t, m: requests.post(hook, json={"msgtype": "markdown", "markdown": {"content": m[:4000]}}, timeout=30)))
    if hook2 := os.getenv("FEISHU_WEBHOOK"):  # 飞书群机器人
        ch.append(("飞书", lambda t, m: requests.post(hook2, json={"msg_type": "text", "content": {"text": f"{t}\n\n{m[:4000]}"}}, timeout=30)))
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        ch.append(("Telegram", lambda t, m: requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                                                          json={"chat_id": tg_chat, "text": f"{t}\n\n{m[:3900]}"}, timeout=30)))
    if hook3 := os.getenv("DINGTALK_WEBHOOK"):  # 钉钉群机器人（关键词安全设置里加「股市」）
        ch.append(("钉钉", lambda t, m: requests.post(hook3, json={"msgtype": "markdown", "markdown": {"title": t, "text": m[:4000]}}, timeout=30)))
    if key2 := os.getenv("PUSHPLUS_TOKEN"):  # PushPlus → 微信
        ch.append(("PushPlus", lambda t, m: requests.post("https://www.pushplus.plus/send", json={"token": key2, "title": t, "content": m, "template": "markdown"}, timeout=30)))
    if key3 := os.getenv("BARK_KEY"):  # Bark → iPhone 通知
        base = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")
        ch.append(("Bark", lambda t, m: requests.post(f"{base}/push", json={"device_key": key3, "title": t, "body": m[:3000], "group": "Stoa"}, timeout=30)))
    if topic := os.getenv("NTFY_TOPIC"):  # ntfy → 安卓/iOS 通知
        nb = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        ch.append(("ntfy", lambda t, m: requests.post(nb, json={"topic": topic, "title": t, "message": m[:4000], "markdown": True}, timeout=30)))
    if hook4 := os.getenv("DISCORD_WEBHOOK"):
        ch.append(("Discord", lambda t, m: requests.post(hook4, json={"content": f"**{t}**\n{m[:1900]}"}, timeout=30)))
    if hook5 := os.getenv("SLACK_WEBHOOK"):
        ch.append(("Slack", lambda t, m: requests.post(hook5, json={"text": f"*{t}*\n{m[:3900]}"}, timeout=30)))
    if hook6 := os.getenv("WEBHOOK_URL"):  # 通用 Webhook：POST JSON {title, markdown}
        ch.append(("Webhook", lambda t, m: requests.post(hook6, json={"title": t, "markdown": m}, timeout=30)))
    return ch


def configured() -> list[str]:
    return [n for n, _ in _channels()]


def _gate_on() -> bool:
    return os.getenv("PUSH_CONFIRM_NEW", "on").lower() not in ("off", "0", "false", "no")


def _seed(names: list[str]):
    """第一次走到这里（刚升级）时，把已配置的渠道记为已确认。"""
    from . import ledger
    if ledger.meta("channels_seeded"):
        return
    for n in names:
        ledger.trust(n, "upgrade")
    ledger.meta("channels_seeded", ledger.now())


def _ok(r) -> bool:
    code = getattr(r, "status_code", 200)
    return code is None or code < 400


def push(title: str, markdown: str, key: str | None = None, only: list[str] | None = None) -> list[str]:
    """推到所有已配置渠道，返回这次实际发出的渠道名。key：幂等键；only：只发这些渠道（批准新渠道后补发用）。"""
    from . import ledger
    chans = [c for c in _channels() if not only or c[0] in only]
    if not chans:
        return []
    _seed([n for n, _ in _channels()])
    done = ledger.outbox_sent(key) if key else set()
    sent = []
    for name, send in chans:
        if name in done:
            continue  # 这个键在这个渠道已经发过（重试 / 补跑）
        if _gate_on() and not ledger.trusted(name) and not only:
            ledger.approval_add("push_channel", f"开始往新渠道「{name}」推送", {"channel": name, "title": title, "markdown": markdown[:6000], "key": key},
                                dedupe=f"push_channel:{name}")
            continue
        try:
            if not _ok(send(title, markdown)):
                continue
        except Exception:
            continue  # 下次同一个键重试时只补这个渠道
        sent.append(name)
        if key:
            ledger.outbox_mark(key, name, title)
    return sent


def _approve_channel(p: dict) -> dict:
    from . import ledger
    ledger.trust(p["channel"], "user")
    sent = push(p.get("title") or "Stoa", p.get("markdown") or "", key=p.get("key"), only=[p["channel"]])
    return {"trusted": p["channel"], "sent": sent}


def _register():
    from . import ledger
    ledger.HANDLERS["push_channel"] = _approve_channel


_register()
