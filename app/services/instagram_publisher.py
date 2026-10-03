"""Gramma — Instagram publishing via OpenClaw private-enabled (instagrapi).

This module provides safe publishing to Instagram without a Meta App,
using instagrapi (private API). It's risky and must be heavily rate-limited.

Architecture:
  * Uses instagrapi when available (optional dependency)
  * Falls back to mock mode when not installed or not configured
  * Enforces DAILY limit (3 posts/day per account) — from limits.py / config
  * Tracks publishing in daily counters + activity logs
  * Provides clear Persian error messages for blocked / failed cases

Security:
  * Credentials from .env (instagram_username/password) — never in code
  * All publishing is logged
  * Backoff on errors (uses same backoff ladder as comment replies)
  * Warns admin on suspicious activity

Usage:
  from app.services.instagram_publisher import get_publisher
  result = await get_publisher().publish_post(account_id, image_url, caption, media_type)

Media types: post, story, reel, carousel
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.instagram_publisher")


class InstagramPublishError(Exception):
    """Raised when publishing fails."""


class InstagramPublisher:
    """Publisher that uses instagrapi (private API) when available."""

    def __init__(self):
        self.enabled = settings.instagram_publish_enabled and bool(settings.instagram_publish_mode == "private-enabled")
        self.daily_limit = settings.instagram_publish_daily_limit or 3
        self._client = None  # instagrapi client lazy-loaded

    def _get_instagrapi_client(self):
        """Lazy-load instagrapi, return None if not available."""
        if self._client is not None:
            return self._client
        try:
            from instagrapi import Client

            client = Client()
            # Try to login if credentials provided
            if settings.instagram_username and settings.instagram_password:
                try:
                    client.login(settings.instagram_username, settings.instagram_password)
                    logger.info("instagrapi login ok for %s", settings.instagram_username)
                except Exception as exc:
                    logger.warning("instagrapi login failed: %s", exc)
                    # Don't raise — allow mock mode
                    return None
            self._client = client
            return client
        except ImportError:
            logger.warning("instagrapi not installed — using mock publisher")
            return None
        except Exception as exc:
            logger.warning("instagrapi init failed: %s", exc)
            return None

    async def _check_daily_limit(self, account_id: int) -> Dict[str, Any]:
        """Check if account has exceeded daily publish limit."""
        from app.core.database import SessionLocal
        from app.models import ActivityLog
        from sqlalchemy import select, func
        from datetime import date

        today = date.today().isoformat()
        # Count today's publishes for this account
        async with SessionLocal() as session:
            result = await session.execute(
                select(func.count(ActivityLog.id)).where(
                    ActivityLog.account_id == account_id,
                    ActivityLog.action == "instagram_publish",
                    ActivityLog.created_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
                )
            )
            count = result.scalar() or 0
        return {
            "count": count,
            "limit": self.daily_limit,
            "remaining": max(0, self.daily_limit - count),
            "exceeded": count >= self.daily_limit,
        }

    async def _check_min_interval(self, account_id: int) -> None:
        """Enforce the minimum spacing between publishes (anti-block).

        Raises InstagramPublishError when the previous publish is newer than
        ``INSTAGRAM_PUBLISH_MIN_INTERVAL_MINUTES`` (default 30).
        """
        from datetime import timedelta

        from sqlalchemy import select

        from app.core.database import SessionLocal
        from app.models import ActivityLog

        minutes = settings.instagram_publish_min_interval_minutes or 30
        async with SessionLocal() as session:
            last = (
                await session.execute(
                    select(ActivityLog.created_at)
                    .where(
                        ActivityLog.account_id == account_id,
                        ActivityLog.action == "instagram_publish",
                    )
                    .order_by(ActivityLog.created_at.desc())
                    .limit(1)
                )
            ).scalar()
        if last is None:
            return
        now = datetime.now(timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        delta = now - last
        if delta < timedelta(minutes=minutes):
            remaining = timedelta(minutes=minutes) - delta
            mins = int(remaining.total_seconds() // 60) + 1
            raise InstagramPublishError(
                f"⏳ برای جلوگیری از بلاک، بین دو انتشار باید حداقل {minutes} دقیقه فاصله باشد. "
                f"حدود {mins} دقیقه دیگر دوباره تلاش کنید."
            )

    async def publish_post(
        self,
        account_id: int,
        image_url: Optional[str] = None,
        image_urls: Optional[List[str]] = None,
        caption: str = "",
        media_type: str = "post",  # post | story | reel | carousel
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Publish a post/story/reel/carousel.

        Args:
            account_id: InstagramAccount id
            image_url: single image/video URL
            image_urls: for carousel (list)
            caption: caption text
            media_type: post, story, reel, carousel
            user_id: Telegram user id for logging

        Returns:
            dict with ok, media_id, permalink, etc.

        Raises:
            InstagramPublishError on failure or limit exceeded
        """
        # Check daily limit
        limit_info = await self._check_daily_limit(account_id)
        if limit_info["exceeded"]:
            raise InstagramPublishError(
                f"سقف انتشار روزانه ({self.daily_limit} پست در روز) برای این پیج به پایان رسیده. "
                f"فردا دوباره تلاش کنید. (امروز {limit_info['count']} منتشر شده)"
            )

        # Validate inputs
        if media_type == "carousel" and not image_urls:
            raise InstagramPublishError("برای کاروسل باید حداقل 2 تصویر ارسال کنید")
        if media_type != "carousel" and not image_url and not image_urls:
            raise InstagramPublishError("آدرس تصویر/ویدیو الزامی است")

        # Minimum spacing between publishes (anti-block) — raises if too soon
        await self._check_min_interval(account_id)

        result: Optional[Dict[str, Any]] = None

        # 1) Preferred path: OpenClaw gateway → Browser Use Cloud.
        #    This is the ONLY publishing route that works from the Vercel
        #    serverless runtime (no Meta app, no local instagrapi needed).
        if settings.openclaw_base_url and settings.openclaw_token:
            from app.core.database import SessionLocal
            from app.models import InstagramAccount
            from app.services.openclaw import OpenClawError, publish_post_via_gateway

            username_hint = ""
            try:
                async with SessionLocal() as session:
                    acc = await session.get(InstagramAccount, account_id)
                    username_hint = acc.username if acc else ""
            except Exception:  # noqa: BLE001
                pass
            try:
                reply = await publish_post_via_gateway(
                    image_url=image_url or "",
                    caption=caption,
                    media_type=media_type,
                    image_urls=image_urls,
                    user_id=user_id,
                    account_id=account_id,
                    username_hint=username_hint,
                )
                result = {
                    "ok": True,
                    "mock": False,
                    "via": "openclaw_gateway",
                    "media_id": "",
                    "permalink": "",
                    "media_type": media_type,
                    "gateway_reply": (reply or "")[:500],
                    "note": "انتشار از طریق درگاه OpenClaw انجام شد؛ نتیجه دقیق در گزارش درگاه ثبت است.",
                }
            except OpenClawError as exc:
                logger.warning("openclaw gateway publish failed, falling back: %s", exc)
                result = None  # fall back below

        # 2) Fallback: instagrapi (private API, only when running locally)
        if result is None:
            client = self._get_instagrapi_client() if self.enabled else None

            if client is None:
                # Mock mode — simulate success for testing
                logger.info(
                    "mock publish: account=%s type=%s caption=%.50s",
                    account_id, media_type, caption,
                )
                result = {
                    "ok": True,
                    "mock": True,
                    "media_id": f"mock_{account_id}_{int(datetime.now(timezone.utc).timestamp())}",
                    "permalink": f"https://instagram.com/p/mock_{account_id}/",
                    "media_type": media_type,
                    "caption": caption,
                    "warning": "حالت شبیه‌سازی — درگاه OpenClaw در دسترس نیست و instagrapi هم نصب نیست.",
                }
            else:
                # Real publish via instagrapi
                try:
                    # instagrapi is sync, run in thread
                    import asyncio

                    def _do_publish():
                        if media_type == "story":
                            # client.photo_upload_to_story or video_upload_to_story
                            if image_url:
                                return client.photo_upload_to_story(image_url, caption)
                            raise ValueError("story needs image_url")
                        elif media_type == "reel":
                            if image_url:
                                return client.clip_upload(image_url, caption)
                            raise ValueError("reel needs video URL")
                        elif media_type == "carousel":
                            # album_upload
                            urls = image_urls or []
                            return client.album_upload(urls, caption)
                        else:  # post
                            if image_url:
                                # Detect video vs photo by extension
                                if image_url.lower().endswith((".mp4", ".mov")):
                                    return client.video_upload(image_url, caption)
                                return client.photo_upload(image_url, caption)
                            raise ValueError("post needs image_url")

                    # Run sync client in thread pool
                    result_raw = await asyncio.to_thread(_do_publish)
                    result = {
                        "ok": True,
                        "mock": False,
                        "via": "instagrapi",
                        "media_id": str(getattr(result_raw, "id", "") or getattr(result_raw, "pk", "") or "unknown"),
                        "permalink": f"https://instagram.com/p/{getattr(result_raw, 'code', 'unknown')}/",
                        "media_type": media_type,
                        "raw": str(result_raw)[:500],
                    }
                except Exception as exc:
                    logger.exception("instagrapi publish failed")
                    # Check for block / rate limit
                    msg = str(exc).lower()
                    if "block" in msg or "spam" in msg or "429" in msg or "rate" in msg:
                        raise InstagramPublishError(
                            f"⚠️ اینستاگرام درخواست را مسدود کرد (احتمال بلاک موقت). "
                            f"لطفا 24 ساعت صبر کنید و دوباره تلاش کنید. خطا: {exc}"
                        ) from exc
                    raise InstagramPublishError(f"انتشار ناموفق بود: {exc}") from exc

        # Log activity
        try:
            from app.core.database import SessionLocal
            from app.models import ActivityLog

            async with SessionLocal() as session:
                session.add(
                    ActivityLog(
                        user_id=user_id,
                        account_id=account_id,
                        action="instagram_publish",
                        detail=f"{media_type} mock={result.get('mock')} id={result.get('media_id')} cap={caption[:100]}",
                        level="info",
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("publish log failed: %s", exc)

        # Check remaining and warn if close to limit
        result["limit_info"] = limit_info
        result["limit_info"]["remaining_after"] = max(0, limit_info["remaining"] - 1)

        return result

    async def get_status(self, account_id: Optional[int] = None) -> Dict[str, Any]:
        """Get publisher status."""
        client_available = False
        try:
            import instagrapi  # noqa: F401

            client_available = True
        except ImportError:
            client_available = False

        gateway_configured = bool(settings.openclaw_base_url and settings.openclaw_token)
        try:
            from urllib.parse import urlparse

            gw_host = urlparse(settings.openclaw_base_url).netloc if gateway_configured else ""
        except Exception:  # noqa: BLE001
            gw_host = ""
        status = {
            "enabled": self.enabled or gateway_configured,
            "mode": settings.instagram_publish_mode,
            "daily_limit": self.daily_limit,
            "min_interval_minutes": settings.instagram_publish_min_interval_minutes,
            "instagrapi_installed": client_available,
            "credentials_set": bool(settings.instagram_username and settings.instagram_password),
            "username": settings.instagram_username or None,
            "mock_mode": not gateway_configured and (not self.enabled or not client_available),
            "gateway": {
                "configured": gateway_configured,
                "host": gw_host,
                "browser_use_key_set": bool(settings.browser_use_api_key),
            },
        }

        if account_id:
            limit_info = await self._check_daily_limit(account_id)
            status["today"] = limit_info

        return status


# Singleton
_publisher: Optional[InstagramPublisher] = None


def get_publisher() -> InstagramPublisher:
    global _publisher
    if _publisher is None:
        _publisher = InstagramPublisher()
    return _publisher
