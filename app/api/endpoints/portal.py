"""
Customer portal endpoints for subscription management
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.license import ProductType
from app.services.license_service import LicenseService
from app.services.stripe_service import StripeService
from app.config import settings

router = APIRouter(prefix="/portal", tags=["portal"])


class CreatePortalRequest(BaseModel):
    """Request body for creating a portal session."""
    email: EmailStr
    return_url: str | None = None


class PortalResponse(BaseModel):
    """Response with portal session URL."""
    url: str


@router.post("/create", response_model=PortalResponse)
async def create_portal_session(
    request: CreatePortalRequest,
    db: AsyncSession = Depends(get_db),
) -> PortalResponse:
    """
    Create a Stripe Customer Portal session.

    The portal allows customers to:
    - Update payment methods
    - View invoices
    - Cancel subscription
    - Change plans
    """
    service = LicenseService(db)

    # Find license with Stripe customer ID
    # Try each product type
    license = None
    for product in ProductType:
        license = await service.get_by_email(request.email, product)
        if license and license.stripe_customer_id:
            break

    if not license or not license.stripe_customer_id:
        raise HTTPException(
            status_code=404,
            detail="Nenhuma assinatura encontrada para este email.",
        )

    try:
        return_url = request.return_url or f"{settings.frontend_url}/account"

        session = await StripeService.create_portal_session(
            customer_id=license.stripe_customer_id,
            return_url=return_url,
        )

        return PortalResponse(url=session.url)

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao criar portal: {str(e)}",
        )


class CancelSubscriptionRequest(BaseModel):
    """Request body for canceling a subscription."""
    email: EmailStr
    product: str = "tribunais-mcp"
    cancel_immediately: bool = False


class CancelResponse(BaseModel):
    """Response after cancellation."""
    success: bool
    message: str
    cancel_at: str | None = None


@router.post("/cancel", response_model=CancelResponse)
async def cancel_subscription(
    request: CancelSubscriptionRequest,
    db: AsyncSession = Depends(get_db),
) -> CancelResponse:
    """
    Cancel a subscription.

    By default, cancels at the end of the current billing period.
    Set cancel_immediately=True to cancel right away.
    """
    service = LicenseService(db)

    try:
        product = ProductType(request.product)
    except ValueError:
        raise HTTPException(status_code=400, detail="Produto invalido")

    license = await service.get_by_email(request.email, product)

    if not license or not license.stripe_subscription_id:
        raise HTTPException(
            status_code=404,
            detail="Assinatura nao encontrada.",
        )

    try:
        subscription = await StripeService.cancel_subscription(
            subscription_id=license.stripe_subscription_id,
            at_period_end=not request.cancel_immediately,
        )

        if request.cancel_immediately:
            message = "Assinatura cancelada imediatamente."
            cancel_at = None
        else:
            message = "Assinatura sera cancelada ao final do periodo."
            cancel_at = license.current_period_end.isoformat()

        return CancelResponse(
            success=True,
            message=message,
            cancel_at=cancel_at,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao cancelar: {str(e)}",
        )


class ReactivateRequest(BaseModel):
    """Request body for reactivating a subscription."""
    email: EmailStr
    product: str = "tribunais-mcp"


@router.post("/reactivate", response_model=CancelResponse)
async def reactivate_subscription(
    request: ReactivateRequest,
    db: AsyncSession = Depends(get_db),
) -> CancelResponse:
    """
    Reactivate a subscription that was set to cancel.

    Only works if the subscription hasn't actually been canceled yet.
    """
    service = LicenseService(db)

    try:
        product = ProductType(request.product)
    except ValueError:
        raise HTTPException(status_code=400, detail="Produto invalido")

    license = await service.get_by_email(request.email, product)

    if not license or not license.stripe_subscription_id:
        raise HTTPException(
            status_code=404,
            detail="Assinatura nao encontrada.",
        )

    if not license.cancel_at_period_end:
        raise HTTPException(
            status_code=400,
            detail="Assinatura nao esta marcada para cancelamento.",
        )

    try:
        import stripe
        subscription = stripe.Subscription.modify(
            license.stripe_subscription_id,
            cancel_at_period_end=False,
        )

        license.cancel_at_period_end = False
        license.canceled_at = None

        return CancelResponse(
            success=True,
            message="Assinatura reativada com sucesso!",
            cancel_at=None,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao reativar: {str(e)}",
        )
