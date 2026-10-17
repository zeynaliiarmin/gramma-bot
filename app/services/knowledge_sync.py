"""Nightly READ-ONLY Zernio Instagram history sweep for owner-review candidates.

Never calls ChatbotX/Zernio/Meta send endpoints. Zernio replays Instagram
history including native-app and other-app sends, but an absent sentVia is
UNKNOWN. All candidates require explicit owner approval before retrieval.
Only the uniquely connected @zeynalikids account is in scope.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import InstagramAccount, KnowledgeSyncState, ManualReplyCandidate
from app.services.knowledge import analyze_approved_style, stage_candidate
from app.services.messaging import FallbackUnavailable, _zernio_account_id
from app.services.zernio_publishing import normalize_username

logger = logging.getLogger("gramma.knowledge_sync")
_BASE = "https://zernio.com/api"


class SyncUnavailable(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result > datetime.now(timezone.utc) + timedelta(minutes=5):
            return None
        return result
    except (ValueError, OverflowError):
        return None


async def _read(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    try:
        response = await client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise SyncUnavailable("zernio_read_unavailable") from exc
    if response.status_code != 200:
        # Never print payloads, response bodies or credentials.
        raise SyncUnavailable(f"zernio_read_http_{response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise SyncUnavailable("zernio_invalid_json") from exc
    if not isinstance(body, dict):
        raise SyncUnavailable("zernio_invalid_payload")
    return body


def _meta_complete(body: dict) -> bool:
    meta = body.get("meta") or {}
    return isinstance(meta, dict) and not meta.get("accountsFailed") and not meta.get("failedAccounts")


async def _stage_dms(account_id: int, conversation: dict, messages: list[dict]) -> int:
    conversation_id = conversation["id"]
    peer_id = conversation.get("participantId")
    clean = [m for m in messages if isinstance(m, dict) and m.get("platform") == "instagram"
             and str(m.get("accountId") or "") == str(conversation["accountId"])
             and str(m.get("conversationId") or "") == conversation_id
             and not m.get("isDeleted") and m.get("id") and _moment(m.get("createdAt"))]
    clean.sort(key=lambda row: _moment(row["createdAt"]))
    last_incoming = None
    staged = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().data_retention_days)
    async with SessionLocal() as session:
        for row in clean:
            at = _moment(row["createdAt"])
            if row.get("direction") == "incoming":
                last_incoming = row if at and row.get("message") else None
                continue
            if row.get("direction") != "outgoing" or at is None or at < cutoff:
                continue
            # Explicit api/workflow/automation sends are never human examples.
            sent_via = row.get("sentVia")
            if sent_via not in (None, "human") or last_incoming is None:
                continue
            before = _moment(last_incoming.get("createdAt"))
            if before is None or not timedelta(0) <= at - before <= timedelta(hours=24):
                continue
            text = row.get("message")
            question = last_incoming.get("message")
            if not isinstance(text, str) or not isinstance(question, str):
                continue
            # An incoming story reply remains a distinct knowledge channel;
            # any outgoing reply in the same thread can answer it.
            channel = "story_reply" if last_incoming.get("storyReply") else "dm"
            metadata = row.get("metadata") or {}
            native_id = metadata.get("platformMessageId") if isinstance(metadata, dict) else None
            inserted = await stage_candidate(
                session, account_id=account_id, source="zernio", message_id=str(row["id"]),
                channel=channel, question=question, answer=text, created_at=at,
                origin="human" if sent_via == "human" else "unknown",
                platform_id=native_id if isinstance(native_id, str) else None,
                peer_id=str(peer_id) if peer_id else None,
            )
            staged += int(inserted)
        await session.commit()
    return staged


async def _scan_conversations(client: httpx.AsyncClient, z_account: str, local_id: int,
                              cursor: str | None, limit: int) -> tuple[int, str | None]:
    params = {"platform": "instagram", "accountId": z_account, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    body = await _read(client, "/v1/inbox/conversations", params)
    if not _meta_complete(body) or not isinstance(body.get("data"), list):
        raise SyncUnavailable("zernio_conversations_incomplete")
    count = 0
    for conv in body["data"]:
        if (not isinstance(conv, dict) or conv.get("platform") != "instagram"
                or str(conv.get("accountId") or "") != z_account
                or not isinstance(conv.get("id"), str) or not conv["id"]):
            continue
        messages = await _read(client, f"/v1/inbox/conversations/{quote(conv['id'], safe='')}/messages",
                               {"accountId": z_account, "sortOrder": "desc", "limit": 100})
        if not isinstance(messages.get("messages"), list):
            raise SyncUnavailable("zernio_messages_incomplete")
        count += await _stage_dms(local_id, conv, messages["messages"])
    page = body.get("pagination")
    if not isinstance(page, dict):
        raise SyncUnavailable("zernio_conversations_no_pagination")
    if page.get("hasMore"):
        next_cursor = page.get("nextCursor")
        if not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 4096:
            raise SyncUnavailable("zernio_conversations_cursor_missing")
        return count, next_cursor
    return count, None


async def _stage_comment_replies(account_id: int, root: dict, replies: list[dict]) -> int:
    if (not isinstance(root, dict) or not isinstance(root.get("id"), str)
            or not isinstance(root.get("message"), str)
            or (root.get("from") or {}).get("isOwner") is True):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().data_retention_days)
    count = 0
    async with SessionLocal() as session:
        for reply in replies:
            if not isinstance(reply, dict) or not isinstance(reply.get("from"), dict):
                continue
            at = _moment(reply.get("createdTime"))
            if (not reply["from"].get("isOwner") is True or at is None or at < cutoff
                    or not isinstance(reply.get("id"), str)
                    or not isinstance(reply.get("message"), str)
                    or (reply.get("parentId") and reply["parentId"] != root["id"])):
                continue
            inserted = await stage_candidate(
                session, account_id=account_id, source="zernio", message_id=reply["id"],
                channel="comment", question=root["message"], answer=reply["message"],
                created_at=at, origin="unknown", platform_id=None,
            )
            count += int(inserted)
        await session.commit()
    return count


async def _scan_posts(client: httpx.AsyncClient, z_account: str, local_id: int,
                      cursor: str | None, limit: int) -> tuple[int, str | None]:
    params = {"platform": "instagram", "accountId": z_account, "minComments": 1,
              "sortBy": "date", "sortOrder": "desc", "limit": limit}
    if cursor:
        params["cursor"] = cursor
    posts = await _read(client, "/v1/inbox/comments", params)
    if not _meta_complete(posts) or not isinstance(posts.get("data"), list):
        raise SyncUnavailable("zernio_comments_incomplete")
    count, thread_reads = 0, 0
    for post in posts["data"]:
        if (not isinstance(post, dict) or post.get("platform") != "instagram"
                or str(post.get("accountId") or "") != z_account
                or not isinstance(post.get("id"), str) or not post["id"]):
            continue
        path = f"/v1/inbox/comments/{quote(post['id'], safe='')}"
        comments = await _read(client, path, {"accountId": z_account, "limit": 80})
        if not isinstance(comments.get("comments"), list):
            raise SyncUnavailable("zernio_comment_thread_incomplete")
        # The post endpoint may expose inline first replies; use a second GET
        # only for threads with more replies and within the request budget.
        for root in comments["comments"]:
            if not isinstance(root, dict):
                continue
            inline = root.get("replies") or []
            if isinstance(inline, list):
                count += await _stage_comment_replies(local_id, root, inline)
            if (root.get("repliesHasMore") or (isinstance(root.get("replyCount"), int)
                                               and root["replyCount"] > len(inline))) and thread_reads < 6:
                thread_reads += 1
                detailed = await _read(client, path,
                                       {"accountId": z_account, "commentId": root.get("id"),
                                        "limit": 100})
                if isinstance(detailed.get("comments"), list):
                    count += await _stage_comment_replies(local_id, root, detailed["comments"])
    page = posts.get("pagination")
    if not isinstance(page, dict):
        raise SyncUnavailable("zernio_posts_no_pagination")
    if page.get("hasMore"):
        next_cursor = page.get("nextCursor")
        if not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 4096:
            raise SyncUnavailable("zernio_posts_cursor_missing")
        return count, next_cursor
    return count, None


async def _update_sync(account_id: int, **changes) -> None:
    async with SessionLocal() as session:
        state = await session.get(KnowledgeSyncState, account_id, with_for_update=True)
        if state is None:
            state = KnowledgeSyncState(account_id=account_id)
            session.add(state)
        for key, value in changes.items():
            setattr(state, key, value)
        state.updated_at = datetime.now(timezone.utc)
        await session.commit()


async def _sync_one() -> dict:
    async with SessionLocal() as session:
        matches = (await session.execute(select(InstagramAccount).where(
            InstagramAccount.status == "connected",
            InstagramAccount.username.ilike("zeynalikids"),
        ))).scalars().all()
        if len(matches) != 1 or normalize_username(matches[0].username) != "zeynalikids":
            return {"state": "page_not_unique", "staged": 0}
        account = matches[0]
        state = await session.get(KnowledgeSyncState, account.id)
        dm_cursor = state.dm_cursor if state else None
        comments_cursor = state.comments_cursor if state else None
        account_id = account.id
    try:
        z_account = await _zernio_account_id(account)  # verified, uniquely mapped GET
    except FallbackUnavailable as exc:
        await _update_sync(account_id, last_status="unavailable", last_error_code=exc.code)
        return {"state": "unavailable", "staged": 0}
    cfg = get_settings()
    count = 0
    async with httpx.AsyncClient(base_url=_BASE,
                                 headers={"Authorization": f"Bearer {cfg.zernio_api_key}",
                                          "Accept": "application/json"},
                                 timeout=7.0, follow_redirects=False) as client:
        # Always sweep current activity, then advance one older page each day.
        recent, new_cursor = await _scan_conversations(client, z_account, account_id, None, 10)
        count += recent
        if dm_cursor:
            old, next_cursor = await _scan_conversations(client, z_account, account_id, dm_cursor, 10)
            count += old
        else:
            next_cursor = new_cursor
        await _update_sync(account_id, dm_cursor=next_cursor)
        recent, new_cursor = await _scan_posts(client, z_account, account_id, None, 3)
        count += recent
        if comments_cursor:
            old, next_cursor = await _scan_posts(client, z_account, account_id, comments_cursor, 3)
            count += old
        else:
            next_cursor = new_cursor
        await _update_sync(account_id, comments_cursor=next_cursor)
    await _update_sync(account_id, last_status="ok", last_error_code=None,
                       last_sync_at=datetime.now(timezone.utc))
    return {"state": "ok", "staged": count}


async def run_nightly_knowledge_sync() -> dict:
    """Cron-only, bounded ingestion + at most one owner-approved style retry.

    No automatic approval, POST to social providers, or delivery paths.
    """
    try:
        result = await asyncio.wait_for(_sync_one(), timeout=55.0)
    except SyncUnavailable as exc:
        logger.warning("knowledge sync read failed code=%s", exc.code)
        async with SessionLocal() as session:
            page = (await session.execute(select(InstagramAccount.id).where(
                InstagramAccount.username.ilike("zeynalikids"),
                InstagramAccount.status == "connected"
            ))).scalars().all()
        if len(page) == 1:
            await _update_sync(page[0], last_status="unavailable", last_error_code=exc.code)
        result = {"state": "unavailable", "staged": 0}
    except asyncio.TimeoutError:
        result = {"state": "partial_timeout", "staged": 0}
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge sync failed class=%s", type(exc).__name__)
        result = {"state": "internal_error", "staged": 0}
    # A style inference is legal ONLY on a candidate the owner already approved.
    # A lack of free models simply leaves style_status=pending for a later night.
    if result["state"] == "ok":
        async with SessionLocal() as session:
            candidate_id = (await session.execute(select(ManualReplyCandidate.id).where(
                ManualReplyCandidate.status == "approved",
                ManualReplyCandidate.style_status == "pending",
            ).order_by(ManualReplyCandidate.approved_at.asc()).limit(1))).scalar_one_or_none()
        if candidate_id is not None:
            await analyze_approved_style(candidate_id)
    return result
