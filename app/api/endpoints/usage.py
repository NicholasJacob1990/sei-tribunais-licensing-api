"""
Usage tracking endpoints

Supports two authentication methods:
1. Bearer token (API token) in Authorization header - for MCP clients
2. Email in request body - for legacy/extension use
"""
from hashlib import sha256
from typing import Literal, Annotated

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.license import ProductType
from app.models.user import User
from app.services.license_service import LicenseService
from app.services.usage_service import UsageService

router = APIRouter(prefix="/usage", tags=["usage"])


async def get_email_from_token(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> str | None:
    """Extract email from Bearer token if provided."""
    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization[7:]  # Remove "Bearer " prefix
    token_hash = sha256(token.encode()).hexdigest()

    result = await db.execute(
        select(User).where(User.api_token_hash == token_hash)
    )
    user = result.scalar_one_or_none()

    if user and user.is_active:
        return user.email
    return None


class RecordUsageRequest(BaseModel):
    """Request body for recording usage."""
    email: EmailStr | None = None  # Optional if using Bearer token
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"] = "sei-mcp"
    operation_type: str | None = None
    count: int = 1


class UsageResponse(BaseModel):
    """Response with usage information."""
    allowed: bool
    remaining: int
    used_today: int
    limit: int | None = None
    unlimited: bool = False
    reason: str | None = None
    email: str | None = None  # Return email for confirmation


@router.post("/record", response_model=UsageResponse)
async def record_usage(
    request: RecordUsageRequest,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> UsageResponse:
    """
    Record an operation and check if within limits.

    Authentication:
    - Bearer token in Authorization header (preferred for MCP clients)
    - OR email in request body (legacy/extension)

    Returns whether the operation is allowed and remaining quota.
    """
    license_service = LicenseService(db)
    usage_service = UsageService(db)

    # Get email from token or request body
    email = await get_email_from_token(authorization, db)
    if not email:
        email = request.email

    if not email:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
            reason="Autenticacao necessaria. Forneca Bearer token ou email.",
        )

    try:
        product = ProductType(request.product)
    except ValueError:
        raise HTTPException(status_code=400, detail="Produto invalido")

    # Get license
    license = await license_service.get_by_email(email, product)

    if not license:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
            email=email,
            reason="Licenca nao encontrada. Inicie seu teste gratuito.",
        )

    if not license.is_active:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
            email=email,
            reason="Licenca inativa. Renove sua assinatura.",
        )

    # Record usage
    result = await usage_service.record_operation(
        license_id=license.id,
        product=request.product,
        operation_type=request.operation_type,
        count=request.count,
    )

    return UsageResponse(
        allowed=result["allowed"],
        remaining=result.get("remaining", 0),
        used_today=result.get("used_today", 0),
        limit=result.get("limit"),
        unlimited=result.get("remaining") == -1,
        email=email,
        reason=result.get("reason"),
    )


class CheckUsageRequest(BaseModel):
    """Request body for checking usage."""
    email: EmailStr | None = None  # Optional if using Bearer token
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"] = "sei-mcp"


@router.post("/check", response_model=UsageResponse)
async def check_usage(
    request: CheckUsageRequest,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> UsageResponse:
    """
    Check current usage without recording.

    Authentication:
    - Bearer token in Authorization header (preferred)
    - OR email in request body (legacy)

    Useful for displaying remaining quota in the UI.
    """
    license_service = LicenseService(db)
    usage_service = UsageService(db)

    # Get email from token or request body
    email = await get_email_from_token(authorization, db)
    if not email:
        email = request.email

    if not email:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
            reason="Autenticacao necessaria",
        )

    try:
        product = ProductType(request.product)
    except ValueError:
        raise HTTPException(status_code=400, detail="Produto invalido")

    license = await license_service.get_by_email(email, product)

    if not license:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
            email=email,
            reason="Licenca nao encontrada",
        )

    result = await usage_service.check_limit(license.id)

    return UsageResponse(
        allowed=result["allowed"],
        remaining=result.get("remaining", 0),
        used_today=result.get("used_today", 0),
        limit=result.get("limit"),
        unlimited=result.get("unlimited", False),
        email=email,
        reason=result.get("reason"),
    )


class UsageStatsRequest(BaseModel):
    """Request body for usage stats."""
    email: EmailStr
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"]
    days: int = 30


class DailyUsage(BaseModel):
    """Daily usage record."""
    date: str
    total: int
    search: int
    download: int
    automation: int


class UsageStatsResponse(BaseModel):
    """Response with usage statistics."""
    total_operations: int
    daily_usage: list[DailyUsage]


@router.post("/stats", response_model=UsageStatsResponse)
async def get_usage_stats(
    request: UsageStatsRequest,
    db: AsyncSession = Depends(get_db),
) -> UsageStatsResponse:
    """
    Get usage statistics for the specified period.

    Returns daily breakdown of operations.
    """
    license_service = LicenseService(db)
    usage_service = UsageService(db)

    try:
        product = ProductType(request.product)
    except ValueError:
        raise HTTPException(status_code=400, detail="Produto invalido")

    license = await license_service.get_by_email(request.email, product)

    if not license:
        raise HTTPException(status_code=404, detail="Licenca nao encontrada")

    stats = await usage_service.get_usage_stats(
        license_id=license.id,
        days=request.days,
    )

    total = await usage_service.get_total_usage(license.id)

    return UsageStatsResponse(
        total_operations=total,
        daily_usage=[DailyUsage(**s) for s in stats],
    )
