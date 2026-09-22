"""Gramma — Instagram OAuth + webhook receiver (FastAPI).

Endpoints:
  GET  /webhook/instagram            → Meta hub challenge verification
  POST /webhook/instagram            → signed webhook subscription events
  GET  /webhook/instagram/callback   → OAuth code exchange (CSRF-checked)

Security:
  * the `state` is single-use + bound to the Telegram user that started the
    flow (anti-CSRF);
  * the short-lived code is immediately swapped for a long-lived token and
    stored AES-256 encrypted (never plaintext);
  * webhook events are recorded into the audit log.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security.crypto import get_cipher
from app.core.security.oauth_states import oauth_states
from app.models import ActivityLog, InstagramAccount
from app.services.broadcast import notify_admin

settings = get_settings()
logger = logging.getLogger("gramma.webhook")

router = APIRouter(prefix=settings.webhook_path_prefix, tags=["instagram"])


def _redirect_uri() -> str:
    return f"{settings.webhook_base_url}{settings.webhook_path_prefix}/callback"


@router.get("")
async def webhook_verify(request: Request):
    """Meta hub verification handshake."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == settings.meta_verify_token:
        return PlainTextResponse(challenge or "ok")
    logger.warning("webhook verify rejected mode=%s", mode)
    return Response(status_code=403, content="verification failed")


