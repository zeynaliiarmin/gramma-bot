#!/usr/bin/env python3
"""Install (or inspect) the Instagram webhook subscription for your Meta app.

After your app is provisioned and the bot is running with a public HTTPS URL,
run this once to tell Meta which webhook fields to deliver:

    python scripts/install_webhook_subscription.py \
        --app-token EAAG... \
        --ig-user-id 1784140... \
        --callback https://your-domain.example/webhook/instagram

Fields:
    comments  — new comments on the page's media
    mentions  — @mentions of the page
    messages  — new DMs (conversations)
    story_insights — story metrics (optional)
"""

from __future__ import annotations

import argparse
import sys

try:
    import requests
except ImportError:
    print("requests is required: pip install requests", file=sys.stderr)
    sys.exit(1)

FIELDS = ["comments", "mention", "messages"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Subscribe Instagram webhook fields")
    parser.add_argument("--app-token", required=True, help="Meta app access token")
    parser.add_argument("--ig-user-id", required=True, help="Instagram Business account id")
    parser.add_argument("--callback", required=True, help="full webhook URL")
    parser.add_argument("--verify-token", default=None, help="must match META_VERIFY_TOKEN")
    args = parser.parse_args()

    verify_token = args.verify_token or input("META_VERIFY_TOKEN: ")

    # 1) App-level webhook target
    r = requests.post(
        f"https://graph.facebook.com/v21.0/{args.ig_user_id}/subscribed_apps",
        params={
            "subscribed_fields": ",".join(FIELDS),
            "access_token": args.app_token,
        },
        timeout=30,
    )
    print("[subscribed_apps]", r.status_code, r.text[:500])
    r.raise_for_status()

    # 2) Page subscription (older API shape; keep for compatibility)
    r2 = requests.post(
        "https://graph.facebook.com/v21.0/me/subscriptions",
        params={
            "object": "instagram",
            "callback_url": args.callback,
            "fields": ",".join(FIELDS),
            "verify_token": verify_token,
            "access_token": args.app_token,
        },
        timeout=30,
    )
    print("[subscriptions]", r2.status_code, r2.text[:500])

    print("\n✅ Done. Webhook events for", ", ".join(FIELDS), "should now arrive at", args.callback)


if __name__ == "__main__":
    main()
