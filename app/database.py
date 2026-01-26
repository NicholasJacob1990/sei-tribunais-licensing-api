"""
Database configuration and session management

Simplified version - uses pool_pre_ping for connection health checks.
"""
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


def _create_engine():
    """Create a new database engine."""
    connect_args = {}
    db_url = settings.async_database_url

    # Only use SSL for external Render connections
    if settings.is_production and ".render.com" in db_url:
        connect_args["ssl"] = "require"

    logger.info(f"Creating database engine (production={settings.is_production})")

    return create_async_engine(
        db_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,   # Auto-recover stale connections
        pool_recycle=300,     # Recycle connections every 5 min
        pool_timeout=30,      # Wait up to 30s for connection
        echo=settings.debug,
        connect_args=connect_args,
    )


# Initialize at module load time (eager initialization)
engine = _create_engine()

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


def get_engine():
    """Get the database engine."""
    return engine


def get_session_factory():
    """Get the session factory."""
    return async_session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency for database sessions.

    Uses pool_pre_ping for automatic connection recovery.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database sessions (for use outside FastAPI)."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Initialize database tables and run migrations."""
    import logging
    logger = logging.getLogger(__name__)

    engine = get_engine()

    # First: run migrations in a separate connection
    try:
        async with engine.begin() as conn:
            await _run_migrations(conn)
            logger.info("Migrations completed")
    except Exception as e:
        logger.warning(f"Migration step: {e}")

    # Then: ensure tables exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _run_migrations(conn) -> None:
    """Run pending schema migrations."""
    from sqlalchemy import text
    import logging
    logger = logging.getLogger(__name__)

    # Migration 002: Add password_hash column and make google_id nullable
    # Check if password_hash column exists
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'password_hash'
    """))
    if not result.fetchone():
        logger.info("Adding password_hash column...")
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"
        ))

    # Check if google_id is nullable
    result = await conn.execute(text("""
        SELECT is_nullable FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'google_id'
    """))
    row = result.fetchone()
    if row and row[0] == 'NO':
        logger.info("Making google_id nullable...")
        await conn.execute(text(
            "ALTER TABLE users ALTER COLUMN google_id DROP NOT NULL"
        ))

    # Migration 003: Add API token fields
    result = await conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'api_token_hash'
    """))
    if not result.fetchone():
        logger.info("Adding api_token_hash column...")
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN api_token_hash VARCHAR(255)"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN api_token_created_at TIMESTAMP WITH TIME ZONE"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_users_api_token_hash ON users(api_token_hash)"
        ))


async def close_db() -> None:
    """Close database connections."""
    engine = get_engine()
    await engine.dispose()
