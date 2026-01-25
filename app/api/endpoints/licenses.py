"""
License management endpoints
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.license import ProductType
from app.services.license_service import LicenseService

router = APIRouter(prefix="/licenses", tags=["licenses"])


class CheckLicenseRequest(BaseModel):
    """Request body for checking a license."""
    email: EmailStr
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"]


class LicenseInfo(BaseModel):
    """License information."""
    id: str
    email: str
    plan: str
    product: str
    status: str
    current_period_start: str
    current_period_end: str
    cancel_at_period_end: bool


class PlanLimits(BaseModel):
    """Plan limits."""
    operations_per_day: int
    users: int


class LicenseValidation(BaseModel):
    """License validation result."""
    valid: bool
    license: LicenseInfo | None = None
    plan: str | None = None
    limits: PlanLimits | None = None
    days_remaining: int | None = None
    message: str
    can_start_trial: bool = False


@router.post("/check", response_model=LicenseValidation)
async def check_license(
    request: CheckLicenseRequest,
    db: AsyncSession = Depends(get_db),
) -> LicenseValidation:
    """
    Check if a license is valid for the given email and product.

    Returns license details if valid, or instructions for getting a license.
    """
    try:
        product = ProductType(request.product)
        service = LicenseService(db)

        result = await service.validate(request.email, product)

        return LicenseValidation(
            valid=result["valid"],
            license=LicenseInfo(**result["license"]) if result.get("license") else None,
            plan=result.get("plan"),
            limits=PlanLimits(**result["limits"]) if result.get("limits") else None,
            days_remaining=result.get("days_remaining"),
            message=result["message"],
            can_start_trial=result.get("can_start_trial", False),
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao verificar licenca: {str(e)}")


class StartTrialRequest(BaseModel):
    """Request body for starting a trial."""
    email: EmailStr
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"]


class StartTrialResponse(BaseModel):
    """Response after starting a trial."""
    success: bool
    license: LicenseInfo
    message: str


@router.post("/trial/start", response_model=StartTrialResponse)
async def start_trial(
    request: StartTrialRequest,
    db: AsyncSession = Depends(get_db),
) -> StartTrialResponse:
    """
    Start a free trial for a product.

    Creates a new license with trial status valid for 7 days.
    """
    try:
        product = ProductType(request.product)
        service = LicenseService(db)

        # Check if already has a license
        existing = await service.get_by_email(request.email, product)
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Voce ja possui uma licenca para este produto.",
            )

        license = await service.create_trial(request.email, product)

        return StartTrialResponse(
            success=True,
            license=LicenseInfo(
                id=license.id,
                email=license.email,
                plan=license.plan.value,
                product=license.product.value,
                status=license.status.value,
                current_period_start=license.current_period_start.isoformat(),
                current_period_end=license.current_period_end.isoformat(),
                cancel_at_period_end=license.cancel_at_period_end,
            ),
            message=f"Teste gratuito iniciado! Voce tem 7 dias para experimentar.",
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao iniciar trial: {str(e)}")


@router.get("/status/{email}")
async def get_license_status(
    email: str,
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"] = "tribunais-mcp",
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get the current license status for an email.

    Useful for displaying status in the extension popup.
    """
    try:
        product_type = ProductType(product)
        service = LicenseService(db)

        license = await service.get_by_email(email, product_type)

        if not license:
            return {
                "has_license": False,
                "status": None,
                "plan": None,
                "message": "Nenhuma licenca encontrada",
            }

        return {
            "has_license": True,
            "status": license.status.value,
            "plan": license.plan.value,
            "is_active": license.is_active,
            "is_trial": license.is_trial,
            "days_remaining": license.days_remaining,
            "cancel_at_period_end": license.cancel_at_period_end,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
