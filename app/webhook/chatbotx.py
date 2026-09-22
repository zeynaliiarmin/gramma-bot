"""Gramma — ChatbotX API Channel webhook handler.

Receives inbound Instagram messages (comment/dm/story_reply) from ChatbotX
API Channel and routes them through Gramma's automation engine:

  Instagram -> ChatbotX (API Channel) -> POST /api/chatbotx-webhook -> Gramma
    -> check auto_replies (Supabase)
    -> if match -> reply via ChatbotX API
    -> else if AI enabled -> AvalAI reply via ChatbotX API
    -> else -> log + optional admin notify

Security:
  * Validates X-ChatbotX-Signature / X-Inbox-Secret / X-ChatbotX-Token / Authorization
    against the channel token (YOUR_CHANNEL_TOKEN_HERE) and workspace token
  * Logs all inbound/outbound in activity_logs
  * Returns 200 quickly to avoid ChatbotX retries
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import InstagramAccount
from app.services.autoreply import find_rule
from app.services.reply_limits import get_counter, tehran_date

settings = get_settings()
logger = logging.getLogger("gramma.webhook.chatbotx")

router = APIRouter(tags=["chatbotx"])


def _is_authorized(request: Request) -> bool:
    """Check ChatbotX webhook signature / token.

    ChatbotX may send one of:
      X-ChatbotX-Signature, X-Inbox-Secret, X-ChatbotX-Token, X-Workspace-Token,
      Authorization: Bearer <token>
    We accept channel token or workspace token.
    If no secret is configured, allow (for local testing).
    """
    channel_token = getattr(settings, "chatbotx_api_channel_token", "") or getattr(settings, "chatbotx_channel_token", "") or ""
    # Also try from env we added
    if not channel_token:
        # fallback to reading from .env directly via settings if new field not yet in config
        # settings may have chatbotx_workspace_token which is also acceptable
        pass

    # Collect all possible provided secrets
    provided = []
    for header_name in (
        "X-ChatbotX-Signature",
        "X-Inbox-Secret",
        "X-ChatbotX-Token",
        "X-Workspace-Token",
        "X-Channel-Token",
        "X-Api-Token",
    ):
        val = request.headers.get(header_name)
        if val:
            provided.append(val)

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        provided.append(auth[7:].strip())

    # Also check query param ?token=
    q_token = request.query_params.get("token") or request.query_params.get("secret")
    if q_token:
        provided.append(q_token)

    # If no channel token configured, allow (dev mode)
    # We check for both channel and workspace tokens
    valid_tokens = []
    # From settings
    if getattr(settings, "chatbotx_api_channel_token", None):
        valid_tokens.append(settings.chatbotx_api_channel_token)
    if getattr(settings, "chatbotx_channel_token", None):
        valid_tokens.append(settings.chatbotx_channel_token)
    if getattr(settings, "chatbotx_workspace_token", None):
        valid_tokens.append(settings.chatbotx_workspace_token)
    if getattr(settings, "chatbotx_webhook_secret", None):
        valid_tokens.append(settings.chatbotx_webhook_secret)

    # Also from env directly (in case config not reloaded)
    import os

    for env_key in ("CHATBOTX_API_CHANNEL_TOKEN", "CHATBOTX_CHANNEL_TOKEN", "CHATBOTX_WORKSPACE_TOKEN", "CHATBOTX_WEBHOOK_SECRET"):
        env_val = os.getenv(env_key)
        if env_val and env_val not in valid_tokens:
            valid_tokens.append(env_val)

    if not valid_tokens:
        # No secret configured — allow for testing
        logger.warning("chatbotx webhook: no valid tokens configured, allowing")
        return True

    # Constant-time compare against any valid token
    for prov in provided:
        for valid in valid_tokens:
            if not valid:
                continue
            # Support both exact match and HMAC signature (if ChatbotX signs payload)
            if hmac.compare_digest(prov.encode(), valid.encode()):
                return True
            # If provided looks like signature (hex), try HMAC verification of body
            # For now, simple contains check for token substring
            if valid in prov:
                return True

    # If no provided token but we have valid tokens, check if request is from known ChatbotX IP?
    # For now, if no token provided at all, deny unless dev
    if not provided:
        # Allow if in simulation/dev mode for testing
        if getattr(settings, "instagram_account_mode", "simulation") == "simulation":
            return True
        logger.warning("chatbotx webhook: no auth header provided")
        return False

    logger.warning("chatbotx webhook: auth failed, provided=%s", [p[:10] + "..." for p in provided])
    return False


def _extract_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract normalized message from ChatbotX payload (flexible parser)."""
    # Try various shapes
    # Shape 1: {event, data: {conversation_id, contact_id, message: {text}, contact, channel, type}}
    # Shape 2: {conversation_id, contact_id, message, type, channel, contact}
    # Shape 3: {message: {text}, conversation_id, ...}

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    # Message text
    msg_text = ""
    if isinstance(data.get("message"), dict):
        msg_text = data["message"].get("text") or data["message"].get("body") or data["message"].get("content") or ""
    elif isinstance(data.get("message"), str):
        msg_text = data["message"]
    else:
        msg_text = data.get("text") or data.get("body") or data.get("content") or ""

    conversation_id = data.get("conversation_id") or data.get("conversationId") or payload.get("conversation_id") or ""
    contact_id = data.get("contact_id") or data.get("contactId") or data.get("contact_id") or ""
    contact = data.get("contact") or {}
    channel = data.get("channel") or payload.get("channel") or "instagram"
    msg_type = data.get("type") or data.get("message_type") or payload.get("type") or "dm"
    # Normalize type
    if msg_type not in ("comment", "dm", "story_reply", "message", "story_mention"):
        # Infer from channel or other fields
        if "comment" in str(payload).lower():
            msg_type = "comment"
        else:
            msg_type = "dm"

    return {
        "text": str(msg_text or "").strip(),
        "conversation_id": str(conversation_id),
        "contact_id": str(contact_id),
        "contact": contact,
        "channel": channel,
        "type": msg_type,
        "raw": payload,
    }


