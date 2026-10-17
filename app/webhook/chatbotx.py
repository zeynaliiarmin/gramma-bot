"""Authenticated ChatbotX inbound webhook with durable, deduplicated ingestion.

During the Phase 4 rollout the *new* outbox worker stays OFF. The existing
ChatbotX keyword/AI path continues to reply inline, now guarded by a shared,
atomic DM+comment budget. Its shadow inbox rows are terminal and can NEVER be
sent again by the future worker. No 3–5-second human wait is introduced.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import ActivityLog, InboxMessage, InstagramAccount
from app.services.zernio_publishing import normalize_username

logger = logging.getLogger("gramma.webhook.chatbotx")
router = APIRouter(tags=["chatbotx"])


def _authorized(request: Request, raw: bytes) -> bool:
    """Fail closed: only an exact secret or a real HMAC of the raw body.

    Never accept substring matches, a missing secret, or simulation-mode
    bypasses. An exact legacy query/header token is retained for compatibility.
    """
    cfg = get_settings()
    tokens = tuple(dict.fromkeys(t for t in (
        cfg.chatbotx_api_channel_token, cfg.chatbotx_channel_token,
        cfg.chatbotx_workspace_token, cfg.chatbotx_webhook_secret,
    ) if t))
    if not tokens:
        logger.error("ChatbotX webhook rejected: no authentication secret configured")
        return False
    provided = [request.headers.get(name, "") for name in (
        "X-Inbox-Secret", "X-ChatbotX-Token", "X-Workspace-Token",
        "X-Channel-Token", "X-Api-Token", "X-ChatbotX-Signature",
    )]
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        provided.append(auth[7:].strip())
    provided.append(request.query_params.get("token") or request.query_params.get("secret") or "")
    for value in provided:
        if value and any(hmac.compare_digest(value, expected) for expected in tokens):
            return True
    # Some ChatbotX installations send a sha256= HMAC rather than a token.
    signature = request.headers.get("X-ChatbotX-Signature", "")
    if cfg.chatbotx_webhook_secret and signature:
        digest = hmac.new(cfg.chatbotx_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if hmac.compare_digest(signature.removeprefix("sha256="), digest):
            return True
    return False


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _string(value, maximum: int = 256) -> str:
    return str(value)[:maximum] if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def _extract_message(payload: dict) -> dict:
    data = _dict(payload.get("data")) or payload
    message = _dict(data.get("message"))
    contact = _dict(data.get("contact"))
    sender = _dict(data.get("sender"))
    comment = _dict(data.get("comment"))
    post = _dict(data.get("post"))
    metadata = _dict(data.get("metadata"))
    # ChatbotX echoes outgoing messages. Do not reply to our own replies.
    direction = _string(message.get("direction") or data.get("direction"), 32).lower()
    event = _string(payload.get("event") or data.get("event"), 64).lower()
    raw_kind = _string(data.get("type") or data.get("message_type"), 32).lower()
    if (direction in ("outgoing", "sent", "outbound")
            or event in ("message.sent", "message.outgoing")
            or raw_kind in ("message.sent", "message.outgoing", "outgoing")
            or message.get("is_from_me") is True or data.get("is_from_me") is True):
        return {"outgoing": True}
    channel = data.get("channel") or payload.get("channel")
    channel_name = _string(channel if not isinstance(channel, dict) else channel.get("platform"), 32).lower()
    kind = _string(data.get("type") or data.get("message_type") or payload.get("type"), 32).lower()
    if "comment" in kind or "comment" in event:
        kind = "comment"
    elif kind in ("story_reply", "story_mention"):
        pass
    else:
        kind = "dm"
    text = (
        message.get("text") or message.get("body") or message.get("content")
        or (data.get("message") if isinstance(data.get("message"), str) else None)
        or data.get("text") or data.get("body") or data.get("content") or ""
    )
    conv_id = _string(data.get("conversation_id") or data.get("conversationId")
                      or payload.get("conversation_id"))
    contact_id = _string(data.get("contact_id") or data.get("contactId") or contact.get("id"))
    message_id = _string(message.get("id") or data.get("message_id") or data.get("messageId")
                         or comment.get("id") or data.get("comment_id"), 256)
    # Strictly whitelist only IDs proven to refer to platform resources. A
    # ChatbotX contact_id / conversation_id is NOT a Zernio/Instagram ID.
    post_id = _string(data.get("post_id") or data.get("media_id") or post.get("id")
                      or message.get("post_id") or metadata.get("post_id"), 128)
    comment_id = _string(data.get("comment_id") or data.get("commentId")
                         or comment.get("id") or message.get("comment_id"), 128)
    peer_id = _string(contact.get("instagram_scoped_user_id") or contact.get("ig_scoped_user_id")
                      or sender.get("instagram_scoped_user_id") or sender.get("ig_scoped_user_id"), 128)
    return {
        "outgoing": False,
        "text": text.strip() if isinstance(text, str) else "",
        "conversation_id": conv_id,
        "contact_id": contact_id,
        "contact_name": _string(contact.get("name"), 128),
        "type": kind,
        "channel_name": channel_name,
        "message_id": message_id,
        "context": {"post_id": post_id, "comment_id": comment_id, "instagram_peer_id": peer_id},
    }


async def _resolve_account(payload: dict) -> InstagramAccount | None:
    data = _dict(payload.get("data")) or payload
    account = _dict(data.get("account"))
    channel = _dict(data.get("channel"))
    # Only fields explicitly about the BUSINESS PAGE may be used to route.
    page_id = _string(data.get("instagram_account_id") or data.get("ig_business_account_id")
                      or account.get("instagram_user_id") or account.get("instagram_account_id"), 64)
    username = normalize_username(_string(
        data.get("account_username") or data.get("instagram_username")
        or account.get("username") or channel.get("account_username")
        or channel.get("username"), 64
    ))
    async with SessionLocal() as session:
        if page_id or username:
            q = select(InstagramAccount).where(InstagramAccount.status == "connected")
            if page_id:
                q = q.where(InstagramAccount.instagram_user_id == page_id)
            if username:
                q = q.where(func.lower(InstagramAccount.username) == username)
            matches = (await session.execute(q)).scalars().all()
            return matches[0] if len(matches) == 1 else None
        # Explicitly approved transition: ONLY one connected page, exactly
        # @zeynalikids. Never route to "first connected" or an arbitrary page.
        rows = (await session.execute(select(InstagramAccount).where(
            InstagramAccount.status == "connected"
        ))).scalars().all()
        if len(rows) == 1 and normalize_username(rows[0].username) == "zeynalikids":
            return rows[0]
    return None


def _event_key(payload: dict, message: dict, account_id: int | None) -> str:
    ident = message.get("message_id")
    if ident:
        # One underlying inbound ID must never produce both a DM-typed and
        # comment-typed reply if the provider emits two views of the event.
        raw = f"page:{account_id or 'unmapped'}:inbound-id:{ident}".encode()
    else:
        # Identical webhook retries hash to the same key. Without a provider
        # message ID, two byte-identical events cannot be distinguished; fail
        # closed rather than send the same reply twice.
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


async def _persist_inbound(payload: dict, message: dict, account: InstagramAccount | None) -> tuple[int, bool]:
    mode = "pending" if get_settings().auto_reply_worker_enabled else "legacy_processing"
    if account is None or (message["channel_name"] and message["channel_name"] != "instagram"):
        mode = "unmapped"
    elif not message["text"] or (not message["conversation_id"] and not message["context"]["comment_id"]):
        mode = "legacy_no_reply" if mode == "legacy_processing" else "no_reply"
    key = _event_key(payload, message, account.id if account and mode != "unmapped" else None)
    async with SessionLocal() as session:
        row = InboxMessage(
            account_id=account.id if account and mode != "unmapped" else None,
            source="chatbotx", event_key=key, channel=message["type"],
            message_type="comment" if message["type"] == "comment" else "dm",
            external_id=key, sender_id=message["contact_id"] or "unknown",
            status="ignored",  # never consumed by the pre-existing Zernio inbox queue
            thread_id=message["conversation_id"] or None,
            parent_post_id=message["context"]["post_id"] or None,
            conversation_id=message["conversation_id"], contact_id=message["contact_id"],
            text=message["text"], context=message["context"], state=mode,
        )
        session.add(row)
        try:
            await session.flush()
            inbox_id = row.id
            # No raw payload or follower text in activity_logs.
            session.add(ActivityLog(
                account_id=row.account_id,
                user_id=account.owner_id if account and row.account_id else None,
                action="chatbotx_inbound", detail=f"inbox={inbox_id} channel={message['type']}",
                level="info",
            ))
            await session.commit()  # DURABLE before ANY provider POST
            return inbox_id, True
        except IntegrityError:
            await session.rollback()
            prior = (await session.execute(select(InboxMessage.id).where(
                InboxMessage.source == "chatbotx", InboxMessage.event_key == key
            ))).scalar_one_or_none()
            if prior is None:
                raise  # not a duplicate; let ChatbotX retry on failure
            return prior, False


@router.post("/api/chatbotx-webhook")
async def chatbotx_webhook(request: Request):
    raw = await request.body()
    if len(raw) > 256 * 1024:
        return Response(status_code=413, content="payload too large")
    if not _authorized(request, raw):
        return Response(status_code=403, content="forbidden")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400, content="bad json")
    message = _extract_message(payload)
    if message.get("outgoing"):
        return {"ok": True, "ignored": "outgoing"}
    try:
        account = await _resolve_account(payload)
        inbox_id, inserted = await _persist_inbound(payload, message, account)
    except Exception:  # noqa: BLE001
        logger.exception("ChatbotX durable inbound insert failed; provider should retry")
        return Response(status_code=503, content="inbox unavailable")
    if not inserted:
        return {"ok": True, "duplicate": True}
    if account is None or (message["channel_name"] and message["channel_name"] != "instagram"):
        from app.services.messaging import alert_admin
        try:
            await alert_admin(page="نامشخص", inbox_id=inbox_id, reason="page_not_identified")
        except Exception as exc:  # noqa: BLE001
            logger.warning("unmapped inbox alert failed: %s", type(exc).__name__)
        return {"ok": True, "replied": False, "reason": "page_not_identified"}
    if not message["text"]:
        return {"ok": True, "replied": False, "reason": "no_text"}
    if not message["conversation_id"] and not message["context"]["comment_id"]:
        return {"ok": True, "replied": False, "reason": "no_destination"}
    if get_settings().auto_reply_worker_enabled:
        # Only newly authenticated inbound rows enter this queue. Prior
        # inline shadow rows are terminal and can never be replayed.
        return {"ok": True, "queued": True, "inbox_id": inbox_id}
    from app.services.auto_reply_queue import legacy_reply
    result = await legacy_reply(inbox_id, account, message)
    if result.get("reason") == "no_match" and message["type"] == "dm":
        try:
            from app.services.notifications import notify_telegram
            async with SessionLocal() as session:
                await notify_telegram(session, account.owner_id,
                                      f"💬 پیام جدید بدون پاسخ خودکار برای @{account.username}.\nشناسه داخلی: {inbox_id}",
                                      kind="chatbotx_no_reply")
        except Exception as exc:  # noqa: BLE001
            logger.warning("no-match notification failed: %s", type(exc).__name__)
    return result
