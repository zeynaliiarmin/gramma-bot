"""Strict Instagram-only Zernio post options; no unsupported API knobs.

The root-level Zernio `mentions` field is stored-only. Real tags belong in
platforms[].platformSpecificData.userTags; image coordinates are mandatory.
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.services.limits import (
    TELEGRAM_POST_COLLABORATOR_LIMIT,
    TELEGRAM_POST_USER_TAG_LIMIT,
    TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT,
)


class InstagramOptionError(ValueError):
    """Safe validation error; never includes provider response bodies."""


def instagram_username(raw: str) -> str:
    if not isinstance(raw, str):
        raise InstagramOptionError("نام کاربری اینستاگرام معتبر نیست.")
    name = raw.strip().removeprefix("@").lower()
    if (not re.fullmatch(r"[a-z0-9._]{1,30}", name)
            or name.startswith(".") or name.endswith(".") or ".." in name):
        raise InstagramOptionError("نام کاربری را بدون فاصله و حداکثر 30 نویسه وارد کنید.")
    return name


def validate_instagram_options(
    options: dict[str, Any] | None, *, media_type: str, media_kinds: list[str],
) -> dict[str, Any]:
    """Return only documented fields, canonicalized and checked per slide."""
    if options is None:
        return {}
    if not isinstance(options, dict) or set(options) - {"locationId", "userTags", "collaborators", "firstComment", "shareToFeed"}:
        raise InstagramOptionError("تنظیمات انتشار ناشناخته یا نامعتبر است.")
    if not options:
        return {}
    if media_type not in ("post", "reel", "carousel"):
        raise InstagramOptionError("این نوع پست تنظیمات پیشرفتهٔ تأییدشده ندارد.")
    if (not isinstance(media_kinds, list) or not media_kinds
            or any(kind not in ("image", "video") for kind in media_kinds)
            or (media_type == "carousel" and not 2 <= len(media_kinds) <= 10)
            or (media_type != "carousel" and len(media_kinds) != 1)):
        raise InstagramOptionError("ترکیب رسانه برای تنظیمات این پست معتبر نیست.")
    result: dict[str, Any] = {}

    if "shareToFeed" in options:
        # This is injected by the queue/adapter for every Reel, not an
        # editable user setting. False and non-Reel placements are rejected.
        if media_type != "reel" or options["shareToFeed"] is not True:
            raise InstagramOptionError("نمایش ریلز در فید باید فعال باشد.")
        result["shareToFeed"] = True

    if "locationId" in options:
        value = options["locationId"]
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise InstagramOptionError("لوکیشن باید شناسهٔ عددی Facebook Page دارای موقعیت باشد، نه نام مکان.")
        result["locationId"] = value

    if "collaborators" in options:
        names = options["collaborators"]
        if not isinstance(names, list) or not 1 <= len(names) <= TELEGRAM_POST_COLLABORATOR_LIMIT:
            raise InstagramOptionError("حداکثر 3 همکارِ دارای حساب عمومی حرفه‌ای مجاز است.")
        clean = [instagram_username(name) for name in names]
        if len(set(clean)) != len(clean):
            raise InstagramOptionError("نام همکار نباید تکراری باشد.")
        result["collaborators"] = clean

    if "userTags" in options:
        tags = options["userTags"]
        if not isinstance(tags, list) or not 1 <= len(tags) <= TELEGRAM_POST_USER_TAG_LIMIT:
            raise InstagramOptionError("برای این پست حداکثر 5 تگ کاربر مجاز است.")
        clean_tags = []
        seen = set()
        for tag in tags:
            if not isinstance(tag, dict) or set(tag) - {"username", "x", "y", "mediaIndex"}:
                raise InstagramOptionError("ساختار تگ کاربر نامعتبر است.")
            name = instagram_username(tag.get("username"))
            if media_type == "carousel":
                index = tag.get("mediaIndex")
                if type(index) is not int or not 0 <= index < len(media_kinds):
                    raise InstagramOptionError("شمارهٔ اسلاید تگ نامعتبر است.")
                entry: dict[str, Any] = {"username": name, "mediaIndex": index}
            else:
                if "mediaIndex" in tag:
                    raise InstagramOptionError("برای رسانهٔ تکی شمارهٔ اسلاید لازم نیست.")
                index = 0
                entry = {"username": name}
            if (name, index) in seen:
                raise InstagramOptionError("این کاربر قبلاً روی همین اسلاید تگ شده است.")
            seen.add((name, index))
            if media_kinds[index] == "image":
                x, y = tag.get("x"), tag.get("y")
                if (type(x) not in (int, float) or type(y) not in (int, float)
                        or not math.isfinite(x) or not math.isfinite(y)
                        or not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0):
                    raise InstagramOptionError("برای تگ عکس، مختصات x/y بین 0 و 1 لازم است.")
                entry.update({"x": float(x), "y": float(y)})
            elif "x" in tag or "y" in tag:
                raise InstagramOptionError("تگ ویدیو مختصات تصویری ندارد.")
            clean_tags.append(entry)
        result["userTags"] = clean_tags

    if "firstComment" in options:
        comment = options["firstComment"]
        if media_type not in ("post", "carousel"):
            raise InstagramOptionError("اولین کامنت طبق مستندات برای این نوع محتوا مجاز نیست.")
        if not isinstance(comment, str) or not 0 < len(comment.strip()) <= 2200:
            raise InstagramOptionError("اولین کامنت باید بین 1 تا 2200 نویسه باشد.")
        result["firstComment"] = comment.strip()

    return result


def validate_instagram_caption(
    caption: str, *, media_type: str, options: dict[str, Any],
    caption_moved_to_comment: bool = False,
) -> str:
    """Allow a blank feed caption only after a deliberate, guarded move.

    Shared by enqueue, Worker preflight and the provider adapter. A mere
    firstComment supplied manually is NOT permission for an empty post.
    """
    if not isinstance(caption, str) or len(caption) > 2200:
        raise InstagramOptionError("کپشن باید حداکثر 2200 نویسه باشد.")
    if type(caption_moved_to_comment) is not bool:
        raise InstagramOptionError("وضعیت انتقال کپشن معتبر نیست.")
    if caption_moved_to_comment:
        if (media_type not in ("post", "carousel")
                or not isinstance(options.get("firstComment"), str)
                or not 0 < len(options["firstComment"].strip()) <= 2200
                or len(caption) > TELEGRAM_SHORT_ON_POST_CAPTION_LIMIT
                or (caption and not caption.strip())):
            raise InstagramOptionError("کپشن منتقل‌شده به اولین کامنت یا متن کوتاهِ روی پست معتبر نیست.")
    elif media_type != "story" and not caption.strip():
        raise InstagramOptionError("برای این پست کپشن غیرخالی لازم است.")
    return caption.strip()
