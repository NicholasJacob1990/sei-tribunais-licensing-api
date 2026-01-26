"""
Google OAuth configuration and helpers
"""
import secrets
from typing import Any

from authlib.integrations.starlette_client import OAuth
from itsdangerous import URLSafeTimedSerializer

from app.config import settings


# Lazy OAuth initialization
_oauth = None
_state_serializer = None


def get_oauth() -> OAuth:
    """Get or create OAuth client."""
    global _oauth
    if _oauth is None:
        _oauth = OAuth()
        _oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={
                "scope": "openid email profile",
            },
        )
    return _oauth


def get_state_serializer() -> URLSafeTimedSerializer:
    """Get or create state serializer."""
    global _state_serializer
    if _state_serializer is None:
        _state_serializer = URLSafeTimedSerializer(settings.session_secret_key)
    return _state_serializer


# Backwards compatibility - use the functions directly
# oauth and state_serializer are accessed via get_oauth() and get_state_serializer()


def generate_state() -> str:
    """
    Generate a secure state parameter for OAuth flow.

    The state parameter prevents CSRF attacks by ensuring
    the callback is from the same flow that was initiated.

    Returns:
        Signed state token
    """
    serializer = get_state_serializer()
    nonce = secrets.token_urlsafe(32)
    return serializer.dumps(nonce)


def verify_state(state: str, max_age: int = 600) -> bool:
    """
    Verify the state parameter from OAuth callback.

    Args:
        state: The state parameter to verify
        max_age: Maximum age in seconds (default: 10 minutes)

    Returns:
        True if state is valid, False otherwise
    """
    try:
        serializer = get_state_serializer()
        serializer.loads(state, max_age=max_age)
        return True
    except Exception:
        return False


def get_google_auth_url(redirect_uri: str | None = None) -> tuple[str, str]:
    """
    Generate Google OAuth authorization URL.

    Args:
        redirect_uri: Custom redirect URI (optional)

    Returns:
        Tuple of (authorization_url, state)
    """
    state = generate_state()

    # Build authorization URL manually
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={settings.google_client_id}"
        f"&redirect_uri={redirect_uri or settings.google_redirect_uri}"
        "&response_type=code"
        "&scope=openid%20email%20profile"
        f"&state={state}"
        "&access_type=offline"
        "&prompt=consent"
    )

    return auth_url, state


async def verify_google_token(token: dict[str, Any]) -> dict[str, Any] | None:
    """
    Verify and extract user info from Google OAuth token.

    Args:
        token: Token response from Google OAuth

    Returns:
        User info dict or None if invalid
    """
    if not token:
        return None

    # Extract user info from ID token
    userinfo = token.get("userinfo")
    if userinfo:
        return {
            "google_id": userinfo.get("sub"),
            "email": userinfo.get("email"),
            "name": userinfo.get("name"),
            "picture": userinfo.get("picture"),
            "email_verified": userinfo.get("email_verified", False),
        }

    return None


async def exchange_code_for_token(code: str, redirect_uri: str | None = None) -> dict[str, Any]:
    """
    Exchange authorization code for access token (manual implementation).

    Args:
        code: Authorization code from Google
        redirect_uri: Redirect URI used in the auth request

    Returns:
        Token response from Google

    Raises:
        Exception: If token exchange fails
    """
    import httpx

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri or settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data)
        if response.status_code != 200:
            raise Exception(f"Token exchange failed: {response.text}")
        return response.json()


async def get_google_user_info(access_token: str) -> dict[str, Any]:
    """
    Get user info from Google using access token.

    Args:
        access_token: Google access token

    Returns:
        User info dict from Google

    Raises:
        Exception: If fetching user info fails
    """
    import httpx

    userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code != 200:
            raise Exception(f"Failed to get user info: {response.text}")
        return response.json()
