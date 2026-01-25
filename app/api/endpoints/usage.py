"""
Usage tracking endpoints
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.license import ProductType
from app.services.license_service import LicenseService
from app.services.usage_service import UsageService

router = APIRouter(prefix="/usage", tags=["usage"])


class RecordUsageRequest(BaseModel):
    """Request body for recording usage."""
    email: EmailStr
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"]
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


@router.post("/record", response_model=UsageResponse)
async def record_usage(
    request: RecordUsageRequest,
    db: AsyncSession = Depends(get_db),
) -> UsageResponse:
    """
    Record an operation and check if within limits.

    Returns whether the operation is allowed and remaining quota.
    """
    license_service = LicenseService(db)
    usage_service = UsageService(db)

    try:
        product = ProductType(request.product)
    except ValueError:
        raise HTTPException(status_code=400, detail="Produto invalido")

    # Get license
    license = await license_service.get_by_email(request.email, product)

    if not license:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
            reason="Licenca nao encontrada. Inicie seu teste gratuito.",
        )

    if not license.is_active:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
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
        reason=result.get("reason"),
    )


class CheckUsageRequest(BaseModel):
    """Request body for checking usage."""
    email: EmailStr
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"]


@router.post("/check", response_model=UsageResponse)
async def check_usage(
    request: CheckUsageRequest,
    db: AsyncSession = Depends(get_db),
) -> UsageResponse:
    """
    Check current usage without recording.

    Useful for displaying remaining quota in the UI.
    """
    license_service = LicenseService(db)
    usage_service = UsageService(db)

    try:
        product = ProductType(request.product)
    except ValueError:
        raise HTTPException(status_code=400, detail="Produto invalido")

    license = await license_service.get_by_email(request.email, product)

    if not license:
        return UsageResponse(
            allowed=False,
            remaining=0,
            used_today=0,
            reason="Licenca nao encontrada",
        )

    result = await usage_service.check_limit(license.id)

    return UsageResponse(
        allowed=result["allowed"],
        remaining=result.get("remaining", 0),
        used_today=result.get("used_today", 0),
        limit=result.get("limit"),
        unlimited=result.get("unlimited", False),
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
