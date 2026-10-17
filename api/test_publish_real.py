"""A one-shot, POST-only, explicitly authorized real Zernio publication.

The route is mounted on the existing FastAPI app via ``app.webapp.main`` and
served by the existing Vercel ``api/index.py`` function. It is inert until a
human supplies the private TEST_SECRET header; it never runs at deployment.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import InstagramAccount
from app.services.zernio_publishing import (
    ZernioPublishError,
    get_zernio_publisher,
    normalize_username,
)

router = APIRouter()
logger = logging.getLogger("gramma.manual_publish")

_IMAGE_URL = "https://picsum.photos/1080/1080"
_CAPTION = "تست انتشار واقعی از طریق گراما 🚀"
_IDEMPOTENCY_KEY = "test_key_002"
_EXPECTED_USERNAME = "zeynalikids"


@router.post("/api/test-publish-real", include_in_schema=False)
async def post_real_publication(request: Request):
    """Publish exactly once; return accepted provider status, never mock data."""
    settings = get_settings()
    if not settings.test_secret:
        return JSONResponse(status_code=503, content={"ok": False, "error": "TEST_SECRET تنظیم نشده است."})
    provided = request.headers.get("x-test-secret", "")
    if not provided or not hmac.compare_digest(provided.encode(), settings.test_secret.encode()):
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})
    if not settings.admin_ids:
        return JSONResponse(status_code=503, content={"ok": False, "error": "مدیر ربات تنظیم نشده است."})

    publisher = get_zernio_publisher()
    if not publisher.configured:
        return JSONResponse(status_code=503, content={"ok": False, "error": "Zernio تنظیم نشده است."})

    # Only the owner's connected local page may be used; never trust request
    # body, account IDs or usernames supplied by a caller holding the secret.
    try:
        async with SessionLocal() as session:
            accounts = (await session.execute(select(InstagramAccount).where(
                InstagramAccount.owner_id.in_(settings.admin_ids),
                InstagramAccount.status == "connected",
            ))).scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Local owner lookup failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"ok": False, "error": "بررسی مالکیت پیج موقتاً ممکن نیست."})
    owned = [account for account in accounts if normalize_username(account.username) == _EXPECTED_USERNAME]
    if len(owned) != 1:
        return JSONResponse(status_code=409, content={"ok": False, "error": "پیج مجاز و متصلِ مدیر یافت نشد."})

    try:
        providers = await publisher.list_instagram_accounts()
    except ZernioPublishError as exc:
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": str(exc)})
    matching = [a for a in providers if normalize_username(a["username"]) == _EXPECTED_USERNAME and a["active"]]
    if len(matching) != 1:
        return JSONResponse(status_code=409, content={"ok": False, "error": "پیج مجاز در Zernio فعال نیست."})

    # Claim the one-shot key in the primary DB BEFORE the SDK can send a POST.
    # PostgreSQL's unique constraint makes concurrent callers race-safe. If
    # the provider times out, keep the claim: the publication may have worked.
    try:
        async with SessionLocal() as session:
            claimed = (await session.execute(text("""
                INSERT INTO public.idempotency_keys (key, meta)
                VALUES (:key, '{"route":"test-publish-real","status":"attempting"}'::jsonb)
                ON CONFLICT (key) DO NOTHING RETURNING key
            """), {"key": _IDEMPOTENCY_KEY})).scalar_one_or_none()
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("One-shot claim result unknown: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "ok": False,
            "error": "وضعیت ثبت کلید نامشخص است؛ پیش از تلاش دوباره وضعیت Zernio و دیتابیس را بررسی کنید.",
            "retry_safe": False,
        })
    if not claimed:
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": "این کلید قبلاً مصرف شده است؛ پیش از هر اقدام دیگر وضعیت Zernio را بررسی کنید.",
            "idempotency_key": _IDEMPOTENCY_KEY,
        })

    try:
        result = await publisher.publish_post(
            zernio_account_id=matching[0]["id"],
            expected_username=owned[0].username,
            caption=_CAPTION,
            media_urls=[_IMAGE_URL],
            media_type="post",
            idempotency_key=_IDEMPOTENCY_KEY,
        )
    except ZernioPublishError as exc:
        await _record_result("unknown", None)
        return JSONResponse(status_code=exc.status_code, content={
            "ok": False, "error": str(exc), "idempotency_key": _IDEMPOTENCY_KEY,
            "retry_safe": False,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unexpected Zernio publish result: %s", type(exc).__name__)
        await _record_result("unknown", None)
        return JSONResponse(status_code=502, content={
            "ok": False,
            "error": "نتیجه انتشار نامشخص است؛ پیش از هر تلاش دیگر داشبورد Zernio را بررسی کنید.",
            "idempotency_key": _IDEMPOTENCY_KEY,
            "retry_safe": False,
        })
    await _record_result("accepted", result["post_id"])
    return result


async def _record_result(status: str, post_id: str | None) -> None:
    """Best-effort audit only; do not turn a successful publish into a retry."""
    try:
        async with SessionLocal() as session:
            await session.execute(text("""
                UPDATE public.idempotency_keys
                SET meta = jsonb_build_object(
                    'route', 'test-publish-real', 'status', :status,
                    'post_id', :post_id
                )
                WHERE key = :key
            """), {"status": status, "post_id": post_id, "key": _IDEMPOTENCY_KEY})
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Zernio audit update failed: %s", type(exc).__name__)
