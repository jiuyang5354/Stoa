"""可选推送：设置对应环境变量就会推送，不设置就跳过。"""
import os

import requests


def push(title: str, markdown: str) -> list[str]:
    sent = []
    if key := os.getenv("SERVERCHAN_KEY"):  # Server酱 → 微信
        requests.post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title, "desp": markdown}, timeout=30)
        sent.append("Server酱")
    if hook := os.getenv("WECOM_WEBHOOK"):  # 企业微信群机器人
        requests.post(hook, json={"msgtype": "markdown", "markdown": {"content": markdown[:4000]}}, timeout=30)
        sent.append("企业微信")
    if hook := os.getenv("FEISHU_WEBHOOK"):  # 飞书群机器人
        requests.post(hook, json={"msg_type": "text", "content": {"text": f"{title}\n\n{markdown[:4000]}"}}, timeout=30)
        sent.append("飞书")
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat, "text": f"{title}\n\n{markdown[:3900]}"},
            timeout=30,
        )
        sent.append("Telegram")
    if hook := os.getenv("DINGTALK_WEBHOOK"):  # 钉钉群机器人（关键词安全设置里加「股市」）
        requests.post(hook, json={"msgtype": "markdown", "markdown": {"title": title, "text": markdown[:4000]}}, timeout=30)
        sent.append("钉钉")
    if key := os.getenv("PUSHPLUS_TOKEN"):  # PushPlus → 微信
        requests.post("https://www.pushplus.plus/send", json={"token": key, "title": title, "content": markdown, "template": "markdown"}, timeout=30)
        sent.append("PushPlus")
    if key := os.getenv("BARK_KEY"):  # Bark → iPhone 通知
        base = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")
        requests.post(f"{base}/push", json={"device_key": key, "title": title, "body": markdown[:3000], "group": "股市Agent"}, timeout=30)
        sent.append("Bark")
    if topic := os.getenv("NTFY_TOPIC"):  # ntfy → 安卓/iOS 通知
        base = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        requests.post(base, json={"topic": topic, "title": title, "message": markdown[:4000], "markdown": True}, timeout=30)
        sent.append("ntfy")
    if hook := os.getenv("DISCORD_WEBHOOK"):
        requests.post(hook, json={"content": f"**{title}**\n{markdown[:1900]}"}, timeout=30)
        sent.append("Discord")
    if hook := os.getenv("SLACK_WEBHOOK"):
        requests.post(hook, json={"text": f"*{title}*\n{markdown[:3900]}"}, timeout=30)
        sent.append("Slack")
    if hook := os.getenv("WEBHOOK_URL"):  # 通用 Webhook：POST JSON {title, markdown}
        requests.post(hook, json={"title": title, "markdown": markdown}, timeout=30)
        sent.append("Webhook")
    return sent
