"""
Database configuration and session management

Includes automatic connection recovery for Render free tier database restarts.
"""
import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import wraps
from typing import TypeVar, Callable, Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.exc import (
    OperationalError,
    InterfaceError,
    DisconnectionError,
)

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


# Lazy initialization of engine and session factory
_engine = None
_async_session_factory = None
_engine_lock = asyncio.Lock()

# Connection error patterns that indicate stale connections
CONNECTION_ERROR_PATTERNS = [
    "connection was closed",
    "connection is closed",
    "server closed the connection",
    "connection refused",
    "connection reset",
    "connection timed out",
    "terminating connection",
    "cannot allocate memory",
    "too many connections",
]


def _is_connection_error(error: Exception) -> bool:
    """Check if an exception is a connection-related error."""
    error_str = str(error).lower()
    return any(pattern in error_str for pattern in CONNECTION_ERROR_PATTERNS)


def _create_engine():
    """Create a new database engine."""
    connect_args = {}
    # Only use SSL for external connections (hostname with .render.com)
    db_url = settings.async_database_url
    if settings.is_production and ".render.com" in db_url:
        connect_args["ssl"] = "require"

    return create_async_engine(
        settings.async_database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,  # Verify connection before using
        pool_recycle=60,     # Recycle connections after 1 min (was 5)
        pool_timeout=30,     # Timeout waiting for connection
        echo=settings.debug,
        connect_args=connect_args,
    )


def get_engine():
    """Get or create the database engine."""
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


async def reset_engine():
    """Dispose and recreate the engine (for connection recovery)."""
    global _engine, _async_session_factory

    async with _engine_lock:
        if _engine is not None:
            logger.warning("Disposing stale database engine...")
            try:
                await _engine.dispose()
            except Exception as e:
                logger.error(f"Error disposing engine: {e}")
            _engine = None
            _async_session_factory = None

        # Recreate engine
        _engine = _create_engine()
        _async_session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info("Database engine recreated successfully")


def get_session_factory():
    """Get or create the session factory."""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_session_factory


# Keep backwards compatibility
@property
def engine():
    return get_engine()


@property
def async_session_factory():
    return get_session_factory()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for getting database sessions with automatic retry.

    On connection errors, disposes the engine and retries with a fresh connection.
    """
    from fastapi import HTTPException

    max_retries = 3
    retry_delay = 1.0
    last_error = None

    logger.debug("get_db called")

    for attempt in range(max_retries):
        factory = get_session_factory()
        logger.debug(f"get_db attempt {attempt + 1}/{max_retries}")
        try:
            async with factory() as session:
                # Test the connection first
                from sqlalchemy import text
                await session.execute(text("SELECT 1"))
                logger.debug("get_db connection test passed")

                try:
                    yield session
                    await session.commit()
                    logger.debug("get_db commit successful")
                except Exception as e:
                    logger.warning(f"get_db rollback due to: {e}")
                    await session.rollback()
                    raise
                return  # Success, exit the retry loop

        except (OperationalError, InterfaceError, DisconnectionError) as e:
            last_error = e
            logger.warning(f"get_db DB error (attempt {attempt + 1}): {type(e).__name__}: {e}")
            if _is_connection_error(e) and attempt < max_retries - 1:
                await reset_engine()
                await asyncio.sleep(retry_delay * (attempt + 1))
            else:
                break
        except Exception as e:
            last_error = e
            logger.warning(f"get_db other error (attempt {attempt + 1}): {type(e).__name__}: {e}")
            if _is_connection_error(e) and attempt < max_retries - 1:
                await reset_engine()
                await asyncio.sleep(retry_delay * (attempt + 1))
            else:
                break

    # All retries failed - raise HTTP exception
    logger.error(f"Database connection failed after {max_retries} attempts: {last_error}")
    raise HTTPException(
        status_code=503,
        detail=f"Database temporarily unavailable. Please try again in a few seconds."
    )


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database sessions with automatic retry."""
    max_retries = 3
    retry_delay = 1.0
    last_error = None

    for attempt in range(max_retries):
        factory = get_session_factory()
        try:
            async with factory() as session:
                # Test the connection first
                from sqlalchemy import text
                await session.execute(text("SELECT 1"))

                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
                return  # Success, exit the retry loop

        except (OperationalError, InterfaceError, DisconnectionError) as e:
            last_error = e
            if _is_connection_error(e) and attempt < max_retries - 1:
                logger.warning(
                    f"Connection error (attempt {attempt + 1}/{max_retries}): {e}"
                )
                await reset_engine()
                await asyncio.sleep(retry_delay * (attempt + 1))
            else:
                break
        except Exception as e:
            last_error = e
            if _is_connection_error(e) and attempt < max_retries - 1:
                logger.warning(
                    f"Connection error (attempt {attempt + 1}/{max_retries}): {e}"
                )
                await reset_engine()
                await asyncio.sleep(retry_delay * (attempt + 1))
            else:
                break

    # All retries failed
    logger.error(f"Database connection failed after {max_retries} attempts: {last_error}")
    raise RuntimeError(f"Database temporarily unavailable: {last_error}")


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
