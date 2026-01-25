"""
Iudex Licensing API - Full Version

API de licenciamento para SEI-MCP e Tribunais-MCP.
Gerencia assinaturas, checkout Stripe, autenticacao Google OAuth.
"""
from contextlib import asynccontextmanager
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db, close_db

# Configure standard logging
logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Configure structlog (used by other modules)
try:
    import structlog
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
    logger.info("structlog configured")
except ImportError:
    logger.warning("structlog not available")


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
# Import each router individually to identify which one fails
import traceback

_routers_loaded = False

def load_routers():
    global _routers_loaded
    if _routers_loaded:
        return

    try:
        logger.info("Loading auth_router...")
        from app.api.endpoints.auth import router as auth_router
        app.include_router(auth_router, prefix="/api/v1")
        logger.info("auth_router loaded")
    except Exception as e:
        logger.error(f"auth_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading checkout_router...")
        from app.api.endpoints.checkout import router as checkout_router
        app.include_router(checkout_router, prefix="/api/v1")
        logger.info("checkout_router loaded")
    except Exception as e:
        logger.error(f"checkout_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading licenses_router...")
        from app.api.endpoints.licenses import router as licenses_router
        app.include_router(licenses_router, prefix="/api/v1")
        logger.info("licenses_router loaded")
    except Exception as e:
        logger.error(f"licenses_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading portal_router...")
        from app.api.endpoints.portal import router as portal_router
        app.include_router(portal_router, prefix="/api/v1")
        logger.info("portal_router loaded")
    except Exception as e:
        logger.error(f"portal_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading usage_router...")
        from app.api.endpoints.usage import router as usage_router
        app.include_router(usage_router, prefix="/api/v1")
        logger.info("usage_router loaded")
    except Exception as e:
        logger.error(f"usage_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading webhooks_router...")
        from app.api.endpoints.webhooks import router as webhooks_router
        app.include_router(webhooks_router, prefix="/api/v1")
        logger.info("webhooks_router loaded")
    except Exception as e:
        logger.error(f"webhooks_router error: {e}\n{traceback.format_exc()}")

    _routers_loaded = True
    logger.info("All routers processed")

# Load routers
load_routers()


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
    # List actually loaded routes
    loaded_routes = [route.path for route in app.routes if hasattr(route, 'path')]
    api_routes = [r for r in loaded_routes if r.startswith('/api/')]

    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "loaded_api_routes": api_routes,
        "endpoints": {
            "health": "/health",
            "auth": "/api/v1/auth",
            "licenses": "/api/v1/licenses",
            "checkout": "/api/v1/checkout",
            "webhooks": "/api/v1/webhooks",
        },
    }
