"""
Iudex Licensing API - Full Version

API de licenciamento para SEI-MCP e Tribunais-MCP.
Gerencia assinaturas, checkout Stripe, autenticacao Google OAuth.
"""
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db, close_db

# Configure logging
logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info(f"Starting app version={settings.app_version} env={settings.environment}")
    try:
        await init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Database init error: {e}")
        # Continue anyway - health check will show status
    yield
    # Shutdown
    logger.info("Shutting down app")
    try:
        await close_db()
    except Exception as e:
        logger.error(f"Database close error: {e}")


# Create app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API de licenciamento para extensoes Chrome SEI-MCP e Tribunais-MCP",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS - allow all in dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers at module load time
try:
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
    logger.info("Routers included successfully")
except Exception as e:
    logger.error(f"Failed to include routers: {e}")


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
        "docs": "/docs",
        "endpoints": {
            "health": "/health",
            "auth": "/api/v1/auth",
            "licenses": "/api/v1/licenses",
            "checkout": "/api/v1/checkout",
            "webhooks": "/api/v1/webhooks",
        },
    }
