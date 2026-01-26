"""
Iudex Licensing API - Full Version

API de licenciamento para SEI-MCP e Tribunais-MCP.
Gerencia assinaturas, checkout Stripe, autenticacao Google OAuth.
"""
from contextlib import asynccontextmanager
import logging
import sys

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings

# Static files directory
STATIC_DIR = Path(__file__).parent / "static"
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
_router_errors = []

def load_routers():
    global _routers_loaded, _router_errors
    if _routers_loaded:
        return

    try:
        logger.info("Loading auth_router...")
        from app.api.endpoints.auth import router as auth_router
        app.include_router(auth_router, prefix="/api/v1")
        logger.info("auth_router loaded")
    except Exception as e:
        err = f"auth_router: {e}"
        _router_errors.append(err)
        logger.error(f"auth_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading checkout_router...")
        from app.api.endpoints.checkout import router as checkout_router
        app.include_router(checkout_router, prefix="/api/v1")
        logger.info("checkout_router loaded")
    except Exception as e:
        err = f"checkout_router: {e}"
        _router_errors.append(err)
        logger.error(f"checkout_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading licenses_router...")
        from app.api.endpoints.licenses import router as licenses_router
        app.include_router(licenses_router, prefix="/api/v1")
        logger.info("licenses_router loaded")
    except Exception as e:
        err = f"licenses_router: {e}"
        _router_errors.append(err)
        logger.error(f"licenses_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading portal_router...")
        from app.api.endpoints.portal import router as portal_router
        app.include_router(portal_router, prefix="/api/v1")
        logger.info("portal_router loaded")
    except Exception as e:
        err = f"portal_router: {e}"
        _router_errors.append(err)
        logger.error(f"portal_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading usage_router...")
        from app.api.endpoints.usage import router as usage_router
        app.include_router(usage_router, prefix="/api/v1")
        logger.info("usage_router loaded")
    except Exception as e:
        err = f"usage_router: {e}"
        _router_errors.append(err)
        logger.error(f"usage_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading webhooks_router...")
        from app.api.endpoints.webhooks import router as webhooks_router
        app.include_router(webhooks_router, prefix="/api/v1")
        logger.info("webhooks_router loaded")
    except Exception as e:
        err = f"webhooks_router: {e}"
        _router_errors.append(err)
        logger.error(f"webhooks_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading mcp_websocket_router...")
        from app.api.endpoints.mcp_websocket import router as mcp_websocket_router
        app.include_router(mcp_websocket_router)
        logger.info("mcp_websocket_router loaded")
    except Exception as e:
        err = f"mcp_websocket_router: {e}"
        _router_errors.append(err)
        logger.error(f"mcp_websocket_router error: {e}\n{traceback.format_exc()}")

    try:
        logger.info("Loading mcp_server_router...")
        from app.api.endpoints.mcp_server import router as mcp_server_router
        app.include_router(mcp_server_router)
        logger.info("mcp_server_router loaded")
    except Exception as e:
        err = f"mcp_server_router: {e}"
        _router_errors.append(err)
        logger.error(f"mcp_server_router error: {e}\n{traceback.format_exc()}")

    _routers_loaded = True
    logger.info(f"All routers processed. Errors: {len(_router_errors)}")

# Load routers
load_routers()

# Mount static files (after routers to not override API routes)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    logger.info(f"Static files mounted from {STATIC_DIR}")


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "version": settings.app_version,
        "environment": settings.environment,
    }


@app.get("/debug/get-db-test")
async def debug_get_db_test():
    """Debug endpoint to test get_db dependency directly."""
    from sqlalchemy import text
    from app.database import get_db, get_session_factory
    from app.config import settings as cfg

    results = {
        "database_url_masked": cfg.async_database_url[:50] + "..." if len(cfg.async_database_url) > 50 else cfg.async_database_url,
        "environment": cfg.environment,
        "is_production": cfg.is_production,
        "tests": [],
    }

    # Test 1: Direct session factory (this works)
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
            results["tests"].append({"name": "direct_factory", "status": "OK"})
    except Exception as e:
        results["tests"].append({"name": "direct_factory", "status": "ERROR", "error": str(e)})

    # Test 2: get_db generator manually
    try:
        gen = get_db()
        session = await gen.__anext__()
        await session.execute(text("SELECT 1"))
        results["tests"].append({"name": "get_db_manual", "status": "OK"})
        # Close the generator properly
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
    except Exception as e:
        results["tests"].append({"name": "get_db_manual", "status": "ERROR", "error": str(e)})

    return results


@app.get("/debug/routers")
async def debug_routers():
    """Debug endpoint to check router loading errors."""
    loaded_routes = [route.path for route in app.routes if hasattr(route, 'path')]
    api_routes = [r for r in loaded_routes if r.startswith('/api/')]
    return {
        "loaded_api_routes": api_routes,
        "router_errors": _router_errors,
        "total_errors": len(_router_errors),
    }


@app.get("/debug/db-schema")
async def debug_db_schema():
    """Debug endpoint to check database schema."""
    from sqlalchemy import text
    from app.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        try:
            # Check columns in users table
            result = await session.execute(text("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'users'
                ORDER BY ordinal_position
            """))
            columns = [{"name": r[0], "type": r[1], "nullable": r[2]} for r in result.fetchall()]

            # Check if api_token_hash column exists
            has_api_token = any(c["name"] == "api_token_hash" for c in columns)

            return {
                "columns": columns,
                "has_api_token_hash": has_api_token,
                "column_count": len(columns),
            }
        except Exception as e:
            return {"error": str(e)}


@app.post("/debug/run-migration")
async def run_migration_manually():
    """Debug endpoint to manually run migrations."""
    from sqlalchemy import text
    from app.database import get_session_factory

    factory = get_session_factory()
    results = []

    async with factory() as session:
        try:
            # Check and add api_token_hash column
            result = await session.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'api_token_hash'
            """))
            if not result.fetchone():
                await session.execute(text(
                    "ALTER TABLE users ADD COLUMN api_token_hash VARCHAR(255)"
                ))
                results.append("Added api_token_hash column")

            # Check and add api_token_created_at column
            result = await session.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'api_token_created_at'
            """))
            if not result.fetchone():
                await session.execute(text(
                    "ALTER TABLE users ADD COLUMN api_token_created_at TIMESTAMP WITH TIME ZONE"
                ))
                results.append("Added api_token_created_at column")

            # Create index
            try:
                await session.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_users_api_token_hash ON users(api_token_hash)"
                ))
                results.append("Index created/verified")
            except Exception as e:
                results.append(f"Index: {e}")

            await session.commit()

            if not results:
                results.append("All columns already exist")

            return {"success": True, "results": results}
        except Exception as e:
            return {"success": False, "error": str(e)}


@app.get("/")
async def root():
    """Serve the registration/login page."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)

    # Fallback to JSON if no static file
    loaded_routes = [route.path for route in app.routes if hasattr(route, 'path')]
    api_routes = [r for r in loaded_routes if r.startswith('/api/')]

    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "register": "/register",
        "loaded_api_routes": api_routes,
    }


@app.get("/register")
async def register_page():
    """Serve the registration page."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"error": "Registration page not found", "docs": "/docs"}


@app.get("/api")
async def api_info():
    """API info endpoint."""
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
