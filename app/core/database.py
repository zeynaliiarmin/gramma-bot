"""
Gramma — async SQLAlchemy engine / session factory.

Supports both PostgreSQL (production) and SQLite (local/dev) transparently
through the same async interface (`asyncpg` vs `aiosqlite`).
"""

from __future__ import annotations

import os
from typing import AsyncIterator

from sqlalchemy import BigInteger, Integer, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from app.core.config import get_settings

settings = get_settings()

# Auto-incrementing primary key that works on BOTH backends:
#   * PostgreSQL → BIGINT (IDENTITY / BIGSERIAL)
#   * SQLite     → INTEGER PRIMARY KEY (the only auto-incrementing type there)
# Lives here (not in app.models) so service-layer models can import it without
# creating an import cycle with the `app.models` package __init__.
BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")

# SQLite needs this pragma to allow concurrent reads from background tasks.
_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"timeout": 30}
else:
    # Serverless / pooled Postgres (Supabase): recycle connections before
    # the pgbouncer/transact pooler idle timeouts, so warm Lambda instances
    # never reuse a stale connection after a cold start.
    _is_pooler = "pooler" in settings.database_url or ":6543" in settings.database_url
    try:
        _pool_size = int(os.environ.get("POOL_SIZE", "5"))
    except (ValueError, TypeError):
        _pool_size = 5
    try:
        _max_overflow = int(os.environ.get("MAX_OVERFLOW", "5" if _is_pooler else "10"))
    except (ValueError, TypeError):
        _max_overflow = 5 if _is_pooler else 10

    # The Supabase *transaction* pooler (port 6543) multiplexes many server
    # leases over a fixed pool: keep per-instance pools small so bursts from
    # several warm Lambdas never exhaust the server-side pool.
    _engine_kwargs["pool_recycle"] = 150
    _engine_kwargs["pool_size"] = _pool_size
    _engine_kwargs["max_overflow"] = _max_overflow
    _engine_kwargs["pool_timeout"] = 10

    # Supabase transaction pooler (port 6543) does NOT support prepared
    # statements; asyncpg must not cache them.
    if _is_pooler:
        _engine_kwargs["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }

engine = create_async_engine(settings.database_url, **_engine_kwargs)

SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

# Base for all ORM models — import from here in every model module.
Base = declarative_base()

# True once the engine has actually been used for a real connection-above
# import time (serverless). Sub-millisecond events then resolve immediately.
_ENGINE_HAS_CONNECTED = False


async def _warmup() -> None:
    """Lazily exercised by the Vercel function on first use only."""
    global _ENGINE_HAS_CONNECTED
    if _ENGINE_HAS_CONNECTED:
        return
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _ENGINE_HAS_CONNECTED = True
    except Exception as exc:  # noqa: BLE001
        _ENGINE_HAS_CONNECTED = False
        import logging

        logging.getLogger("gramma.vercel").error(
            "db warmup failed: %s: %s", type(exc).__name__, exc
        )
        raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-style dependency that hands out a scoped DB session."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables (idempotent)."""
    from app import models  # noqa: F401  (register models)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
