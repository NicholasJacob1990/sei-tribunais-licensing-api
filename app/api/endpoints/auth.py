"""
Authentication endpoints for Google OAuth and Email/Password
"""
from datetime import datetime, timezone
from hashlib import sha256
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
import bcrypt
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.license import License, ProductType
from app.auth.google import get_oauth, get_google_auth_url, verify_state, exchange_code_for_token, get_google_user_info
from app.auth.jwt import (
    create_access_token,
    create_refresh_token,
    verify_token,
    TokenError,
)
from app.auth.dependencies import get_current_user, CurrentUser

router = APIRouter(prefix="/auth", tags=["authentication"])

# Password hashing functions using bcrypt directly


def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against hash."""
    password_bytes = password.encode('utf-8')
    hash_bytes = password_hash.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)


# ============================================================================
# Schemas
# ============================================================================


class TokenResponse(BaseModel):
    """Response containing access and refresh tokens."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshTokenRequest(BaseModel):
    """Request to refresh access token."""
    refresh_token: str


class LoginRequest(BaseModel):
    """Request for email/password login."""
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    """Request for email/password registration."""
    email: EmailStr
    password: str
    name: str | None = None


class GoogleCallbackRequest(BaseModel):
    """Request for Google OAuth callback from Chrome extension."""
    google_token: str
    email: EmailStr
    name: str | None = None
    picture: str | None = None


class UserResponse(BaseModel):
    """User information response."""
    id: str
    email: str
    name: str
    picture: str | None
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class UserWithLicensesResponse(BaseModel):
    """User information with associated licenses."""
    user: UserResponse
    licenses: list[dict]


# ============================================================================
# Endpoints
# ============================================================================


@router.post("/register", response_model=TokenResponse)
async def register_with_email(
    request: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Register a new user with email and password.

    Args:
        request: Registration request with email, password, and optional name
        db: Database session

    Returns:
        JWT access and refresh tokens
    """
    try:
        # Check if email already exists
        result = await db.execute(
            select(User).where(User.email == request.email)
        )
        existing_user = result.scalar_one_or_none()

        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

        # Validate password
        if len(request.password) < 6:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password must be at least 6 characters",
            )

        # Create user
        user = User(
            email=request.email,
            name=request.name or request.email.split("@")[0],
            password_hash=hash_password(request.password),
            last_login_at=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.flush()

        # Create tokens
        access_token = create_access_token({"sub": user.id, "email": user.email})
        refresh_token = create_refresh_token({"sub": user.id})

        # Store refresh token hash
        user.refresh_token_hash = sha256(refresh_token.encode()).hexdigest()
        await db.commit()

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Register error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration error: {str(e)}",
        )


@router.post("/login", response_model=TokenResponse)
async def login_with_email(
    request: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Login with email and password.

    Args:
        request: Login request with email and password
        db: Database session

    Returns:
        JWT access and refresh tokens
    """
    # Find user by email
    result = await db.execute(
        select(User).where(User.email == request.email)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Check if user has password (might be Google-only user)
    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account uses Google login. Please use 'Login with Google'.",
        )

    # Verify password
    if not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is disabled",
        )

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)

    # Create tokens
    access_token = create_access_token({"sub": user.id, "email": user.email})
    refresh_token = create_refresh_token({"sub": user.id})

    # Store refresh token hash
    user.refresh_token_hash = sha256(refresh_token.encode()).hexdigest()
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


