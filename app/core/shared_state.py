"""Shared, process-wide singletons.

The Telegram bot, the celery workers and the tiny webhook HTTP server all run
in the *same* process here, so they share these stateful objects.
"""

from app.core.security.oauth_states import oauth_states  # noqa: F401

# ── Serverless observability (written by api/index.py, read by tests) ──
telegram_updates_seen: int = 0
telegram_last_update_id: int | None = None
telegram_last_processed_at: float | None = None

# Best-effort per-process update dedupe: within one warm Lambda container,
# Telegram may re-deliver the same update_id while we answer slowly; a
# bounded LRU set lets us skip genuine duplicates without holding state
# across cold starts (state lives in the DB, never here).
from collections import OrderedDict  # noqa: E402


class _SeenUpdates:
    __slots__ = ("_lru", "maxlen")

    def __init__(self, maxlen: int = 256):
        self._lru: "OrderedDict[int, bool]" = OrderedDict()
        self.maxlen = maxlen

    def add(self, update_id: int) -> None:
        if update_id is None or update_id <= 0:
            return
        self._lru[update_id] = True
        self._lru.move_to_end(update_id)
        while len(self._lru) > self.maxlen:
            self._lru.popitem(last=False)

    def __contains__(self, update_id: int) -> bool:
        return update_id is not None and update_id in self._lru


seen_update_ids = _SeenUpdates()
