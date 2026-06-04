"""
database.py — Async SQLAlchemy setup for the Intelligence API.

Uses SQLite by default (aiosqlite driver).
Switch to PostgreSQL by setting DATABASE_URL in environment:
    DATABASE_URL=postgresql+asyncpg://user:pass@host/dbname
"""

import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger("db")

# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────
_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/store_intelligence.db")

# For SQLite: enable WAL mode for concurrent reads + single writer
_CONNECT_ARGS = {}
if "sqlite" in _DATABASE_URL:
    _CONNECT_ARGS = {"check_same_thread": False}

engine = create_async_engine(
    _DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args=_CONNECT_ARGS,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


# ─────────────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Dependency
# ─────────────────────────────────────────────────────────────────────────────
async def get_db() -> AsyncSession:  # type: ignore[return]
    async with AsyncSessionLocal() as session:
        yield session


# ─────────────────────────────────────────────────────────────────────────────
# Init
# ─────────────────────────────────────────────────────────────────────────────
async def init_db():
    """Create all tables and enable SQLite WAL mode."""
    # Import models so Base has them registered
    from app.db import orm  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # SQLite WAL mode for better concurrency
    if "sqlite" in _DATABASE_URL:
        async with AsyncSessionLocal() as session:
            await session.execute(text("PRAGMA journal_mode=WAL"))
            await session.execute(text("PRAGMA synchronous=NORMAL"))
            await session.commit()

    logger.info("Database ready: %s", _DATABASE_URL.split("///")[-1])


async def check_db_health() -> bool:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("DB health check failed: %s", e)
        return False
