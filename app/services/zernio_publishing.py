"""The sole Instagram post-publication adapter, using Zernio's official SDK.

ChatbotX and Meta remain messaging/insights integrations, not publishers.
Both manual and scheduled publication must verify an active Zernio account
against a connected InstagramAccount owned by the caller before this adapter
is invoked. POST retries are disabled to avoid duplicate public posts.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from zernio import (
    Zernio,
    ZernioAuthenticationError,
    ZernioError,
    ZernioRateLimitError,
    ZernioTimeoutError,
)

from app.core.config import get_settings
from app.utils import jalali

logger = logging.getLogger("gramma.zernio")


class ZernioPublishError(Exception):
    """A safe, user-facing error; never contains credentials or raw API bodies."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def normalize_username(username: str | None) -> str:
    """Exact, case-insensitive handle match (never fuzzy-match another page)."""
    return (username or "").strip().lstrip("@").casefold()


def _media_item(url: str) -> dict[str, str]:
    if not isinstance(url, str) or not 0 < len(url) <= 2048:
        raise ZernioPublishError("آدرس رسانه معتبر نیست.")
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        if (
            parts.scheme != "https" or not host or parts.username or parts.password
            or host == "localhost" or host.endswith((".local", ".internal"))
        ):
            raise ValueError("non-public URL")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError("private address")
        except ValueError as exc:
            if str(exc) == "private address":
                raise
            # A hostname rather than a literal IP is expected for public media.
        if any(ch.isspace() for ch in url):
            raise ValueError("whitespace")
    except ValueError as exc:
        raise ZernioPublishError("رسانه باید آدرس عمومی HTTPS باشد.") from exc
    kind = "video" if parts.path.lower().endswith((".mp4", ".mov")) else "image"
    return {"type": kind, "url": url.strip()}


def _media_items(urls: list[str], kind: str) -> list[dict[str, str]]:
    if not isinstance(urls, list) or not 1 <= len(urls) <= 10:
        raise ZernioPublishError("بین 1 تا 10 آدرس رسانه وارد کنید.")
    items = [_media_item(url) for url in urls]
    if kind == "carousel" and len(items) < 2:
        raise ZernioPublishError("برای کاروسل حداقل 2 رسانه لازم است.")
    if kind != "carousel" and len(items) != 1:
        raise ZernioPublishError("برای این نوع محتوا دقیقاً 1 رسانه لازم است.")
    if kind == "post" and items[0]["type"] != "image":
        raise ZernioPublishError("برای ویدیو گزینه ریلز را انتخاب کنید.")
    if kind == "reel" and items[0]["type"] != "video":
        raise ZernioPublishError("ریلز به فایل ویدیویی MP4 یا MOV نیاز دارد.")
    return items


def _schedule_time(value: str) -> datetime:
    # The UI only accepts a Jalali date with Latin digits; the provider gets ISO.
    text = (value or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2} [0-2]\d:[0-5]\d", text):
        raise ZernioPublishError("زمان‌بندی را به صورت شمسی 1405/08/01 10:00 وارد کنید.")
    try:
        parsed = jalali.parse_jalali_input(text)
        if not parsed:
            raise ValueError("invalid Jalali date")
        year, month, day = map(int, text.split(" ")[0].split("/"))
        hour, minute = map(int, text.split(" ")[1].split(":"))
        canonical = f"{year:04d}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}"
        if jalali.to_jalali_str(parsed) != canonical:
            raise ValueError("out of range Jalali date")
    except (ValueError, OverflowError) as exc:
        raise ZernioPublishError("تاریخ یا ساعت شمسی نامعتبر است.") from exc
    if parsed.astimezone(timezone.utc) < datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ZernioPublishError("زمان انتشار باید حداقل 5 دقیقه در آینده باشد.")
    return parsed


def _provider_error(exc: ZernioError, operation: str) -> ZernioPublishError:
    """Never propagate upstream bodies: APIs can reflect confidential headers."""
    status = getattr(exc, "status_code", None)
    logger.warning("Zernio %s failed: %s (HTTP %s)", operation, type(exc).__name__, status)
    if isinstance(exc, ZernioTimeoutError):
        return ZernioPublishError(
            "پاسخ Zernio به‌موقع نرسید؛ قبل از تلاش دوباره، داشبورد Zernio را برای جلوگیری از ثبت تکراری بررسی کنید.",
            504,
        )
    if isinstance(exc, ZernioAuthenticationError) or status == 401:
        return ZernioPublishError("کلید Zernio نامعتبر یا منقضی شده است.", 502)
    if isinstance(exc, ZernioRateLimitError) or status == 429:
        return ZernioPublishError("سقف درخواست Zernio پر شده است؛ کمی بعد دوباره تلاش کنید.", 503)
    return ZernioPublishError("درخواست Zernio انجام نشد؛ وضعیت اتصال یا داده‌ها را بررسی کنید.", 502)


