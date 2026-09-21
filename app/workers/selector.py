"""Gramma — scheduler backend selection.

* `SCHEDULER_BACKEND=celery`      → assume an external celery beat/worker.
* `SCHEDULER_BACKEND=inprocess`   → always run the in-process APScheduler.
* `SCHEDULER_BACKEND=auto`        → run the in-process scheduler UNLESS Redis
  is reachable (which signals the full Celery stack is deployed).
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings

settings = get_settings()


async def redis_is_reachable(url: str | None = None) -> bool:
    """Best-effort TCP check against the Redis host/port."""
    url = url or settings.redis_url
    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover
        return False
    try:
        client = aioredis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0)
        await client.ping()
        await client.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


async def should_run_inprocess_scheduler() -> bool:
    """Return True when the bot process should run background jobs itself."""
    backend = settings.scheduler_backend
    if backend == "inprocess":
        return True
    if backend == "celery":
        return False
    # auto
    return not await redis_is_reachable()
