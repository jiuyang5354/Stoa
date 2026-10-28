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
    return sent
