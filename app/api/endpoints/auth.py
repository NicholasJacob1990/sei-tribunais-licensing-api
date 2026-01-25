"""
Authentication endpoints for Google OAuth
"""
from datetime import datetime
from hashlib import sha256
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.license import License, ProductType
from app.auth.google import oauth, get_google_auth_url, verify_state
from app.auth.jwt import (
    create_access_token,
    create_refresh_token,
    verify_token,
    TokenError,
)
from app.auth.dependencies import get_current_user, CurrentUser

router = APIRouter(prefix="/auth", tags=["authentication"])


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


@router.get("/google/login")
async def google_login(
    redirect_uri: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """
    Initiate Google OAuth login flow.

    Redirects the user to Google's authorization page.

    Args:
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

    # Store state in a cookie for verification on callback
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
) -> TokenResponse:
    """
    Handle Google OAuth callback.

    Exchanges the authorization code for tokens and creates/updates the user.

    Args:
        request: The incoming request
        code: Authorization code from Google
        state: State parameter for CSRF verification
        db: Database session

    Returns:
        JWT access and refresh tokens
    """
    # Verify state parameter
    stored_state = request.cookies.get("oauth_state")
    if not stored_state or stored_state != state or not verify_state(state):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid state parameter",
        )

    try:
        # Exchange code for token
        token = await oauth.google.authorize_access_token(request)

        if not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to get access token from Google",
            )

        # Get user info from token
        userinfo = token.get("userinfo")
        if not userinfo:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to get user info from Google",
            )

        google_id = userinfo.get("sub")
        email = userinfo.get("email")
        name = userinfo.get("name", email.split("@")[0])
        picture = userinfo.get("picture")

        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email not provided by Google",
            )

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
            user.last_login_at = datetime.utcnow()
        else:
            # Create new user
            user = User(
                google_id=google_id,
                email=email,
                name=name,
                picture=picture,
                last_login_at=datetime.utcnow(),
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
            detail=f"OAuth error: {str(e)}",
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
