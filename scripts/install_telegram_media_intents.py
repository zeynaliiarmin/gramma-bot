"""Apply only the additive private Telegram media-intent/caption-move migration.

No cron, webhook, publishing worker, Zernio API, or Telegram API is called.
Checks the authorized Supabase ref and the pre-existing Phase 2 draft table.
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv

from install_telegram_album_cron import EXPECTED_REF, ROOT, _query


def main() -> int:
    load_dotenv(ROOT / ".env")
    if os.getenv("SUPABASE_PROJECT_REF") != EXPECTED_REF or not os.getenv("SUPABASE_PAT"):
        print("Permitted Supabase ref or management credential missing; no changes made.")
        return 2
    try:
        with httpx.Client(timeout=90.0) as client:
            token = os.environ["SUPABASE_PAT"]
            present = _query(client, token, """
                SELECT to_regclass('public.telegram_post_drafts') IS NOT NULL AS installed
            """)
            if len(present) != 1 or not present[0]["installed"]:
                raise RuntimeError("Pre-existing Phase 2 drafts not found")
            sql = (ROOT / "scripts" / "migrate_telegram_media_intents.sql").read_text(encoding="utf-8")
            _query(client, token, "BEGIN;\n" + sql + "\nCOMMIT;")
            verified = _query(client, token, """
                SELECT to_regclass('public.telegram_media_intents') IS NOT NULL AS installed,
                       (SELECT relrowsecurity FROM pg_class
                         WHERE oid = to_regclass('public.telegram_media_intents')) AS rls,
                       (SELECT COUNT(*) = 3 FROM information_schema.columns
                         WHERE table_schema='public' AND table_name='telegram_post_drafts'
                           AND column_name IN ('media_intent','caption_moved_to_comment','visible_caption'))
                           AS columns_ready
            """)
            if len(verified) != 1 or not all(verified[0].values()):
                raise RuntimeError("New table, RLS or additive draft columns not visible")
        print("Private media intents and caption-move fields installed; no posts or cron jobs touched.")
        return 0
    except Exception as exc:  # noqa: BLE001
        # Never print SQL/API response bodies or environment credentials.
        print(f"Additive Telegram migration failed ({type(exc).__name__}); no Worker invoked.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
