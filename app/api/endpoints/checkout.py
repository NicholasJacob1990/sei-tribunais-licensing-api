"""
Checkout endpoints for creating Stripe payment sessions
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.license import PlanId, ProductType
from app.services.stripe_service import StripeService

router = APIRouter(prefix="/checkout", tags=["checkout"])


class CreateCheckoutRequest(BaseModel):
    """Request body for creating a checkout session."""
    email: EmailStr
    product: Literal["sei-mcp", "tribunais-mcp", "bundle"]
    plan: Literal["professional", "office"]
    interval: Literal["month", "year"] = "month"
    success_url: str
    cancel_url: str
    client_reference_id: str | None = None


class CheckoutResponse(BaseModel):
    """Response with checkout session details."""
    checkout_url: str
    session_id: str


@router.post("/create", response_model=CheckoutResponse)
async def create_checkout_session(
    request: CreateCheckoutRequest,
    db: AsyncSession = Depends(get_db),
) -> CheckoutResponse:
    """
    Create a Stripe Checkout session for subscription purchase.

    Returns a URL to redirect the user to complete payment.
    """
    try:
        product = ProductType(request.product)
        plan = PlanId(request.plan)

        session = await StripeService.create_checkout_session(
            email=request.email,
            product=product,
            plan=plan,
            interval=request.interval,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            client_reference_id=request.client_reference_id,
        )

        return CheckoutResponse(
            checkout_url=session.url,
            session_id=session.id,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao criar sessao: {str(e)}")


class PlanInfo(BaseModel):
    """Plan information for display."""
    id: str
    name: str
    description: str
    features: list[str]
    price_monthly: int
    price_yearly: int
    currency: str = "BRL"


@router.get("/plans", response_model=list[PlanInfo])
async def get_plans() -> list[PlanInfo]:
    """Get available subscription plans."""
    return [
        PlanInfo(
            id="professional",
            name="Profissional",
            description="Para advogados e profissionais individuais",
            features=[
                "Operacoes ilimitadas",
                "Suporte por email (24h)",
                "Atualizacoes automaticas",
                "Integracao com IA (Claude/GPT)",
                "Historico de 90 dias",
            ],
            price_monthly=9700,
            price_yearly=97000,
        ),
        PlanInfo(
            id="office",
            name="Escritorio",
            description="Para escritorios com multiplos usuarios",
            features=[
                "Tudo do Profissional",
                "Ate 5 usuarios incluidos",
                "Dashboard de uso",
                "API dedicada",
                "Suporte prioritario (4h)",
                "Relatorios de produtividade",
            ],
            price_monthly=29700,
            price_yearly=297000,
        ),
    ]
