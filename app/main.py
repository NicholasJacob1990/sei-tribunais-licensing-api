"""
Iudex Licensing API - Main Application
FastAPI application for managing licenses and Stripe integration
"""
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import structlog
import logging

from app.config import settings
from app.database import init_db, close_db
from app.api.endpoints import (
    checkout_router,
    licenses_router,
    webhooks_router,
    portal_router,
    usage_router,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Simple structlog config
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
    logger.info("application_starting", environment=settings.environment)
    try:
        await init_db()
        logger.info("database_initialized")
        app.state.db_healthy = True
    except Exception as e:
        logger.error("database_initialization_failed", error=str(e))
        app.state.db_healthy = False
        # Continue startup even if DB fails - allows health checks

    yield

    # Shutdown
    logger.info("application_shutting_down")
    try:
        await close_db()
        logger.info("database_closed")
    except Exception as e:
        logger.error("database_close_failed", error=str(e))


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API de licenciamento e cobranca para extensoes Iudex",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    openapi_url="/openapi.json" if not settings.is_production else None,
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check endpoint
@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    db_healthy = getattr(app.state, "db_healthy", False)
    return {
        "status": "healthy" if db_healthy else "degraded",
        "version": settings.app_version,
        "environment": settings.environment,
        "database": "connected" if db_healthy else "disconnected",
    }


# API info endpoint
@app.get("/")
async def root() -> dict:
    """Root endpoint with API information."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs" if not settings.is_production else None,
    }


# Include routers
app.include_router(checkout_router, prefix="/api/v1")
app.include_router(licenses_router, prefix="/api/v1")
app.include_router(webhooks_router, prefix="/api/v1")
app.include_router(portal_router, prefix="/api/v1")
app.include_router(usage_router, prefix="/api/v1")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=not settings.is_production,
    )