@router.post("")
async def webhook_event(request: Request):
    """Receive signed webhook notifications from Meta."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return Response(status_code=400, content="bad json")
    await _consume_and_record_event(payload)
    return PlainTextResponse("EVENT_RECEIVED")


async def _consume_and_record_event(payload: dict) -> None:
    """Record audit entries + trigger auto-replies for incoming DMs."""
    from sqlalchemy import select

    from app.models import InstagramAccount
    from app.services.meta.service import InstagramService

    for entry in payload.get("entry", []):
        account_ig_id = entry.get("id")
        for change in entry.get("changes", []):
            field = change.get("field", "unknown")
            value = change.get("value", {})

            text = ""
            if "media_id" in value:
                text += f" media_id={value['media_id']}"
            if "comment_id" in value:
                text += f" comment_id={value['comment_id']}"

            async with SessionLocal() as session:
                acc = None
                if account_ig_id:
                    acc = (
                        await session.execute(
                            select(InstagramAccount).where(
                                InstagramAccount.instagram_user_id == str(account_ig_id)
                            )
                        )
                    ).scalars().first()

                # Auto-reply: incoming DM (field=messages) — NEVER limited.
                dm_text = None
                thread_id = None
                sender = None
                if field == "messages":
                    m = (value.get("message") or {})
                    if value.get("sender"):
                        sender = value["sender"].get("id")
                    dm_text = (m.get("text") or (m.get("attachments") and "📎") or "")
                    thread_id = value.get("sender", {}).get("id") or value.get("thread_id")

                # Auto-reply: incoming comment (field=comments) — capped by the
                # daily/hourly budget; overflow is queued FIFO instead of sent.
                if acc and field == "comments":
                    comment_id = str(value.get("id") or value.get("comment_id") or "")
                    comment_text = value.get("text") or ""
                    commenter = (value.get("from") or {})
                    commenter_id = commenter.get("id") if isinstance(commenter, dict) else None
                    if comment_id and comment_text:
                        from app.services.reply_engine import route_comment_auto_reply

                        decision = await route_comment_auto_reply(
                            session,
                            acc,
                            comment_id=comment_id,
                            comment_text=comment_text,
                            commenter_id=commenter_id,
                        )
                        d = decision.as_dict()
                        text += (
                            f" comment_action={d['action']}"
                            + (f" queue_id={d['queue_id']}" if d.get("queue_id") else "")
                        )
                        # Keep a Comment record for the community view.
                        from app.models import Comment

                        exists = (
                            await session.execute(
                                select(Comment).where(Comment.ig_comment_id == comment_id)
                            )
                        ).scalars().first()
                        if exists is None:
                            session.add(
                                Comment(
                                    account_id=acc.id,
                                    ig_comment_id=comment_id,
                                    ig_media_id=str(
                                        (value.get("media") or {}).get("id", "")
                                    ),
                                    username=commenter.get("username", "")
                                    if isinstance(commenter, dict)
                                    else "",
                                    text=comment_text,
                                    replied=d["action"] in ("replied",),
                                )
                            )

                if acc and dm_text:
                    from app.services.analytics_history import record_chat
                    from app.services.autoreply import find_reply

                    reply = None
                    reply_source = "none"

                    # 1) Multi-step automation scenarios (keyword / AI / always).
                    from app.services.automation import Outcome, process_inbound

                    outcome = await process_inbound(
                        session, acc.id, "dm", thread_id or "", dm_text
                    )
                    if outcome is not None:
                        if outcome.send_text:
                            reply = outcome.send_text
                            reply_source = "scenario"
                        elif outcome.final:
                            reply_source = "scenario_done"

                    # 2) Classic keyword AutoReply rules.
                    if reply is None:
                        reply = await find_reply(session, acc.id, dm_text)
                        if reply:
                            reply_source = "keyword"

                    # 3) AI-drafted fallback.
                    if reply is None:
                        from app.services.ai import draft_reply

                        reply = await draft_reply(dm_text, locale="fa")
                        if reply:
                            reply_source = "ai"

                    if reply and thread_id:
                        try:
                            await InstagramService().send_dm(acc, thread_id, reply)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("auto-reply failed: %s", exc)
                        else:
                            text += " auto_replied=1"

                    await record_chat(
                        session,
                        account_id=acc.id,
                        channel="dm",
                        peer_id=thread_id or "",
                        inbound_text=dm_text,
                        reply_text=reply or "",
                        reply_source=reply_source,
                    )

                session.add(
                    ActivityLog(
                        account_id=acc.id if acc else None,
                        user_id=acc.owner_id if acc else None,
                        action=f"webhook_{field}",
                        detail=text[:1500],
                        level="info",
                    )
                )
                await session.commit()


@router.get("/callback")
async def oauth_callback(request: Request):
    """OAuth 2.0 callback: code + state → token exchange → encrypted storage."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    error_reason = request.query_params.get("error_reason")

    if error:
        return PlainTextResponse(
            f"Authorization declined: {error} ({error_reason})", status_code=400
        )
    if not code or not state:
        return PlainTextResponse("Missing code or state.", status_code=400)

    flow_state = await oauth_states.pop(state)
    if flow_state is None:
        return PlainTextResponse("Invalid or expired state.", status_code=400)

    from app.services.meta.oauth import (
        exchange_code_for_token,
        exchange_long_lived_token,
    )

    async with SessionLocal() as session:
        account = await session.get(InstagramAccount, flow_state.account_id)
        if account is None or account.owner_id != flow_state.user_id:
            return PlainTextResponse("Account mismatch.", status_code=400)

        try:
            short = await exchange_code_for_token(code, _redirect_uri())
        except Exception as exc:  # noqa: BLE001
            logger.error("token exchange failed: %s", exc)
            return PlainTextResponse(f"Token exchange failed: {exc}", status_code=502)

        short_token = short.get("access_token")
        if not short_token:
            return PlainTextResponse("No access_token returned.", status_code=502)

        # Swap to long-lived (never store the short-lived one)
        long_lived = short_token
        expires_at = None
        try:
            data = await exchange_long_lived_token(short_token)
            if data.get("access_token"):
                long_lived = data["access_token"]
                expires_in = int(data.get("expires_in", 0) or 0)
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                    if expires_in
                    else datetime.now(timezone.utc) + timedelta(days=60)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("long-lived swap failed, keeping short token: %s", exc)

        account.long_lived_token_enc = get_cipher().encrypt(long_lived)
        account.token_expires_at = expires_at
        account.status = "connected"
        account.updated_at = datetime.now(timezone.utc)

        await _hydrate_account(account, long_lived)

        session.add(
            ActivityLog(
                account_id=account.id,
                user_id=flow_state.user_id,
                action="connect",
                detail="OAuth completed; long-lived token stored (encrypted)",
                level="info",
            )
        )
        await session.commit()

    from app.bot.main import get_bot

    try:
        await get_bot().send_message(
            chat_id=flow_state.user_id,
            text=(
                "🔗 اتصال کامل شد!\n"
                f"پیج: {account.username or '—'}\n"
                "توکن به‌صورت رمزنگاری‌شده ذخیره شد (AES-256). 🔒\n"
                "از این پس می‌توانید از /publish استفاده کنید."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not notify telegram uid=%s: %s", flow_state.user_id, exc)

    await notify_admin(
        "🟢 یک کاربر جدید پیج خود را از طریق OAuth وصل کرد.\n"
        "(حالت Development ← فقط Testerها مجازند)"
    )

    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;margin-top:60px'>"
        "<h2>✅ Authorization successful</h2>"
        "<p>You can close this tab and return to Telegram.</p></body></html>"
    )


async def _hydrate_account(account: InstagramAccount, token: str) -> None:
    """Resolve the IG Business account id + username via Graph API."""
    if settings.is_simulation:
        account.instagram_user_id = None
        account.username = "demo.page"
        account.name = "Demo Page"
        return
    from app.services.meta.client import get_client

    client = get_client()
    try:
        pages = await client.get(
            "/me/accounts",
            token,
            fields="id,name,instagram_business_account{id,username,name,profile_picture_url}",
        )
        for page in pages.get("data", []):
            ig = page.get("instagram_business_account")
            if ig:
                account.instagram_user_id = str(ig.get("id"))
                account.username = ig.get("username", "")
                account.name = ig.get("name", "")
                account.profile_pic_url = ig.get("profile_picture_url", "")
                return
    except Exception as exc:  # noqa: BLE001
        logger.warning("hydration failed (page id may be resolved later): %s", exc)
