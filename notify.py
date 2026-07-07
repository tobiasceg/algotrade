"""Telegram alerts. One HTTP POST — no bot framework needed.

Setup (once, ~5 minutes):
  1. Message @BotFather on Telegram, /newbot, copy the token.
  2. Send your new bot any message, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates
     and read your chat id from the response.
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (GitHub secrets / env).

Fail-soft by design: a Telegram outage or missing config prints the message
locally and never blocks trading logic.
"""

import os

import requests


def send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[notify] telegram not configured — message follows:")
        print(text)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        if not resp.ok:
            print(f"[notify] telegram error {resp.status_code}: {resp.text}")
        return resp.ok
    except requests.RequestException as exc:
        print(f"[notify] telegram unreachable: {exc}")
        return False
