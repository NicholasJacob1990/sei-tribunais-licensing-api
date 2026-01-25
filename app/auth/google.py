"""
Google OAuth configuration and helpers
"""
import secrets
from typing import Any

from authlib.integrations.starlette_client import OAuth
from itsdangerous import URLSafeTimedSerializer

from app.config import settings


# OAuth client configuration
oauth = OAuth()

# Register Google OAuth provider
oauth.register(
    name="google",
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": "openid email profile",
    },
)


# State serializer for CSRF protection
state_serializer = URLSafeTimedSerializer(settings.session_secret_key)


def generate_state() -> str:
    """
    Generate a secure state parameter for OAuth flow.

    The state parameter prevents CSRF attacks by ensuring
    the callback is from the same flow that was initiated.

    Returns:
        Signed state token
    """
    nonce = secrets.token_urlsafe(32)
    return state_serializer.dumps(nonce)


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
        state_serializer.loads(state, max_age=max_age)
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
