"""Shared, process-wide singletons.

The Telegram bot, the celery workers and the tiny webhook HTTP server all run
in the *same* process here, so they share these stateful objects.
"""

from app.core.security.oauth_states import oauth_states  # noqa: F401

# ── Serverless observability (written by api/index.py, read by tests) ──
telegram_updates_seen: int = 0
telegram_last_update_id: int | None = None
telegram_last_processed_at: float | None = None
