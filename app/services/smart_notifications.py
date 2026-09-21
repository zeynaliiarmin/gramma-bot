"""Gramma v3 — smart notifications (only important alerts).

Instead of spamming the owner, this tiny gate decides whether an event is
worth a Telegram push. High-signal events (publish failures, token expiry,
collab requests, security alerts) always notify; routine events are batched
or silently logged. The policy is pluggable by event `kind`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger("gramma.notify")

# Events that ALWAYS notify (high signal).
ALWAYS_NOTIFY = {
    "publish_failed",
    "publish_success",   # success of user-authored content is expected signal
    "token_expiring",
    "token_refresh_failed",
    "collab_request",
    "collab_accepted",
    "security_alert",
    "daily_report",
}

# Events that notify at most once per `COOLDOWN_SECONDS` (anti-spam).
THROTTLED = {
    "auto_reply_sent": 3600,
    "webhook_event": 1800,
}

_last_sent: dict[str, float] = {}


def should_notify(kind: str) -> bool:
    """Return True if a notification of `kind` should be sent now."""
    if not settings.smart_notifications_enabled:
        return True  # smart filtering off → send everything (default)
    if kind in ALWAYS_NOTIFY:
        return True
    if kind in THROTTLED:
        now = datetime.now(timezone.utc).timestamp()
        cooldown = THROTTLED[kind]
        last = _last_sent.get(kind, 0)
        if now - last < cooldown:
            return False
        _last_sent[kind] = now
        return True
    return False


def mark_sent(kind: str) -> None:
    _last_sent[kind] = datetime.now(timezone.utc).timestamp()
