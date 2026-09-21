"""Gramma — Mini-App authentication.

Two independent, layered checks gate every Mini-App API call:

  1. **Telegram initData (who you are)** — validated once when the Mini-App
     opens, using the canonical HMAC-SHA256 algorithm signed with the bot
     token (see app/core/security/tg_initdata.py). The validated telegram_id
     is exchanged for a short-lived HMAC ticket via `POST /api/auth/verify`.
  2. **Session ticket (what the API trusts)** — an HMAC-SHA256 hex hash of
     `<userId>:<expiresAt>` signed with `ENCRYPTION_KEY`, sent by the SPA as
     the `X-Mini-App-Hash` header on every request. Stored in sessionStorage
     only (never localStorage, never a cookie).

`initDataUnsafe.user.id` is never used as a source of truth — the hash is
always verified first.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException

from app.core.config import get_settings
from app.core.security.tg_initdata import validate_init_data

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


def verify_telegram_init_data(init_data: str) -> int | None:
    """Validate Telegram's initData and return the **verified** telegram id.

    Never trusts `initDataUnsafe`; the HMAC hash must match first.
    Returns None when initData is missing/forged/expired.
    """
    fields = validate_init_data(init_data, settings.telegram_bot_token)
    if not fields:
        return None
    user = fields.get("user")
    if not isinstance(user, dict):
        return None
    try:
        return int(user.get("id"))
    except (TypeError, ValueError):
        return None


async def resolve_miniapp_user(
    x_mini_app_hash: str | None = Header(default=None),
) -> MiniAppIdentity:
    """FastAPI dependency: validate the session-ticket header."""
    if not x_mini_app_hash:
        raise HTTPException(status_code=401, detail="missing auth header")
    identity = verify_ticket(x_mini_app_hash)
    if identity is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return identity
