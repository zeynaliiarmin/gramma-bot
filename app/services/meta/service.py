"""Gramma — Instagram publishing + community + insights service.

Every method receives an `account` row (with an ENCRYPTED token column) and
decrypts it only at call time. Nothing is ever logged with the raw token.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.core.security.crypto import get_cipher
from app.services.meta.client import GraphClient, GraphAPIError, get_client

if TYPE_CHECKING:
    from app.models import InstagramAccount

_MEDIA_PUBLISH_FIELDS = "id,status,permalink,caption,timestamp,media_type,media_url"


def _token(account: "InstagramAccount") -> str:
    """Decrypt the account access token (fails loudly if corrupt)."""
    return get_cipher().decrypt(account.long_lived_token_enc or "")


class InstagramService:
    """High-level operations grouped by feature area."""

    def __init__(self, client: GraphClient | None = None) -> None:
        self.client = client or get_client()

    # ── Account ───────────────────────────────────────────────
    async def get_account_info(self, account: "InstagramAccount") -> dict:
        """Fetch fresh profile metadata from the Graph API."""
        if not account.instagram_user_id:
            # Simulation path
            return {
                "username": account.username or "demo.page",
                "followers_count": 1234,
                "media_count": 56,
                "name": account.name or "Demo Page",
            }
        data = await self.client.get(
            f"/{account.instagram_user_id}",
            _token(account),
            fields="username,name,followers_count,media_count,profile_picture_url",
        )
        return data

    # ── Publishing ────────────────────────────────────────────
    async def publish_media(
        self, account: "InstagramAccount", media_url: str, caption: str,
        is_video: bool = False, is_reel: bool = False, is_story: bool = False,
    ) -> dict:
        """Create + publish a single media container (image/video/reel/story)."""
        token = _token(account)
        uname = account.username or "demo"

        if not account.instagram_user_id:  # Simulation
            await asyncio_sleep(0.6)
            return {"id": f"sim_{int(datetime.now().timestamp())}", "permalink": "https://instagram.com/p/demo"}

        media_type = "STORIES" if is_story else ("REELS" if is_reel else ("VIDEO" if is_video else "IMAGE"))
        container = await self.client.post(
            f"/{account.instagram_user_id}/media",
            token,
            media_type=media_type,
            media_url=media_url,
            caption=caption,
        )
        cid = container.get("id")
        if not cid:
            raise GraphAPIError(None, "empty_container", f"no container id for {uname}")

        publish = await self.client.post(
            f"/{account.instagram_user_id}/media_publish",
            token,
            creation_id=cid,
        )
        await self.client.get(
            f"/{publish.get('id')}", token, fields=_MEDIA_PUBLISH_FIELDS
        )
        return publish

    async def publish_carousel(
        self, account: "InstagramAccount", children: list[dict], caption: str
    ) -> dict:
        """Publish a carousel from already-created child containers.

        `children` is a list like [{"id": "cid1"}, {"id": "cid2"}].
        """
        token = _token(account)
        if not account.instagram_user_id:
            await asyncio_sleep(0.9)
            return {"id": f"sim_carousel_{int(datetime.now().timestamp())}", "permalink": "https://instagram.com/p/demo"}

        ordered = [{"id": c["id"]} for c in children]
        container = await self.client.post(
            f"/{account.instagram_user_id}/media",
            token,
            media_type="CAROUSEL",
            children=",".join(c["id"] for c in ordered),
            caption=caption,
        )
        cid = container.get("id")
        if not cid:
            raise GraphAPIError(None, "empty_container", "no carousel container id")
        return await self.client.post(
            f"/{account.instagram_user_id}/media_publish",
            token, creation_id=cid,
        )

    async def create_child_container(
        self, account: "InstagramAccount", media_url: str, is_video: bool = False
    ) -> dict:
        """Create one child container for a carousel."""
        token = _token(account)
        if not account.instagram_user_id:
            return {"id": f"sim_child_{int(datetime.now().timestamp())}"}
        media_type = "VIDEO" if is_video else "IMAGE"
        return await self.client.post(
            f"/{account.instagram_user_id}/media",
            token, is_carousel_item="true", media_type=media_type, media_url=media_url,
        )

    # ── Comments ──────────────────────────────────────────────
    async def list_comments(self, account: "InstagramAccount", media_id: str, limit: int = 25) -> list[dict]:
        if not account.instagram_user_id:
            return [
                {"id": f"sim_c{i}", "username": f"fan_{i}", "text": f"نمونه کامنت {i}", "timestamp": datetime.now(timezone.utc).isoformat()}
                for i in range(1, 6)
            ]
        data = await self.client.get(
            f"/{media_id}/comments", _token(account),
            fields="id,username,text,timestamp", limit=limit,
        )
        return data.get("data", [])

    async def reply_comment(self, account: "InstagramAccount", comment_id: str, message: str) -> dict:
        if not account.instagram_user_id:
            return {"id": f"sim_reply_{int(datetime.now().timestamp())}"}
        return await self.client.post(f"/{comment_id}/replies", _token(account), message=message)

    async def hide_comment(self, account: "InstagramAccount", comment_id: str, hide: bool = True) -> dict:
        if not account.instagram_user_id:
            return {"success": True}
        return await self.client.post(f"/{comment_id}", _token(account), hide=hide)

    async def delete_comment(self, account: "InstagramAccount", comment_id: str) -> dict:
        if not account.instagram_user_id:
            return {"success": True}
        return await self.client.request("DELETE", f"/{comment_id}", token=_token(account))

    async def private_reply(self, account: "InstagramAccount", comment_id: str, message: str) -> dict:
        """Send a DM as a reply to a comment (creates a thread)."""
        if not account.instagram_user_id:
            return {"id": f"sim_pr_{int(datetime.now().timestamp())}"}
        return await self.client.post(f"/{comment_id}/private_replies", _token(account), message=message)

    # ── Direct messages ───────────────────────────────────────
    async def list_conversations(self, account: "InstagramAccount", limit: int = 10) -> list[dict]:
        if not account.instagram_user_id:
            return [
                {
                    "id": f"sim_t{i}", "name": f"کاربر {i}",
                    "last_message": f"پیام نمونه برای تاپیک {i}",
                    "category": "needs_reply",
                }
                for i in range(1, 4)
            ]
        data = await self.client.get(
            f"/{account.instagram_user_id}/conversations", _token(account),
            fields="id,participants,messages{from,message,created_time}",
            platform="instagram", limit=limit,
        )
        return data.get("data", [])

    async def send_dm(self, account: "InstagramAccount", thread_id: str, text: str) -> dict:
        if not account.instagram_user_id:
            return {"id": f"sim_dm_{int(datetime.now().timestamp())}"}
        return await self.client.post(
            f"/{account.instagram_user_id}/messages", _token(account),
            recipient={"id": thread_id}, message={"text": text},
        )

    # ── Insights ──────────────────────────────────────────────
    async def get_insights(self, account: "InstagramAccount", metric: str, period: str = "day") -> dict:
        if not account.instagram_user_id:
            import random

            return {
                "name": metric,
                "value": random.randint(100, 5000),
                "period": period,
                "note": "(simulated data)",
            }
        return await self.client.get(
            f"/{account.instagram_user_id}/insights", _token(account),
            metric=metric, period=period,
        )


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
