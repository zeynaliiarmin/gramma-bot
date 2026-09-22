"""Gramma — set/clear the Telegram webhook for the serverless runtime.

Points Telegram at the Vercel function so every update lands on
``{MINIAPP_PUBLIC_URL}/api/telegram/webhook`` with the shared secret token
(TELEGRAM_WEBHOOK_SECRET), and disables getUpdates-only polling.

Usage:
    python scripts/set_telegram_webhook.py           # set webhook
    python scripts/set_telegram_webhook.py --unset   # delete webhook (polling back)

Credentials (from .env / process env, never committed):
    TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, MINIAPP_PUBLIC_URL
Optional override: --url https://… --secret …
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import httpx  # noqa: E402

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "inline_query",
    "web_app_data",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unset", action="store_true", help="delete the webhook")
    parser.add_argument("--url", default="", help="override webhook URL")
    parser.add_argument("--secret", default="", help="override secret token")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("✗ TELEGRAM_BOT_TOKEN is not set")
        return 2

    base = os.environ.get("MINIAPP_PUBLIC_URL", "").rstrip("/")
    secret = args.secret or os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    url = args.url or (f"{base}/api/telegram/webhook" if base else "")
    if not url:
        print("✗ webhook URL unknown — set MINIAPP_PUBLIC_URL or pass --url")
        return 2

    api = f"https://api.telegram.org/bot{token}"

    if args.unset:
        r = httpx.post(f"{api}/deleteWebhook", json={"drop_pending_updates": False})
        print("deleteWebhook →", r.status_code, r.text[:200])
        return 0

    payload = {"url": url, "allowed_updates": ALLOWED_UPDATES}
    if secret:
        payload["secret_token"] = secret
    r = httpx.post(f"{api}/setWebhook", json=payload, timeout=30.0)
    print("setWebhook →", r.status_code)
    print(r.text[:300])
    if r.status_code == 200:
        info = httpx.post(f"{api}/getWebhookInfo", timeout=30.0).json()
        print("getWebhookInfo →", info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
