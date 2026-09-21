"""
Gramma — async SQLAlchemy engine / session factory.

Supports both PostgreSQL (production) and SQLite (local/dev) transparently
through the same async interface (`asyncpg` vs `aiosqlite`).
"""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from app.core.config import get_settings

settings = get_settings()

# SQLite needs this pragma to allow concurrent reads from background tasks.
_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(settings.database_url, **_engine_kwargs)

SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

# Base for all ORM models — import from here in every model module.
Base = declarative_base()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-style dependency that hands out a scoped DB session."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables (idempotent)."""
    from app import models  # noqa: F401  (register models)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
