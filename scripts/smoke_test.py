"""Smoke-test the core pipeline without any external services.

Run with:  python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure the project root is importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0000000000:smoke-test-token")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./smoke.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")


async def main() -> None:
    from app.core.config import get_settings  # noqa: F401
    from app.core.database import SessionLocal, init_db
    from app.core.security.crypto import get_cipher
    from app.db.repositories import get_or_create_user
    from app.models import InstagramAccount
    from app.services.ai import generate_caption
    from app.services.insights import build_health_report
    from app.services.meta.service import InstagramService

    # 1) crypto round-trip
    cipher = get_cipher()
    ct = cipher.encrypt("super-secret-token")
    assert ct.startswith("enc:")
    assert cipher.decrypt(ct) == "super-secret-token"
    print("✅ crypto round-trip OK")

    # 2) db + tenant creation
    await init_db()
    async with SessionLocal() as session:
        u = await get_or_create_user(session, 123456, username="testuser", full_name="Test")
        acc = InstagramAccount(
            owner_id=u.id, username="demo.page", name="Demo",
            long_lived_token_enc=cipher.encrypt("token-A"),
        )
        session.add(acc)
        await session.commit()
        acc_id = acc.id
        print("✅ tenancy + encrypted token stored")

    # 3) isolation: another user must NOT see the account
    from app.db.repositories import get_accounts_for_user

    async with SessionLocal() as session:
        other = await get_or_create_user(session, 999999, full_name="Other")
        theirs = await get_accounts_for_user(session, 999999)
        assert theirs == [], "isolation violated!"
        print("✅ data isolation OK")

    # 4) simulated publish
    async with SessionLocal() as session:
        acc = await session.get(InstagramAccount, acc_id)
        svc = InstagramService()
        res = await svc.publish_media(acc, "https://example.com/x.jpg", "hello")
        assert "id" in res
        print("✅ simulated publish OK:", res["id"])

    # 5) health report
    async with SessionLocal() as session:
        acc = await session.get(InstagramAccount, acc_id)
        report = await build_health_report(acc)
        assert report.metrics, "no metrics"
        print("✅ health report OK")

    # 6) caption engine (fallback)
    cap = await generate_caption("فروش ویژه", tone="friendly", language="fa")
    assert cap.strip()
    print("✅ caption engine OK ->", cap[:40])

    print("\n🎉 ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
