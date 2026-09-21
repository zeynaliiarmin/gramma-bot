"""Live end-to-end test: Gramma's OpenClawClient vs a REAL OpenClaw gateway.

Run from the project root with the venv python:
    .venv/bin/python scripts/openclaw_smoke.py

Exercises all six smart capabilities through the real /api/ask route and
verifies the joint's persistence in activity_logs.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./openclaw_smoke.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")


async def main() -> None:
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.core.database import SessionLocal, init_db
    from app.models import ActivityLog
    from app.services.openclaw import (
        OpenClawClient,
        analyze_page,
        generate_caption_via_openclaw,
        generate_post_ideas,
        suggest_comment_reply,
        suggest_dm_reply,
        web_search_via_openclaw,
    )

    settings = get_settings()
    print(f"OPENCLAW_BASE_URL = {settings.openclaw_base_url}")
    print(f"OPENCLAW_TIMEOUT   = {settings.openclaw_timeout}")
    print(f"OPENCLAW_TOKEN     = {'set' if settings.openclaw_token else 'EMPTY'}")

    client = OpenClawClient()
    assert client.enabled, "OpenClaw client disabled!"

    await init_db()
    results: dict[str, str] = {}
    results["caption"] = await generate_caption_via_openclaw(
        "کفش چرم مردانه", tone="فروش", language="fa", user_id=495432021, account_id=1
    )
    results["comment_reply"] = await suggest_comment_reply(
        "قیمت رو می‌فرستید؟", username="sara", user_id=495432021, account_id=1
    )
    results["dm_reply"] = await suggest_dm_reply(
        "سلام، چطور می‌تونم سفارش بدم؟", sender_name="علی", user_id=495432021, account_id=1
    )
    results["post_ideas"] = await generate_post_ideas(
        "کافه‌ی دنج", count=3, user_id=495432021, account_id=1
    )
    results["web_search"] = await web_search_via_openclaw(
        "بهترین زمان پست اینستاگرام", user_id=495432021, account_id=1
    )
    results["analytics"] = await analyze_page(
        {"followers": 3200, "media_count": 48, "trend": "+12%"},
        user_id=495432021, account_id=1,
    )

    ok_all = True
    for name, text in results.items():
        good = bool(text and len(text.strip()) > 3)
        ok_all &= good
        print(f"\n=== {name} ({'OK' if good else 'FAIL'}) ===")
        print(text[:200])

    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(ActivityLog).where(ActivityLog.user_id == 495432021)))
            .scalars()
            .all()
        )
    actions = {r.action for r in rows}
    expected = {
        "openclaw_caption", "openclaw_comment_reply", "openclaw_dm_reply",
        "openclaw_post_ideas", "openclaw_web_search", "openclaw_analytics",
    }
    print(f"\nactivity_logs rows for user 495432021: {len(rows)}")
    print("actions:", sorted(actions))
    assert expected <= actions, f"missing logs: {expected - actions}"

    print("\nALL OPENCLAW E2E CHECKS PASSED ✅" if ok_all else "\nSOME CHECKS FAILED ❌")


if __name__ == "__main__":
    asyncio.run(main())
