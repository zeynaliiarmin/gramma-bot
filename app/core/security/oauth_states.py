"""
Gramma — state-machine helper for Instagram OAuth flows.

Guards against CSRF by binding a random `state` to a Telegram `user_id`.
States are short-lived and single-use.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass

_STATE_TTL_SECONDS = 15 * 60  # 15 minutes


@dataclass
class OAuthState:
    user_id: int
    account_id: int
    created_at: float


class OAuthStateStore:
    """In-memory state store (shared by the state-machine + the FastAPI
    webhook receiver via `app.core.shared_state`)."""

    def __init__(self) -> None:
        self._states: dict[str, OAuthState] = {}
        self._lock = asyncio.Lock()

    def create(self, user_id: int, account_id: int) -> str:
        state = secrets.token_urlsafe(32)
        self._states[state] = OAuthState(
            user_id=user_id, account_id=account_id, created_at=time.time()
        )
        return state

    async def pop(self, state: str) -> OAuthState | None:
        """Fetch + consume a state (single-use) if still valid."""
        async with self._lock:
            item = self._states.pop(state, None)
        if item is None:
            return None
        if time.time() - item.created_at > _STATE_TTL_SECONDS:
            return None
        return item

    def expire_old(self) -> None:
        """Drop expired entries (best-effort cleanup)."""
        now = time.time()
        for state in list(self._states):
            if now - self._states[state].created_at > _STATE_TTL_SECONDS:
                self._states.pop(state, None)


oauth_states = OAuthStateStore()