async def _find_account_for_chatbotx() -> Optional[InstagramAccount]:
    """Find the Instagram account linked via ChatbotX (e.g., @zeynalikids)."""
    async with SessionLocal() as session:
        # Try to find by username zeynalikids
        result = await session.execute(
            select(InstagramAccount).where(
                InstagramAccount.username.ilike("%zeynalikids%")
            )
        )
        acc = result.scalars().first()
        if acc:
            return acc

        # Fallback: first connected account
        result = await session.execute(
            select(InstagramAccount).where(InstagramAccount.status == "connected").order_by(InstagramAccount.id)
        )
        acc = result.scalars().first()
        if acc:
            return acc

        # Fallback: any account
        result = await session.execute(select(InstagramAccount).order_by(InstagramAccount.id))
        return result.scalars().first()


async def _log_activity(account_id: Optional[int], user_id: Optional[int], action: str, detail: str, level: str = "info"):
    try:
        from app.models import ActivityLog

        async with SessionLocal() as session:
            session.add(
                ActivityLog(
                    account_id=account_id,
                    user_id=user_id,
                    action=action,
                    detail=detail[:2000],
                    level=level,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("activity log failed: %s", exc)


async def _check_daily_limit(account: InstagramAccount) -> tuple[bool, int, int]:
    """Check if comment daily limit exceeded. Returns (exceeded, used, limit)."""
    from app.services import limits

    try:
        async with SessionLocal() as session:
            counter = await get_counter(session, account.id)
            cap = limits.reply_limit_for_account(account.created_at)
            used = counter.replies_count if counter else 0
            return (used >= cap, used, cap)
    except Exception:
        return (False, 0, 1000)


async def _generate_ai_reply(text: str, account_id: Optional[int] = None, user_id: Optional[int] = None) -> Optional[str]:
    """Generate AI reply via AvalAI / OpenClaw."""
    try:
        # Try OpenClaw first (central brain)
        if getattr(settings, "openclaw_base_url", None):
            try:
                from app.services.openclaw import suggest_dm_reply, suggest_comment_reply

                # Use comment or DM based on context
                if len(text) < 200:  # likely comment
                    reply = await suggest_comment_reply(text, user_id=user_id, account_id=account_id)
                else:
                    reply = await suggest_dm_reply(text, user_id=user_id, account_id=account_id)
                if reply:
                    return reply
            except Exception as exc:
                logger.warning("openclaw AI failed: %s", exc)

        # Fallback to direct AvalAI
        from app.services.ai import draft_reply

        reply = await draft_reply(text, locale="fa")
        return reply
    except Exception as exc:
        logger.warning("AI reply generation failed: %s", exc)
        return None


@router.post("/api/chatbotx-webhook")
async def chatbotx_webhook(request: Request):
    """Main webhook for ChatbotX API Channel."""
    # Security check
    if not _is_authorized(request):
        return Response(status_code=403, content="forbidden")

    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400, content="bad json")

    msg = _extract_message(payload)
    text = msg["text"]
    conv_id = msg["conversation_id"]
    contact_id = msg["contact_id"]
    msg_type = msg["type"]

    if not text and not conv_id:
        # Empty payload, just ack
        logger.info("chatbotx webhook empty, ack")
        return {"ok": True, "empty": True}

    logger.info("chatbotx inbound: type=%s conv=%s text=%.100s", msg_type, conv_id, text)

    # Find account
    account = await _find_account_for_chatbotx()
    account_id = account.id if account else None
    user_id = account.owner_id if account else None

    # Log inbound
    await _log_activity(
        account_id, user_id,
        "chatbotx_inbound",
        f"type={msg_type} conv={conv_id} contact={contact_id} text={text[:200]}",
        "info",
    )

    # If comment, check daily limit
    if msg_type == "comment" and account:
        exceeded, used, cap = await _check_daily_limit(account)
        if exceeded:
            await _log_activity(
                account_id, user_id,
                "chatbotx_daily_limit",
                f"comment daily limit reached {used}/{cap}, queuing conv {conv_id}",
                "warning",
            )
            # Queue logic: for now, just log and don't reply (will be retried tomorrow via queue)
            # We could insert into comment_reply_queue
            try:
                from app.services.reply_limits import CommentReplyQueue

                async with SessionLocal() as session:
                    session.add(
                        CommentReplyQueue(
                            account_id=account.id,
                            comment_id=conv_id[:64] if conv_id else f"chatbotx_{contact_id}",
                            comment_text=text[:1000],
                            status="pending",
                        )
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning("queue insert failed: %s", exc)
            return {"ok": True, "queued": True, "reason": "daily_limit"}

    # 1. Check auto_replies rules via reply_engine.route_incoming_message
    reply_text = None
    rule_id = None
    ai_used = False
    decision = None
    if account:
        try:
            from app.services.reply_engine import route_incoming_message

            async with SessionLocal() as session:
                # Need to re-fetch account in this session for FK
                acc_in_session = await session.get(InstagramAccount, account.id)
                if acc_in_session:
                    decision = await route_incoming_message(
                        session,
                        acc_in_session,
                        message_text=text,
                        message_type=msg_type,
                        contact_id=contact_id,
                        conversation_id=conv_id,
                    )
                    if decision.should_reply:
                        reply_text = decision.reply_text
                        rule_id = decision.auto_reply_id
                        ai_used = decision.ai_used
                        # Commit matches increment
                        await session.commit()
                    else:
                        await session.rollback()
                    await _log_activity(
                        account_id, user_id,
                        f"chatbotx_{decision.source}_match" if decision.matched or decision.ai_used else "chatbotx_no_match",
                        f"type={msg_type} text={text[:100]} -> reply={reply_text[:100] if reply_text else 'none'} source={decision.source}",
                        "info",
                    )
        except Exception as exc:
            logger.warning("route_incoming_message failed: %s", exc)
            # Fallback to old direct find_rule logic
            try:
                async with SessionLocal() as session:
                    rule = await find_rule(session, account.id, text)
                    if rule:
                        reply_text = rule.reply
                        rule_id = rule.id
                        rule.matches = (rule.matches or 0) + 1
                        await session.commit()
                        await _log_activity(
                            account_id, user_id,
                            "chatbotx_rule_match",
                            f"rule {rule_id} matched for text={text[:100]} -> reply={reply_text[:100]}",
                            "info",
                        )
            except Exception as exc2:
                logger.warning("fallback rule matching failed: %s", exc2)

    # 3. If still no reply, optionally notify admin and store
    if not reply_text:
        await _log_activity(
            account_id, user_id,
            "chatbotx_no_reply",
            f"no rule/AI for conv={conv_id} text={text[:200]}",
            "info",
        )
        # Optionally notify admin (only for DMs, not for comments to avoid spam)
        if msg_type == "dm":
            try:
                from app.services.notifications import notify_telegram

                async with SessionLocal() as session:
                    if user_id:
                        await notify_telegram(
                            session, user_id,
                            f"💬 پیام جدید بدون پاسخ خودکار:\n"
                            f"نوع: {msg_type}\n"
                            f"متن: {text[:300]}\n"
                            f"مکالمه: {conv_id}\n\n"
                            f"برای افزودن قانون، به تب اتوماسیون بروید.",
                            kind="chatbotx_no_reply",
                        )
            except Exception as exc:
                logger.warning("admin notify failed: %s", exc)
        return {"ok": True, "replied": False, "reason": "no_match"}

    # 4. Send reply via ChatbotX API
    if not conv_id:
        logger.warning("no conversation_id, cannot send reply")
        return {"ok": True, "replied": False, "reason": "no_conversation_id", "would_reply": reply_text[:100]}

    try:
        from app.services.chatbotx_service import get_chatbotx_client

        client = get_chatbotx_client()
        if not client.enabled:
            logger.warning("chatbotx client disabled, cannot send")
            return {"ok": True, "replied": False, "reason": "client_disabled", "would_reply": reply_text[:100]}

        result = await client.send_message(conv_id, reply_text)
        await _log_activity(
            account_id, user_id,
            "chatbotx_outbound",
            f"sent to conv={conv_id} rule={rule_id} ai={ai_used} text={reply_text[:200]} result={str(result)[:200]}",
            "info",
        )

        # Record chat history
        try:
            from app.services.analytics_history import record_chat

            async with SessionLocal() as session:
                await record_chat(
                    session,
                    account_id=account_id or 0,
                    channel=msg_type,
                    peer_id=contact_id or conv_id,
                    peer_name=msg["contact"].get("name") if isinstance(msg["contact"], dict) else "",
                    inbound_text=text,
                    reply_text=reply_text,
                    reply_source=f"chatbotx_{'ai' if ai_used else 'rule'}_{rule_id or ''}",
                )
                await session.commit()
        except Exception as exc:
            logger.warning("chat history record failed: %s", exc)

        # If comment, record in daily counter
        if msg_type == "comment" and account:
            try:
                from app.services.reply_limits import record_reply

                async with SessionLocal() as session:
                    await record_reply(session, account.id)
                    await session.commit()
            except Exception as exc:
                logger.warning("record_reply failed: %s", exc)

        return {"ok": True, "replied": True, "ai": ai_used, "rule_id": rule_id, "result": result}

    except Exception as exc:
        logger.exception("chatbotx send failed")
        await _log_activity(
            account_id, user_id,
            "chatbotx_send_failed",
            f"failed to send to conv={conv_id}: {exc} reply={reply_text[:100]}",
            "error",
        )
        # Return 200 to avoid ChatbotX retry loop, but include error details
        return {
            "ok": True,
            "replied": False,
            "send_error": str(exc)[:500],
            "would_reply": reply_text[:200],
            "conversation_id": conv_id,
        }


@router.get("/api/chatbotx-webhook")
async def chatbotx_webhook_get():
    """GET for verification / health."""
    return {"ok": True, "webhook": "chatbotx", "time": datetime.now(timezone.utc).isoformat()}
