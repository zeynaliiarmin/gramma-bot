"""Gramma — media storage (downloads Telegram files, optional S3 CDN).

Instagram Graph API requires a *publicly reachable* media URL. Locally we
save files to disk; in production set the S3_* variables to upload them to
an S3-compatible bucket and use S3_PUBLIC_BASE as the public prefix.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message

from app.core.config import get_settings

settings = get_settings()

UPLOAD_DIR = Path("media_uploads")
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
