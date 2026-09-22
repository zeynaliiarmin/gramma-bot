"""Tests for Instagram publisher (OpenClaw private-enabled)."""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:test")
os.environ.setdefault("ENCRYPTION_KEY", "9f6Bd2S19LFy/SQ/9Om4559N1Lgi5upd2zZGwvITWOA=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests_publisher.db")
os.environ.setdefault("INSTAGRAM_ACCOUNT_MODE", "simulation")
os.environ.setdefault("INSTAGRAM_PUBLISH_ENABLED", "true")
os.environ.setdefault("INSTAGRAM_PUBLISH_DAILY_LIMIT", "3")
os.environ.setdefault("INSTAGRAM_PUBLISH_MODE", "private-enabled")

import pytest
from datetime import datetime, timezone

from app.core.database import SessionLocal, init_db
from app.models import InstagramAccount, User


@pytest.fixture()
async def db():
    await init_db()
    async with SessionLocal() as session:
        from sqlalchemy import delete
        from app.models import ActivityLog, InstagramAccount, User

        for model in (ActivityLog, InstagramAccount, User):
            await session.execute(delete(model))
        await session.commit()

        user = User(id=1, telegram_username="tester", full_name="T", locale="fa")
        session.add(user)
        acc = InstagramAccount(
            id=10, owner_id=1, username="testpage", name="Test Page", status="connected",
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        session.add(acc)
        await session.commit()
        yield session


@pytest.mark.asyncio
async def test_publisher_status(db):
    from app.services.instagram_publisher import get_publisher

    pub = get_publisher()
    status = await pub.get_status(account_id=10)
    assert "enabled" in status
    assert "daily_limit" in status
    assert status["daily_limit"] == 3
    assert "mock_mode" in status


@pytest.mark.asyncio
async def test_publisher_daily_limit_enforcement(db):
    from app.services.instagram_publisher import get_publisher, InstagramPublishError
    from app.models import ActivityLog

    pub = get_publisher()
    # Simulate 3 publishes today
    async with SessionLocal() as session:
        for i in range(3):
            session.add(
                ActivityLog(
                    account_id=10,
                    user_id=1,
                    action="instagram_publish",
                    detail=f"test publish {i}",
                    level="info",
                )
            )
        await session.commit()

    # 4th should fail
    try:
        await pub.publish_post(
            account_id=10,
            image_url="https://example.com/image.jpg",
            caption="Test caption",
            media_type="post",
            user_id=1,
        )
        # If mock mode, it checks limit via DB — should raise
        assert False, "Should have raised limit error"
    except InstagramPublishError as exc:
        assert "سقف انتشار" in str(exc) or "limit" in str(exc).lower() or "3" in str(exc)


@pytest.mark.asyncio
async def test_publisher_mock_publish(db):
    from app.services.instagram_publisher import get_publisher

    # Clear logs first
    async with SessionLocal() as session:
        from sqlalchemy import delete
        from app.models import ActivityLog

        await session.execute(delete(ActivityLog).where(ActivityLog.account_id == 10))
        await session.commit()

    pub = get_publisher()
    result = await pub.publish_post(
        account_id=10,
        image_url="https://example.com/photo.jpg",
        caption="کپشن تست برای پست اینستاگرام",
        media_type="post",
        user_id=1,
    )
    assert result["ok"] is True
    assert "media_id" in result
    assert result["media_type"] == "post"


@pytest.mark.asyncio
async def test_publisher_story_and_reel(db):
    from app.services.instagram_publisher import get_publisher
    from sqlalchemy import delete
    from app.models import ActivityLog

    async with SessionLocal() as session:
        await session.execute(delete(ActivityLog).where(ActivityLog.account_id == 10))
        await session.commit()

    pub = get_publisher()

    # Story
    res_story = await pub.publish_post(
        account_id=10,
        image_url="https://example.com/story.jpg",
        caption="استوری تست",
        media_type="story",
        user_id=1,
    )
    assert res_story["ok"] is True

    # Clear again for reel
    async with SessionLocal() as session:
        await session.execute(delete(ActivityLog).where(ActivityLog.account_id == 10))
        await session.commit()

    res_reel = await pub.publish_post(
        account_id=10,
        image_url="https://example.com/reel.mp4",
        caption="ریلز تست",
        media_type="reel",
        user_id=1,
    )
    assert res_reel["ok"] is True


@pytest.mark.asyncio
async def test_publisher_carousel_validation():
    from app.services.instagram_publisher import get_publisher, InstagramPublishError

    pub = get_publisher()
    try:
        await pub.publish_post(
            account_id=10,
            caption="کاروسل بدون عکس",
            media_type="carousel",
            user_id=1,
        )
        assert False, "Should have raised validation error"
    except InstagramPublishError as exc:
        assert "کاروسل" in str(exc) or "carousel" in str(exc).lower() or "2" in str(exc)