class ZernioPublisher:
    """A narrowly scoped adapter; SDK POSTs are never retried automatically."""

    def __init__(
        self,
        api_key: str | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._key = api_key if api_key is not None else get_settings().zernio_api_key
        # SDK max_retries is the TOTAL number of attempts: 1 disables POST retry.
        self._client_factory = client_factory or (
            lambda: Zernio(api_key=self._key, timeout=18.0, max_retries=1)
        )

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def list_instagram_accounts(self) -> list[dict[str, Any]]:
        if not self.configured:
            raise ZernioPublishError("کلید Zernio تنظیم نشده است.", 503)
        try:
            async with self._client_factory() as client:
                response = await client.accounts.alist()
        except ZernioError as exc:
            raise _provider_error(exc, "list_accounts") from exc
        except Exception as exc:
            logger.warning("Zernio list_accounts failed: %s", type(exc).__name__)
            raise ZernioPublishError("اتصال به Zernio ممکن نشد.", 502) from exc

        # Whitelist fields; do not return any provider tokens or raw metadata.
        accounts = []
        for account in response.accounts:
            platform = getattr(account.platform, "value", account.platform)
            if platform == "instagram":
                accounts.append({
                    "id": account.field_id,
                    "platform": "instagram",
                    "username": account.username or "",
                    "active": bool(account.isActive),
                })
        return accounts

    async def create_post(
        self,
        *,
        zernio_account_id: str,
        expected_username: str,
        caption: str,
        media_urls: list[str],
        media_type: str = "post",
        mode: str = "draft",
        scheduled_jalali: str = "",
    ) -> dict[str, Any]:
        """Create only a draft or a user-requested scheduled post; never publish now."""
        if mode not in ("draft", "schedule"):
            raise ZernioPublishError("نوع عملیات نامعتبر است (فقط پیش‌نویس یا زمان‌بندی).")
        if media_type not in ("post", "story", "reel", "carousel"):
            raise ZernioPublishError("نوع محتوا نامعتبر است.")
        if not isinstance(caption, str) or len(caption) > 2200 or (not caption.strip() and media_type != "story"):
            raise ZernioPublishError("برای پست/ریلز/کاروسل کپشن تا 2200 نویسه لازم است.")
        items = _media_items(media_urls, media_type)
        scheduled = _schedule_time(scheduled_jalali) if mode == "schedule" else None
        if not zernio_account_id or not normalize_username(expected_username):
            raise ZernioPublishError("اکانت Zernio نامعتبر است.", 403)

        # Never trust an account ID supplied by the browser. Fetch a fresh list
        # from Zernio and require an exact handle match with the owned DB page.
        provider_accounts = await self.list_instagram_accounts()
        matches = [a for a in provider_accounts if a["id"] == zernio_account_id]
        if len(matches) != 1 or normalize_username(matches[0]["username"]) != normalize_username(expected_username):
            raise ZernioPublishError("این پیج در Zernio به حساب شما متصل نیست.", 403)
        if not matches[0]["active"]:
            raise ZernioPublishError("اتصال پیج در Zernio غیرفعال است؛ آن را دوباره متصل کنید.", 409)

        platform = {"platform": "instagram", "accountId": zernio_account_id}
        if media_type == "story":
            platform["platformSpecificData"] = {"contentType": "story"}
        try:
            async with self._client_factory() as client:
                result = await client.posts.acreate(
                    content=caption.strip() if media_type != "story" else (caption.strip() or "استوری"),
                    platforms=[platform],
                    media_items=items,
                    is_draft=(mode == "draft"),
                    scheduled_for=(scheduled.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if scheduled else None),
                    timezone="Asia/Tehran",
                    publish_now=False,
                )
        except ZernioError as exc:
            raise _provider_error(exc, "create_post") from exc
        except Exception as exc:
            logger.warning("Zernio create_post failed: %s", type(exc).__name__)
            raise ZernioPublishError(
                "نتیجه ثبت در Zernio نامشخص است؛ پیش از تلاش دوباره داشبورد را بررسی کنید.", 502
            ) from exc
        post = result.post
        if post is None or not post.field_id:
            raise ZernioPublishError("پاسخ Zernio ناقص است؛ قبل از تکرار، داشبورد را بررسی کنید.", 502)
        status = getattr(post.status, "value", post.status) if post.status else "unknown"
        return {
            "ok": True,
            "post_id": post.field_id,
            "status": status,
            "mode": mode,
            "scheduled_jalali": jalali.to_jalali_str(scheduled) if scheduled else None,
        }

    async def get_post_status(
        self, post_id: str, *, expected_account_id: str
    ) -> dict[str, Any]:
        """GET only: reconcile one accepted post, never submit or retry it."""
        if not self.configured:
            raise ZernioPublishError("کلید Zernio تنظیم نشده است.", 503)
        if not isinstance(post_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", post_id):
            raise ZernioPublishError("شناسه پست Zernio نامعتبر است.")
        if not expected_account_id:
            raise ZernioPublishError("شناسه پیج Zernio نامعتبر است.")
        try:
            async with self._client_factory() as client:
                response = await client.posts.aget_post(post_id)
        except ZernioError as exc:
            raise _provider_error(exc, "get_post") from exc
        except Exception as exc:
            logger.warning("Zernio get_post_status failed: %s", type(exc).__name__)
            raise ZernioPublishError("بروزرسانی وضعیت از Zernio ممکن نشد.", 502) from exc

        post = response.get("post") if isinstance(response, dict) else None
        if not isinstance(post, dict) or str(post.get("_id", "")) != post_id:
            raise ZernioPublishError("پاسخ وضعیت Zernio معتبر نیست.", 502)
        targets = post.get("platforms") or []
        instagram = [p for p in targets if isinstance(p, dict) and p.get("platform") == "instagram"]
        if len(instagram) != 1:
            raise ZernioPublishError("پیج مقصد پست در Zernio مشخص نیست.", 502)
        target = instagram[0]
        account = target.get("accountId")
        target_id = account.get("_id") if isinstance(account, dict) else account
        if str(target_id) != expected_account_id:
            raise ZernioPublishError("پیج مقصد پست در Zernio تغییر کرده است.", 403)
        status = str(post.get("status") or "unknown").lower()
        target_status = str(target.get("status") or "unknown").lower()
        # Both the post AND Instagram's target must confirm success. For one
        # target, a terminal platform failure takes precedence over a rollup.
        if target_status in ("failed", "partial") or status in ("failed", "partial"):
            safe_status = "failed" if status != "partial" else "partial"
        elif status == "published" and target_status == "published":
            safe_status = "published"
        else:
            safe_status = "publishing"
        timestamp = target.get("publishedAt") or post.get("publishedAt")
        published_at = None
        if timestamp and safe_status == "published":
            try:
                stamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    published_at = stamp.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        return {"status": safe_status, "published_at": published_at}

    async def publish_post(
        self,
        *,
        zernio_account_id: str,
        expected_username: str,
        caption: str,
        media_urls: list[str],
        idempotency_key: str,
        media_type: str = "post",
    ) -> dict[str, Any]:
        """Submit one real post for immediate publication, never a draft/mock.

        The caller MUST durably claim ``idempotency_key`` before invoking this
        method. A timeout has an ambiguous result: neither the SDK nor this
        adapter automatically retries a publish request.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ZernioPublishError("کلید جلوگیری از انتشار تکراری لازم است.")
        if media_type not in ("post", "story", "reel", "carousel"):
            raise ZernioPublishError("نوع محتوا نامعتبر است.")
        if not isinstance(caption, str) or len(caption) > 2200 or (not caption.strip() and media_type != "story"):
            raise ZernioPublishError("برای پست/ریلز/کاروسل کپشن تا 2200 نویسه لازم است.")
        items = _media_items(media_urls, media_type)
        if not zernio_account_id or not normalize_username(expected_username):
            raise ZernioPublishError("اکانت Zernio نامعتبر است.", 403)

        # Refresh the provider list: never publish to an ID from a stale cache.
        provider_accounts = await self.list_instagram_accounts()
        matches = [a for a in provider_accounts if a["id"] == zernio_account_id]
        if len(matches) != 1 or normalize_username(matches[0]["username"]) != normalize_username(expected_username):
            raise ZernioPublishError("این پیج در Zernio به حساب شما متصل نیست.", 403)
        if not matches[0]["active"]:
            raise ZernioPublishError("اتصال پیج در Zernio غیرفعال است؛ آن را دوباره متصل کنید.", 409)

        platform = {"platform": "instagram", "accountId": zernio_account_id}
        if media_type == "story":
            platform["platformSpecificData"] = {"contentType": "story"}
        try:
            async with self._client_factory() as client:
                response = await client.posts.acreate(
                    content=caption.strip() if media_type != "story" else (caption.strip() or "استوری"),
                    platforms=[platform],
                    media_items=items,
                    is_draft=False,
                    scheduled_for=None,
                    timezone="Asia/Tehran",
                    metadata={"idempotencyKey": idempotency_key},
                    publish_now=True,
                )
        except ZernioError as exc:
            raise _provider_error(exc, "publish_post") from exc
        except Exception as exc:
            logger.warning("Zernio publish_post failed: %s", type(exc).__name__)
            raise ZernioPublishError(
                "نتیجه انتشار در Zernio نامشخص است؛ پیش از تلاش دوباره داشبورد را بررسی کنید.", 502
            ) from exc
        post = response.post
        if post is None or not post.field_id:
            raise ZernioPublishError("پاسخ Zernio ناقص است؛ قبل از تکرار، داشبورد را بررسی کنید.", 502)
        status = getattr(post.status, "value", post.status) if post.status else "unknown"
        if status not in ("publishing", "published"):
            raise ZernioPublishError(
                "Zernio انتشار فوری را تأیید نکرد؛ قبل از هر تلاش دیگر وضعیت پست را در داشبورد بررسی کنید.", 502
            )
        return {
            "ok": True,
            "provider": "zernio",
            "post_id": post.field_id,
            "status": status,
            "published": status == "published",
            "idempotency_key": idempotency_key,
        }


def get_zernio_publisher() -> ZernioPublisher:
    return ZernioPublisher()
