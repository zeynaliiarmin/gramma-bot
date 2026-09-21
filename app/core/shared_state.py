"""Shared, process-wide singletons.

The Telegram bot, the celery workers and the tiny webhook HTTP server all run
in the *same* process here, so they share these stateful objects.
"""

from app.core.security.oauth_states import oauth_states  # noqa: F401
