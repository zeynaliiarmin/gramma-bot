"""Safe ChatbotX-primary automatic reply delivery with Zernio Inbox fallback.

Never reuse ChatbotX conversation/contact IDs as Zernio IDs. A fallback needs
an exact Zernio account handle AND a verified platform comment pair or an
exact Instagram-scoped participant match from Zernio's conversation list.
A timed-out or 5xx ChatbotX send is AMBIGUOUS: do not send via either provider
again; surface a Telegram admin alert for manual reconciliation.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.models import InboxMessage, InstagramAccount
from app.services.chatbotx_service import ChatbotXClient, ChatbotXError
from app.services.zernio_publishing import ZernioPublisher, normalize_username

logger = logging.getLogger("gramma.messaging")
ZERNIO_BASE = "https://zernio.com/api"


@dataclass(frozen=True)
class DeliveryOutcome:
    state: str  # sent | failed | unknown
    provider: str | None = None
    reason: str = ""
    provider_message_id: str | None = None
    platform_message_id: str | None = None


def _message_ids(result: object) -> tuple[str | None, str | None]:
    """Extract only explicitly returned IDs, never infer cross-provider IDs."""
    if not isinstance(result, dict):
        return None, None
    data = result.get("data") or result.get("message") or result
    if not isinstance(data, dict):
        return None, None
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    raw = message.get("id") or message.get("messageId") or message.get("_id")
    native = message.get("platformMessageId") or message.get("instagram_message_id")
    def valid(value):
        return value[:256] if isinstance(value, str) and 1 <= len(value) <= 256 else None
    return valid(raw), valid(native)


class FallbackUnavailable(Exception):
    """No safe mapping/permission exists. No Zernio POST was made."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def _zernio_get(client: httpx.AsyncClient, path: str, *, params: dict) -> dict:
    try:
        response = await client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise FallbackUnavailable("zernio_lookup_unavailable") from exc
    if response.status_code != 200:
        raise FallbackUnavailable(f"zernio_lookup_http_{response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise FallbackUnavailable("zernio_lookup_invalid") from exc
    if not isinstance(data, dict):
        raise FallbackUnavailable("zernio_lookup_invalid")
    return data


async def _zernio_account_id(account: InstagramAccount) -> str:
    """Exact, active Instagram handle only; never first-match another tenant."""
    if not get_settings().zernio_api_key or not normalize_username(account.username):
        raise FallbackUnavailable("zernio_not_configured")
    try:
        matches = [a for a in await ZernioPublisher().list_instagram_accounts()
                   if a["active"] and normalize_username(a["username"]) == normalize_username(account.username)]
    except Exception as exc:
        logger.warning("Zernio account lookup failed: %s", type(exc).__name__)
        raise FallbackUnavailable("zernio_account_lookup_failed") from exc
    if len(matches) != 1:
        raise FallbackUnavailable("zernio_account_not_unique_or_missing")
    return str(matches[0]["id"])


async def _zernio_dm_id(client: httpx.AsyncClient, account_id: str, peer_id: str) -> str:
    """Match a *platform-scoped* Instagram peer ID, never a ChatbotX contact ID."""
    if not peer_id or len(peer_id) > 128:
        raise FallbackUnavailable("instagram_peer_id_missing")
    cursor = None
    matches: set[str] = set()
    # Zernio's inbox can have many pages. If pagination is incomplete, the
    # match is not proved unique and fallback must be refused.
    for _ in range(5):
        params = {"platform": "instagram", "accountId": account_id, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = await _zernio_get(client, "/v1/inbox/conversations", params=params)
        meta = data.get("meta") or {}
        if not isinstance(meta, dict) or meta.get("accountsFailed"):
            raise FallbackUnavailable("zernio_conversations_incomplete")
        rows = data.get("data")
        if not isinstance(rows, list):
            raise FallbackUnavailable("zernio_conversations_invalid")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (row.get("platform") == "instagram"
                    and str(row.get("accountId") or "") == account_id
                    and str(row.get("participantId") or "") == peer_id
                    and row.get("status") == "active" and row.get("id")):
                matches.add(str(row["id"]))
        page = data.get("pagination") or {}
        if not isinstance(page, dict):
            raise FallbackUnavailable("zernio_conversations_invalid")
        if not page.get("hasMore"):
            if len(matches) != 1:
                raise FallbackUnavailable("zernio_conversation_not_unique_or_missing")
            return matches.pop()
        cursor = page.get("nextCursor")
        if not isinstance(cursor, str) or not cursor:
            raise FallbackUnavailable("zernio_conversations_incomplete")
    raise FallbackUnavailable("zernio_conversations_incomplete")


async def _zernio_fallback(account: InstagramAccount, inbox: InboxMessage, text: str) -> DeliveryOutcome:
    cfg = get_settings()
    account_id = await _zernio_account_id(account)
    headers = {"Authorization": f"Bearer {cfg.zernio_api_key}", "Accept": "application/json"}
    idempotency_key = "gramma-reply-" + hashlib.sha256(f"{inbox.id}:{account.id}".encode()).hexdigest()
    try:
        async with httpx.AsyncClient(
            base_url=ZERNIO_BASE, headers=headers, timeout=8.0, follow_redirects=False
        ) as client:
            peer_id = str(inbox.context.get("instagram_peer_id") or "")
            if inbox.channel == "comment" and not peer_id:
                # ChatbotX's conversation send is PRIVATE. Never silently
                # substitute a PUBLIC comment reply. The Zernio comment-to-DM
                # endpoint is private and requires exact platform IDs.
                post_id = str(inbox.context.get("post_id") or "")
                comment_id = str(inbox.context.get("comment_id") or "")
                if not (post_id and comment_id and len(post_id) <= 128 and len(comment_id) <= 128):
                    raise FallbackUnavailable("private_reply_identifiers_missing")
                path = (f"/v1/inbox/comments/{quote(post_id, safe='')}/"
                        f"{quote(comment_id, safe='')}/private-reply")
                body = {"accountId": account_id, "message": text}
                post_headers = {}  # private-reply does not document idempotency
            else:
                conversation_id = await _zernio_dm_id(client, account_id, peer_id)
                path = f"/v1/inbox/conversations/{quote(conversation_id, safe='')}/messages"
                body = {"accountId": account_id, "message": text}
                post_headers = {"Idempotency-Key": idempotency_key}
            # Exactly ONE POST. No SDK retries, no fallback retry after timeout.
            response = await client.post(path, json=body, headers=post_headers)
    except FallbackUnavailable:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        return DeliveryOutcome("failed", reason="zernio_connection_not_sent")
    except httpx.HTTPError:
        return DeliveryOutcome("unknown", reason="zernio_delivery_ambiguous")
    if 200 <= response.status_code < 300:
        try:
            returned = response.json()
        except ValueError:
            returned = {}
        message_id, native_id = _message_ids(returned)
        return DeliveryOutcome("sent", provider="zernio",
                               provider_message_id=message_id, platform_message_id=native_id)
    if response.status_code in (400, 401, 403, 404, 422, 429):
        return DeliveryOutcome("failed", reason=f"zernio_http_{response.status_code}")
    # Even a 5xx can happen after the underlying Instagram action succeeded.
    return DeliveryOutcome("unknown", reason=f"zernio_http_{response.status_code}_ambiguous")


async def deliver_auto_reply(
    account: InstagramAccount, inbox: InboxMessage, text: str, *, allow_fallback: bool = False
) -> DeliveryOutcome:
    """Send only from an authorized legacy webhook/approved worker call path.

    The caller must first reserve a per-page slot. Never call from the manual
    Mini-App send endpoint (which is intentionally outside the auto budget).
    """
    if not text or not text.strip() or len(text) > 2000 or not account or not inbox.conversation_id:
        return DeliveryOutcome("failed", reason="invalid_auto_reply")
    received = inbox.received_at or inbox.created_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= received + timedelta(hours=24) - timedelta(minutes=5):
        return DeliveryOutcome("failed", reason="reply_window_expired")
    client = ChatbotXClient(timeout=8.0)
    try:
        returned = await client.send_message(inbox.conversation_id, text)
        message_id, native_id = _message_ids(returned)
        return DeliveryOutcome("sent", provider="chatbotx",
                               provider_message_id=message_id, platform_message_id=native_id)
    except ChatbotXError as exc:
        if not exc.definitely_not_sent:
            logger.warning("ChatbotX auto send ambiguous account=%s inbox=%s status=%s",
                           account.id, inbox.id, exc.status_code)
            return DeliveryOutcome("unknown", reason="chatbotx_delivery_ambiguous")
        logger.warning("ChatbotX auto send rejected account=%s inbox=%s status=%s",
                       account.id, inbox.id, exc.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ChatbotX auto send uncertain: %s", type(exc).__name__)
        return DeliveryOutcome("unknown", reason="chatbotx_delivery_ambiguous")
    # The legacy inline path may only use its existing ChatbotX provider.
    # Only the approved Worker with a verified mapping may use Zernio fallback.
    if not allow_fallback or not get_settings().auto_reply_worker_enabled:
        return DeliveryOutcome("failed", reason="chatbotx_rejected_fallback_inactive")
    try:
        return await _zernio_fallback(account, inbox, text)
    except FallbackUnavailable as exc:
        logger.warning("Zernio fallback unavailable account=%s inbox=%s code=%s",
                       account.id, inbox.id, exc.code)
        return DeliveryOutcome("failed", reason=exc.code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Zernio fallback uncertain: %s", type(exc).__name__)
        return DeliveryOutcome("unknown", reason="zernio_delivery_ambiguous")


async def alert_admin(*, page: str, inbox_id: int, reason: str, ambiguous: bool = False) -> None:
    """Telegram admin-only failure notice. Called only by live production paths.

    Never include inbound text, credentials, raw HTTP responses or contact IDs.
    No caller in build/static verification should invoke this function.
    """
    cfg = get_settings()
    if not cfg.admin_ids:
        logger.error("Auto-reply needs manual review but ADMIN_TELEGRAM_IDS is empty: inbox=%s", inbox_id)
        return
    from aiogram import Bot

    safe_page = (page or "نامشخص")[:64]
    code = (reason or "unknown")[:64]
    if reason == "page_not_identified":
        text = "⚠️ پیام بدون شناسه پیج دریافت شد و نیاز به بررسی دارد.\n"
    elif reason == "llm_unavailable":
        text = "⚠️ مدلِ رایگانِ تأییدشده پاسخ نداد؛ پیام تا ساعت 13 روز بعد تهران در Hold است. اگر پنجرهٔ 24ساعته تمام شود فقط در مینی‌اپ بررسی می‌شود.\n"
    elif ambiguous:
        text = f"⚠️ وضعیت پیام داخلی {inbox_id} نامشخص است؛ لطفاً بررسی کن.\n"
    else:
        text = "⚠️ پاسخ خودکار ارسال نشد؛ بررسی دستی لازم است.\n"
    text += f"پیج: @{safe_page}\nشناسه پیام داخلی: {inbox_id}\nعلت: {code}"
    bot = Bot(token=cfg.telegram_bot_token)
    try:
        for telegram_id in sorted(cfg.admin_ids):
            try:
                await bot.send_message(chat_id=telegram_id, text=text, parse_mode=None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-reply admin alert failed: %s", type(exc).__name__)
    finally:
        await bot.session.close()
