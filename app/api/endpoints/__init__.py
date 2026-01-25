"""
API endpoint modules

Routers are imported lazily by main.py to allow better error handling.
Import directly from the specific module when needed:
    from app.api.endpoints.auth import router as auth_router
"""
# Lazy imports - don't import at module level to prevent circular imports
# and allow main.py to handle errors individually