@router.get("/google/login")
async def google_login(
    request: Request,
    redirect_uri: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """
    Initiate Google OAuth login flow.

    Redirects the user to Google's authorization page.

    Args:
        request: The incoming request (for session access)
        redirect_uri: Optional custom redirect URI after auth

    Returns:
        Redirect to Google OAuth authorization URL
    """
    if not settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured",
        )

    auth_url, state = get_google_auth_url(redirect_uri)

    # Store state in session for authlib verification AND in cookie as backup
    request.session["_state_google_"] = state

    response = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key="oauth_state",
        value=state,
        max_age=600,  # 10 minutes
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )

    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    db: AsyncSession = Depends(get_db),
):
    """
    Handle Google OAuth callback.

    Exchanges the authorization code for tokens and redirects to the app.

    Args:
        request: The incoming request
        code: Authorization code from Google
        state: State parameter for CSRF verification
        db: Database session

    Returns:
        Redirect to app with tokens in URL params
    """
    import logging
    logger = logging.getLogger(__name__)
    from urllib.parse import urlencode

    # Verify state parameter - check both cookie and session
    stored_state_cookie = request.cookies.get("oauth_state")
    stored_state_session = request.session.get("_state_google_")

    logger.info(f"OAuth callback - state from URL: {state[:20]}...")
    logger.info(f"OAuth callback - state from cookie: {stored_state_cookie[:20] if stored_state_cookie else 'None'}...")
    logger.info(f"OAuth callback - state from session: {stored_state_session[:20] if stored_state_session else 'None'}...")

    # Use session state if available (authlib needs it), fallback to cookie
    stored_state = stored_state_session or stored_state_cookie
    if not stored_state or stored_state != state or not verify_state(state):
        logger.error(f"OAuth state mismatch! URL state != stored state")
        return RedirectResponse(url="/?error=invalid_state")

    try:
        # Exchange code for token (manual implementation - no authlib dependency)
        token = await exchange_code_for_token(code)
        logger.info(f"Token exchange successful")

        if not token or "access_token" not in token:
            return RedirectResponse(url="/?error=no_token")

        # Get user info from Google
        userinfo = await get_google_user_info(token["access_token"])
        logger.info(f"Got user info for: {userinfo.get('email')}")

        if not userinfo:
            return RedirectResponse(url="/?error=no_userinfo")

        google_id = userinfo.get("id")  # Google userinfo uses 'id' not 'sub'
        email = userinfo.get("email")
        name = userinfo.get("name", email.split("@")[0] if email else "User")
        picture = userinfo.get("picture")

        if not email:
            return RedirectResponse(url="/?error=no_email")

        # Find or create user
        result = await db.execute(
            select(User).where(User.google_id == google_id)
        )
        user = result.scalar_one_or_none()

        if user:
            # Update existing user
            user.email = email
            user.name = name
            user.picture = picture
            user.last_login_at = datetime.now(timezone.utc)
        else:
            # Create new user
            user = User(
                google_id=google_id,
                email=email,
                name=name,
                picture=picture,
                last_login_at=datetime.now(timezone.utc),
            )
            db.add(user)

        await db.flush()

        # Create tokens
        access_token = create_access_token({"sub": user.id, "email": user.email})
        refresh_token = create_refresh_token({"sub": user.id})

        # Store refresh token hash
        user.refresh_token_hash = sha256(refresh_token.encode()).hexdigest()
        await db.commit()

        # Redirect to app with tokens
        params = urlencode({
            "access_token": access_token,
            "refresh_token": refresh_token,
        })
        return RedirectResponse(url=f"/?{params}")

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"OAuth callback error: {e}")
        return RedirectResponse(url=f"/?error=oauth_error")


@router.post("/google/callback", response_model=TokenResponse)
async def google_callback_post(
    request: GoogleCallbackRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Handle Google OAuth callback from Chrome extension.

    The extension gets the Google token directly and sends it here
    along with user info fetched from Google.

    Args:
        request: Google OAuth data from extension
        db: Database session

    Returns:
        JWT access and refresh tokens
    """
    try:
        # Verify token with Google
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {request.google_token}"},
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid Google token",
                )

            google_data = response.json()
            google_id = google_data.get("id")
            verified_email = google_data.get("email")

            if not google_id or verified_email != request.email:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token email mismatch",
                )

        # Find or create user
        result = await db.execute(
            select(User).where(
                or_(User.google_id == google_id, User.email == request.email)
            )
        )
        user = result.scalar_one_or_none()

        if user:
            # Update existing user
            user.google_id = google_id
            user.name = request.name or user.name
            user.picture = request.picture or user.picture
            user.last_login_at = datetime.now(timezone.utc)
        else:
            # Create new user
            user = User(
                google_id=google_id,
                email=request.email,
                name=request.name or request.email.split("@")[0],
                picture=request.picture,
                last_login_at=datetime.now(timezone.utc),
            )
            db.add(user)

        await db.flush()

        # Create tokens
        access_token = create_access_token({"sub": user.id, "email": user.email})
        refresh_token = create_refresh_token({"sub": user.id})

        # Store refresh token hash
        user.refresh_token_hash = sha256(refresh_token.encode()).hexdigest()
        await db.commit()

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Google auth error: {str(e)}",
        )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Refresh access token using refresh token.

    Args:
        request: Refresh token request body
        db: Database session

    Returns:
        New access and refresh tokens
    """
    try:
        # Verify refresh token
        payload = verify_token(request.refresh_token, expected_type="refresh")
        user_id = payload.get("sub")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
            )

        # Fetch user
        result = await db.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is disabled",
            )

        # Verify refresh token hash (token rotation)
        token_hash = sha256(request.refresh_token.encode()).hexdigest()
        if user.refresh_token_hash != token_hash:
            # Token was already rotated or revoked
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
            )

        # Create new tokens
        access_token = create_access_token({"sub": user.id, "email": user.email})
        new_refresh_token = create_refresh_token({"sub": user.id})

        # Rotate refresh token
        user.refresh_token_hash = sha256(new_refresh_token.encode()).hexdigest()
        await db.commit()

        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        )

    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


