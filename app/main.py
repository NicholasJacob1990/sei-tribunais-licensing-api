"""
Iudex Licensing API - Main Application
FastAPI application for managing licenses and Stripe integration
"""
import os
import sys
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


# Print debug info
print(f"Python version: {sys.version}")
print(f"Environment: {os.environ.get('ENVIRONMENT', 'development')}")
print(f"DATABASE_URL set: {bool(os.environ.get('DATABASE_URL'))}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    print("Starting application...")

    # Lazy import to avoid import-time errors
    try:
        from app.config import settings
        print(f"Config loaded - environment: {settings.environment}")

        from app.database import init_db, close_db
        await init_db()
        print("Database initialized")
        app.state.db_healthy = True
    except Exception as e:
        print(f"Startup error: {e}")
        import traceback
        traceback.print_exc()
        app.state.db_healthy = False

    yield

    # Shutdown
    print("Shutting down...")
    try:
        from app.database import close_db
        await close_db()
    except Exception as e:
        print(f"Shutdown error: {e}")


# Create FastAPI application
app = FastAPI(
    title="Iudex Licensing API",
    version="1.0.0",
    description="API de licenciamento e cobranca para extensoes Iudex",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
        "version": "1.0.0",
        "database": "connected" if db_healthy else "disconnected",
    }


# API info endpoint
@app.get("/")
async def root() -> dict:
    """Root endpoint with API information."""
    return {
        "name": "Iudex Licensing API",
        "version": "1.0.0",
    }


# Lazy load routers to avoid import-time errors
@app.on_event("startup")
async def load_routers():
    """Load routers after startup to avoid import-time errors."""
    try:
        from app.api.endpoints import (
            checkout_router,
            licenses_router,
            webhooks_router,
            portal_router,
            usage_router,
            auth_router,
        )
        app.include_router(checkout_router, prefix="/api/v1")
        app.include_router(licenses_router, prefix="/api/v1")
        app.include_router(webhooks_router, prefix="/api/v1")
        app.include_router(portal_router, prefix="/api/v1")
        app.include_router(usage_router, prefix="/api/v1")
        app.include_router(auth_router, prefix="/api/v1")
        print("Routers loaded successfully")
    except Exception as e:
        print(f"Failed to load routers: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
