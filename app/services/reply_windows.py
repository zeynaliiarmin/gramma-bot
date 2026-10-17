"""Fail-closed Meta automatic-reply windows, shared by claim, send and sweep.

A comment's *single private reply* to a post/reel is allowed for seven days;
DMs and story replies/mentions use the ordinary 24-hour messaging window.
Live-comment private replies end with the broadcast, which our providers do
not attest: never automatically send them. We do not assume an ad-triggered
extension or a HUMAN_AGENT tag for an automated reply.

A webhook delivery timestamp is NOT evidence of the original action time.
Only a verified, authenticated provider creation timestamp may open a window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WINDOW_MANUAL_REASONS = frozenset({
    "reply_window_expired", "comment_window_expired", "live_comment_unverified",
    "inbound_time_unverified", "comment_context_unverified", "reply_channel_unsupported",
})
_SAFETY_BUFFER = timedelta(minutes=5)


def automatic_reply_window_reason(inbox, now: datetime | None = None) -> str | None:
    """Return a private-review reason, or None only for a proven open window."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    context = inbox.context if isinstance(inbox.context, dict) else {}
    if context.get("inbound_timestamp_verified") is not True:
        return "inbound_time_unverified"
    received = inbox.received_at
    if not isinstance(received, datetime) or received.tzinfo is None:
        return "inbound_time_unverified"
    if received > now + timedelta(minutes=1):
        return "inbound_time_unverified"
    if inbox.channel == "comment":
        if context.get("live_comment") is True:
            return "live_comment_unverified"
        if not context.get("comment_id"):
            return "comment_context_unverified"
        return ("comment_window_expired" if now >= received + timedelta(days=7) - _SAFETY_BUFFER
                else None)
    if inbox.channel in ("dm", "story_reply", "story_mention"):
        return ("reply_window_expired" if now >= received + timedelta(hours=24) - _SAFETY_BUFFER
                else None)
    return "reply_channel_unsupported"
