"""
Iudex Licensing API - Full Version

API de licenciamento para SEI-MCP e Tribunais-MCP.
Gerencia assinaturas, checkout Stripe, autenticacao Google OAuth.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import structlog

from app.config import settings
from app.database import init_db, close_db


# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("starting_app", version=settings.app_version, env=settings.environment)
    await init_db()
    yield
    # Shutdown
    logger.info("shutting_down_app")
    await close_db()


# Create app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API de licenciamento para extensoes Chrome SEI-MCP e Tribunais-MCP",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins + ["*"] if not settings.is_production else settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers - lazy import to avoid import-time errors
@app.on_event("startup")
async def include_routers():
    """Include routers after startup to ensure all dependencies are ready."""
    from app.api.endpoints import (
        auth_router,
        checkout_router,
        licenses_router,
        portal_router,
        usage_router,
        webhooks_router,
    )

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(checkout_router, prefix="/api/v1")
    app.include_router(licenses_router, prefix="/api/v1")
    app.include_router(portal_router, prefix="/api/v1")
    app.include_router(usage_router, prefix="/api/v1")
    app.include_router(webhooks_router, prefix="/api/v1")

    logger.info("routers_included")


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "version": settings.app_version,
        "environment": settings.environment,
    }


@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs" if not settings.is_production else "disabled",
        "endpoints": {
            "health": "/health",
            "auth": "/api/v1/auth",
            "licenses": "/api/v1/licenses",
            "checkout": "/api/v1/checkout",
            "webhooks": "/api/v1/webhooks",
        },
    }