@router.get("/me", response_model=UserWithLicensesResponse)
async def get_current_user_info(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserWithLicensesResponse:
    """
    Get current authenticated user information and licenses.

    Args:
        current_user: The authenticated user (injected)
        db: Database session

    Returns:
        User info with associated licenses
    """
    # Fetch user's licenses
    result = await db.execute(
        select(License).where(License.email == current_user.email)
    )
    licenses = result.scalars().all()

    license_list = [
        {
            "id": license.id,
            "product": license.product.value,
            "plan": license.plan.value,
            "status": license.status.value,
            "is_active": license.is_active,
            "current_period_end": license.current_period_end.isoformat(),
            "days_remaining": license.days_remaining,
        }
        for license in licenses
    ]

    return UserWithLicensesResponse(
        user=UserResponse(
            id=current_user.id,
            email=current_user.email,
            name=current_user.name,
            picture=current_user.picture,
            is_active=current_user.is_active,
            created_at=current_user.created_at,
            last_login_at=current_user.last_login_at,
        ),
        licenses=license_list,
    )


@router.post("/logout")
async def logout(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Logout the current user by invalidating refresh token.

    Args:
        current_user: The authenticated user (injected)
        db: Database session

    Returns:
        Success message
    """
    # Invalidate refresh token
    current_user.refresh_token_hash = None
    await db.commit()

    return {"message": "Logged out successfully"}


# ============================================================================
# API Token Management (for MCP clients)
# ============================================================================


class ApiTokenResponse(BaseModel):
    """Response containing API token."""
    api_token: str
    created_at: datetime
    message: str


class ValidateTokenRequest(BaseModel):
    """Request to validate an API token."""
    token: str


class ValidateTokenResponse(BaseModel):
    """Response from token validation."""
    valid: bool
    user_id: str | None = None
    email: str | None = None
    reason: str | None = None


@router.post("/api-token/generate", response_model=ApiTokenResponse)
async def generate_api_token(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApiTokenResponse:
    """
    Generate a new API token for MCP clients.

    This token is long-lived and can be used with Bearer auth.
    Generating a new token invalidates the previous one.

    Args:
        current_user: The authenticated user (injected)
        db: Database session

    Returns:
        The new API token (shown only once)
    """
    import secrets

    # Generate a secure random token
    api_token = f"sei_{secrets.token_hex(32)}"

    # Store hash of the token
    current_user.api_token_hash = sha256(api_token.encode()).hexdigest()
    current_user.api_token_created_at = datetime.now(timezone.utc)
    await db.commit()

    return ApiTokenResponse(
        api_token=api_token,
        created_at=current_user.api_token_created_at,
        message="Token gerado com sucesso. Guarde-o em local seguro - ele não será mostrado novamente.",
    )


@router.post("/api-token/revoke")
async def revoke_api_token(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Revoke the current API token.

    Args:
        current_user: The authenticated user (injected)
        db: Database session

    Returns:
        Success message
    """
    current_user.api_token_hash = None
    current_user.api_token_created_at = None
    await db.commit()

    return {"message": "API token revogado com sucesso"}


@router.post("/api-token/validate", response_model=ValidateTokenResponse)
async def validate_api_token(
    request: ValidateTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> ValidateTokenResponse:
    """
    Validate an API token.

    This endpoint can be called by external services (like sei-mcp)
    to verify a Bearer token and get user info.

    Args:
        request: Token to validate
        db: Database session

    Returns:
        Validation result with user info if valid
    """
    if not request.token:
        return ValidateTokenResponse(valid=False, reason="Token vazio")

    # Hash the provided token
    token_hash = sha256(request.token.encode()).hexdigest()

    # Find user by token hash
    result = await db.execute(
        select(User).where(User.api_token_hash == token_hash)
    )
    user = result.scalar_one_or_none()

    if not user:
        return ValidateTokenResponse(valid=False, reason="Token invalido")

    if not user.is_active:
        return ValidateTokenResponse(valid=False, reason="Conta desativada")

    return ValidateTokenResponse(
        valid=True,
        user_id=user.id,
        email=user.email,
    )
