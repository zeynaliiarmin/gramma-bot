"""Gramma — Mini-App authentication.

Every Mini-App request is authenticated with `X-Mini-App-Hash` — the
HMAC-SHA256 hash (hex) of `<userId>:<expiresAt>` signed with the secret
`ENCRYPTION_KEY`. We compute it server-side, embed it via the safe query
parameter `_token` in the WebApp button URL, and the frontend sends it back
as a header. Raw tokens are never persisted (no JWT in localStorage); the
hash is only valid within its expiry window.

This is a pragmatic auth for a bot used by a closed group of 5 testers,
with every following API call additionally scoped by the resolved user id.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.core.config import get_settings

settings = get_settings()


@dataclass
class MiniAppIdentity:
    user_id: int
    expires_at: int


def _secret() -> bytes:
    return settings.encryption_key.encode("ascii")


def make_miniapp_hash(user_id: int, expires_at: int) -> str:
    """HMAC-SHA256 hex hash of `<userId>:<expiresAt>`."""
    message = f"{user_id}:{expires_at}".encode("ascii")
    return hmac.new(_secret(), message, "sha256").hexdigest()


def make_miniapp_ticket(user_id: int, ttl_seconds: int = 24 * 3600) -> str:
    """Return a signed `user:expires:hash` ticket to embed in the WebApp URL."""
    expires_at = int(time.time()) + ttl_seconds
    return f"{user_id}:{expires_at}:{make_miniapp_hash(user_id, expires_at)}"


def verify_ticket(ticket: str) -> MiniAppIdentity | None:
    parts = ticket.split(":")
    if len(parts) != 3:
        return None
    try:
        user_id = int(parts[0])
        expires_at = int(parts[1])
    except ValueError:
        return None
    if int(time.time()) > expires_at:
        return None
    expected = make_miniapp_hash(user_id, expires_at)
    if not hmac.compare_digest(expected, parts[2]):
        return None
    return MiniAppIdentity(user_id=user_id, expires_at=expires_at)


async def resolve_miniapp_user(
    x_mini_app_hash: str | None = Header(default=None),
) -> MiniAppIdentity:
    """FastAPI dependency: validate the header and return the identity."""
    if not x_mini_app_hash:
        raise HTTPException(status_code=401, detail="missing auth header")
    identity = verify_ticket(x_mini_app_hash)
    if identity is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return identity
