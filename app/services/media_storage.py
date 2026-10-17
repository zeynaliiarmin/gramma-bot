"""Gramma — media storage (downloads Telegram files, optional S3 CDN).

Instagram Graph API requires a *publicly reachable* media URL. Locally we
save files to disk; in production set the S3_* variables to upload them to
an S3-compatible bucket and use S3_PUBLIC_BASE as the public prefix.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.types import Message

from app.core.config import get_settings
from app.services.limits import TELEGRAM_BOT_DOWNLOAD_MAX_BYTES

settings = get_settings()

# Serverless-safe upload dir: Vercel's FS is read-only except /tmp.
if os.environ.get("VERCEL") or settings.run_mode == "webhook":
    UPLOAD_DIR = Path("/tmp/media_uploads")
else:
    UPLOAD_DIR = Path("media_uploads")
try:
    UPLOAD_DIR.mkdir(exist_ok=True)
except OSError:  # noqa: BLE001  (read-only FS etc.)
    UPLOAD_DIR = Path(tempfile.gettempdir()) / "media_uploads"
    UPLOAD_DIR.mkdir(exist_ok=True)


async def save_downloaded_media(bot: Bot, message: Message, is_video: bool = False) -> str:
    """Download a media message to local storage (and S3 if configured)."""
    if message.photo:
        file_obj = message.photo[-1]
        ext = "jpg"
    elif message.video:
        file_obj = message.video
        ext = "mp4"
    elif message.video_note:
        file_obj = message.video_note
        ext = "mp4"
    elif message.document:
        file_obj = message.document
        ext = os.path.splitext(message.document.file_name or "")[-1].lstrip(".") or "bin"
    else:
        raise ValueError("unsupported media")

    tg_file = await bot.get_file(file_obj.file_id)
    local_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.{ext}"
    await bot.download_file(tg_file.file_path, destination=local_path)

    if settings.s3_bucket:
        return await _upload_to_s3(local_path, ext)
    return str(local_path)


async def _upload_to_s3(local_path: Path, ext: str) -> str:
    """Upload to S3-compatible storage, return the public URL."""
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint or None,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    key = f"gramma/{uuid.uuid4().hex}.{ext}"
    client.upload_file(str(local_path), settings.s3_bucket, key)
    base = settings.s3_public_base or f"https://{settings.s3_bucket}.s3.amazonaws.com"
    return f"{base.rstrip('/')}/{key}"


async def upload_to_supabase_storage(local_path: Path, ext: str) -> str:
    """Upload a local file to the Supabase Storage bucket (serverless media).

    Requires ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``. Returns the
    public HTTPS URL. Prefer this on Vercel (no S3 credentials, no boto3).
    """
    import httpx

    key = f"gramma/{uuid.uuid4().hex}.{ext}"
    bucket = settings.supabase_storage_bucket
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{key}"
    content_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "mp4": "video/mp4", "mov": "video/quicktime"}.get(ext.lower(), "application/octet-stream")
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
        "Content-Type": content_type,
        "x-upsert": "false",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        with open(local_path, "rb") as fh:
            resp = await client.post(url, headers=headers, content=fh.read())
        if resp.status_code not in (200, 201):
            # Never log a remote response body (could contain credentials).
            raise RuntimeError(f"storage upload failed ({resp.status_code})")
    return f"{settings.supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/{key}"


def owned_supabase_storage_path(url: str) -> str | None:
    """Recognize ONLY this project's bot-created, unique Supabase objects.

    An external URL, a copied path, a query string or a different bucket can
    never authorize a Storage DELETE. The UUID key is generated above, server
    side; neither a user-supplied URL nor another application's file matches.
    """
    if not isinstance(url, str) or not settings.supabase_url:
        return None
    bucket = settings.supabase_storage_bucket
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", bucket or ""):
        return None
    base = urlsplit(settings.supabase_url.rstrip("/"))
    candidate = urlsplit(url)
    prefix = base.path.rstrip("/") + f"/storage/v1/object/public/{bucket}/"
    if (base.scheme != "https" or candidate.scheme != "https"
            or base.netloc != candidate.netloc or candidate.query or candidate.fragment
            or not candidate.path.startswith(prefix)):
        return None
    path = candidate.path[len(prefix):]
    if not re.fullmatch(r"gramma/[0-9a-f]{32}\.(?:jpg|jpeg|png|mp4|mov)", path):
        return None
    return path


async def delete_owned_supabase_media(path: str) -> bool:
    """Remove one confirmed-published bot object via Storage API, not SQL.

    HTTP 404 is idempotent success (already removed); other errors remain
    pending for a later read-only-status-confirmed cleanup pass.
    """
    import httpx

    if (not isinstance(path, str) or not re.fullmatch(
            r"gramma/[0-9a-f]{32}\.(?:jpg|jpeg|png|mp4|mov)", path)
            or not settings.supabase_url or not settings.supabase_service_role_key):
        return False
    bucket = settings.supabase_storage_bucket
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", bucket or ""):
        return False
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{path}"
    headers = {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.delete(url, headers=headers)
    return response.status_code in (200, 204, 404)


async def save_telegram_photo_to_supabase(bot: Bot, message: Message) -> str:
    """Upload one Telegram photo directly to our existing public bucket.

    This intentionally ignores optional S3 settings: the admin's spontaneous
    photo always becomes a durable Supabase HTTPS URL, even in polling mode.
    No local file path is ever queued. The /tmp download is always removed.
    """
    if not message.photo or not settings.supabase_url or not settings.supabase_service_role_key:
        raise ValueError("photo or Supabase Storage is not configured")
    local_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.jpg"
    try:
        telegram_file = await bot.get_file(message.photo[-1].file_id)
        await bot.download_file(telegram_file.file_path, destination=local_path)
        return await upload_to_supabase_storage(local_path, "jpg")
    finally:
        local_path.unlink(missing_ok=True)


class TelegramPostMediaError(ValueError):
    """Safe, user-facing validation error for a Telegram draft attachment."""


def describe_telegram_post_media(message: Message) -> dict:
    """Accept only Telegram photos and supported MP4/MOV videos, not documents."""
    if message.photo:
        media = message.photo[-1]
        kind, ext = "image", "jpg"
        duration = None
        mime = "image/jpeg"
    elif message.video:
        media = message.video
        kind = "video"
        mime = (media.mime_type or "").lower()
        if mime not in ("video/mp4", "video/quicktime"):
            raise TelegramPostMediaError("فقط ویدیوی MP4 یا MOV قابل افزودن به پست است.")
        ext = "mov" if mime == "video/quicktime" else "mp4"
        suffix = os.path.splitext((media.file_name or "").lower())[1].lstrip(".")
        if suffix in ("mp4", "mov") and suffix != ext:
            raise TelegramPostMediaError("پسوند و نوع ویدیو یکسان نیست؛ فایل را دوباره ارسال کنید.")
        duration = media.duration
        max_duration = 60 if message.media_group_id else 90
        if not isinstance(duration, int) or not 3 <= duration <= max_duration:
            raise TelegramPostMediaError(
                f"مدت ویدیو باید بین 3 و {max_duration} ثانیه باشد."
            )
    else:
        raise TelegramPostMediaError("برای این مسیر عکس یا ویدیوی تکی/آلبومی بفرستید.")
    if not isinstance(media.width, int) or not isinstance(media.height, int) or media.height < 1:
        raise TelegramPostMediaError("ابعاد رسانه مشخص نیست.")
    ratio = media.width / media.height
    if kind == "video" and not message.media_group_id:
        if not 0.52 <= ratio <= 0.61:
            raise TelegramPostMediaError("ویدیوی تکی به‌صورت ریلز منتشر می‌شود و باید عمودی 9:16 باشد.")
    elif not 0.55 <= ratio <= 1.93:
        raise TelegramPostMediaError("نسبت تصویر پست باید در محدودهٔ مستند Instagram باشد.")
    size = media.file_size
    if size is not None and size > TELEGRAM_BOT_DOWNLOAD_MAX_BYTES:
        raise TelegramPostMediaError("بات فایل بزرگ‌تر از 20 MB را دریافت نمی‌کند؛ فایل کوچک‌تری بفرستید (آپلود مستقیم هنوز فعال نیست).")
    return {
        "kind": kind, "ext": ext, "mime": mime, "duration": duration,
        "file_id": media.file_id, "file_size": size,
        "width": media.width, "height": media.height,
    }


async def save_telegram_post_media_to_supabase(bot: Bot, message: Message, media_info: dict) -> str:
    """Download a bounded Telegram file, upload it durably, erase /tmp."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise TelegramPostMediaError("فضای نگهداری رسانه تنظیم نشده است؛ چیزی در صف انتشار ثبت نشد.")
    tg_file = await bot.get_file(media_info["file_id"])
    size = tg_file.file_size or media_info.get("file_size")
    if size is None or size <= 0 or size > TELEGRAM_BOT_DOWNLOAD_MAX_BYTES:
        raise TelegramPostMediaError("اندازهٔ فایل نامشخص یا خارج از سقف دانلود 20 MB بات است.")
    if not tg_file.file_path:
        raise TelegramPostMediaError("دریافت فایل از تلگرام ممکن نیست.")
    local_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.{media_info['ext']}"
    try:
        await bot.download_file(tg_file.file_path, destination=local_path)
        if not 0 < local_path.stat().st_size <= TELEGRAM_BOT_DOWNLOAD_MAX_BYTES:
            raise TelegramPostMediaError("فایل دریافت‌شده خالی یا بزرگ‌تر از سقف دانلود بات است.")
        return await upload_to_supabase_storage(local_path, media_info["ext"])
    finally:
        local_path.unlink(missing_ok=True)


async def save_downloaded_media_serverless(bot: Bot, message: Message, is_video: bool = False) -> str:
    """Vercel-friendly media save: local /tmp download → Supabase Storage.

    Falls back to the plain local path when Supabase is not configured.
    """
    local = await save_downloaded_media(bot, message, is_video=is_video)
    if settings.supabase_url and settings.supabase_service_role_key:
        ext = local.rsplit(".", 1)[-1] if "." in local else "bin"
        public = await upload_to_supabase_storage(Path(local), ext)
        try:
            os.remove(local)
        except OSError:
            pass
        return public
    return local
