"""
Checkout endpoints for creating Stripe payment sessions

Endpoints:
- POST /checkout/create - Criar sessao de checkout
- POST /checkout/free - Registrar plano gratuito
- GET /checkout/plans - Listar planos disponiveis
- GET /checkout/session/{session_id} - Obter detalhes da sessao
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
import stripe

import structlog

from app.database import get_db
from app.models.license import PlanId, ProductType, LicenseStatus
from app.services.stripe_service import StripeService, PLAN_CONFIGS
from app.services.license_service import LicenseService
from app.config import settings

logger = structlog.get_logger()

router = APIRouter(prefix="/checkout", tags=["checkout"])


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class CreateCheckoutRequest(BaseModel):
    """Request body for creating a checkout session."""
    email: EmailStr = Field(..., description="Email do cliente")
    plan: Literal["starter", "pro", "professional", "enterprise"] = Field(
        ...,
        description="Plano: starter/pro (R$29.90/mes) ou professional/enterprise (R$99.90/mes)"
    )
    interval: Literal["monthly", "yearly"] = Field(
        default="monthly",
        description="Intervalo de cobranca"
    )
    success_url: str | None = Field(
        default=None,
        description="URL de redirecionamento apos sucesso"
    )
    cancel_url: str | None = Field(
        default=None,
        description="URL de redirecionamento se cancelar"
    )
    client_reference_id: str | None = Field(
        default=None,
        description="ID de referencia do seu sistema"
    )
    customer_name: str | None = Field(
        default=None,
        description="Nome do cliente"
    )
    # Compatibilidade com produtos existentes
    product: Literal["sei-mcp", "tribunais-mcp", "bundle", "default"] | None = Field(
        default=None,
        description="Produto (opcional, para multiplos produtos)"
    )


class CheckoutResponse(BaseModel):
    """Response with checkout session details."""
    checkout_url: str = Field(..., description="URL para redirecionar o cliente")
    session_id: str = Field(..., description="ID da sessao do Stripe")
    expires_at: int | None = Field(default=None, description="Timestamp de expiracao")


class RegisterFreeRequest(BaseModel):
    """Request body for registering a free plan."""
    email: EmailStr = Field(..., description="Email do cliente")
    product: Literal["sei-mcp", "tribunais-mcp", "bundle", "default"] = Field(
        default="default",
        description="Produto"
    )


class RegisterFreeResponse(BaseModel):
    """Response after registering free plan."""
    success: bool
    message: str
    license_id: str | None = None
    plan: str = "free"
    requests_per_month: int = 50


class PlanInfo(BaseModel):
    """Plan information for display."""
    id: str
    name: str
    description: str
    features: list[str]
    price_monthly: int = Field(..., description="Preco mensal em centavos")
    price_yearly: int = Field(..., description="Preco anual em centavos")
    requests_per_month: int = Field(..., description="Limite de requisicoes (-1 = ilimitado)")
    currency: str = "BRL"
    recommended: bool = False


class SessionStatusResponse(BaseModel):
    """Response with checkout session status."""
    session_id: str
    status: str
    payment_status: str
    customer_email: str | None
    subscription_id: str | None
    plan: str | None
    amount_total: int | None
    currency: str | None


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/create", response_model=CheckoutResponse)
async def create_checkout_session(
    request: CreateCheckoutRequest,
    db: AsyncSession = Depends(get_db),
) -> CheckoutResponse:
    """
    Criar uma sessao de checkout do Stripe para assinatura.

    Retorna uma URL para redirecionar o usuario para completar o pagamento.

    **Planos disponiveis:**
    - `professional`: R$ 29,90/mes - 500 requisicoes/mes
    - `enterprise`: R$ 99,90/mes - Requisicoes ilimitadas

    **Exemplo de uso:**
    ```python
    response = requests.post("/api/v1/checkout/create", json={
        "email": "cliente@email.com",
        "plan": "professional",
        "interval": "monthly",
        "success_url": "https://seusite.com/sucesso",
        "cancel_url": "https://seusite.com/cancelado"
    })
    # Redirecione o usuario para response["checkout_url"]
    ```
    """
    try:
        plan = PlanId(request.plan)
        product = ProductType(request.product) if request.product and request.product != "default" else None

        session = await StripeService.create_checkout_session(
            email=request.email,
            plan=plan,
            interval=request.interval,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            client_reference_id=request.client_reference_id,
            product=product,
            customer_name=request.customer_name,
        )

        return CheckoutResponse(
            checkout_url=session.url,
            session_id=session.id,
            expires_at=session.expires_at,
        )

    except ValueError as e:
        logger.warning("checkout_validation_error", email=request.email, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except stripe.StripeError as e:
        logger.error("checkout_stripe_error", email=request.email, error=str(e))
        raise HTTPException(status_code=500, detail=f"Erro no Stripe: {str(e)}")
    except Exception as e:
        logger.error("checkout_error", email=request.email, error=str(e))
        raise HTTPException(status_code=500, detail=f"Erro ao criar sessao: {str(e)}")


@router.post("/free", response_model=RegisterFreeResponse)
async def register_free_plan(
    request: RegisterFreeRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterFreeResponse:
    """
    Registrar um plano gratuito (FREE).

    O plano gratuito nao requer pagamento e oferece:
    - 50 requisicoes/mes
    - Acesso basico a API
    - Suporte por email

    Ideal para testar a API antes de assinar um plano pago.
    """
    try:
        service = LicenseService(db)

        # Determina o produto
        if request.product == "default":
            product = ProductType.TRIBUNAIS_MCP  # Produto padrao
        else:
            product = ProductType(request.product)

        # Verifica se ja existe licenca
        existing = await service.get_by_email(request.email, product)

        if existing:
            return RegisterFreeResponse(
                success=True,
                message="Voce ja possui uma licenca ativa.",
                license_id=existing.id,
                plan=existing.plan.value,
                requests_per_month=StripeService.get_request_limit(existing.plan),
            )

        # Cria licenca gratuita (como trial permanente)
        license = await service.create_trial(
            email=request.email,
            product=product,
        )

        # Atualiza para FREE (nao trialing)
        license.plan = PlanId.FREE
        license.status = LicenseStatus.ACTIVE
        await db.commit()

        logger.info(
            "free_plan_registered",
            email=request.email,
            license_id=license.id,
        )

        return RegisterFreeResponse(
            success=True,
            message="Plano gratuito ativado com sucesso!",
            license_id=license.id,
            plan="free",
            requests_per_month=50,
        )

    except ValueError as e:
        logger.warning("free_registration_error", email=request.email, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("free_registration_error", email=request.email, error=str(e))
        raise HTTPException(status_code=500, detail=f"Erro ao registrar: {str(e)}")


@router.get("/plans", response_model=list[PlanInfo])
async def get_plans() -> list[PlanInfo]:
    """
    Listar todos os planos disponiveis.

    Retorna informacoes sobre cada plano incluindo:
    - Preco mensal e anual (em centavos)
    - Limite de requisicoes por mes
    - Lista de funcionalidades

    **Planos:**
    - FREE: R$ 0 - 50 req/mes
    - PROFESSIONAL: R$ 29,90/mes - 500 req/mes
    - ENTERPRISE: R$ 99,90/mes - Ilimitado
    """
    plans = []

    for plan_id, config in PLAN_CONFIGS.items():
        plans.append(PlanInfo(
            id=config.plan_id.value,
            name=config.name,
            description=_get_plan_description(config.plan_id),
            features=config.features,
            price_monthly=config.price_monthly_cents,
            price_yearly=config.price_yearly_cents,
            requests_per_month=config.requests_per_month,
            currency="BRL",
            recommended=config.plan_id == PlanId.PROFESSIONAL,
        ))

    return plans


@router.get("/session/{session_id}", response_model=SessionStatusResponse)
async def get_checkout_session(
    session_id: str,
) -> SessionStatusResponse:
    """
    Obter detalhes de uma sessao de checkout.

    Use este endpoint para verificar o status do pagamento
    apos o usuario retornar do checkout.

    **Status possiveis:**
    - `complete`: Pagamento concluido com sucesso
    - `expired`: Sessao expirou
    - `open`: Aguardando pagamento
    """
    try:
        session = stripe.checkout.Session.retrieve(
            session_id,
            expand=["subscription"],
        )

        metadata = session.get("metadata", {})

        return SessionStatusResponse(
            session_id=session.id,
            status=session.status,
            payment_status=session.payment_status,
            customer_email=session.customer_email or session.customer_details.email if session.customer_details else None,
            subscription_id=session.subscription.id if session.subscription else None,
            plan=metadata.get("plan"),
            amount_total=session.amount_total,
            currency=session.currency,
        )

    except stripe.InvalidRequestError:
        raise HTTPException(status_code=404, detail="Sessao nao encontrada")
    except stripe.StripeError as e:
        logger.error("get_session_error", session_id=session_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Erro ao buscar sessao: {str(e)}")


@router.get("/prices")
async def get_stripe_prices(
    product: str | None = Query(default=None, description="Filtrar por produto"),
) -> dict:
    """
    Listar precos configurados no Stripe.

    Endpoint de debug para verificar se os price_ids estao corretos.
    Retorna os precos ativos no Stripe.
    """
    try:
        prices = stripe.Price.list(
            active=True,
            limit=100,
            expand=["data.product"],
        )

        result = []
        for price in prices.data:
            product_name = price.product.name if hasattr(price.product, "name") else price.product
            result.append({
                "price_id": price.id,
                "product": product_name,
                "unit_amount": price.unit_amount,
                "currency": price.currency,
                "interval": price.recurring.interval if price.recurring else None,
                "active": price.active,
            })

        return {
            "prices": result,
            "total": len(result),
        }

    except stripe.StripeError as e:
        logger.error("list_prices_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Erro ao listar precos: {str(e)}")


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _get_plan_description(plan: PlanId) -> str:
    """Get plan description."""
    descriptions = {
        PlanId.FREE: "Ideal para testar a API",
        PlanId.PROFESSIONAL: "Para desenvolvedores e pequenos projetos",
        PlanId.ENTERPRISE: "Para empresas com alto volume de requisicoes",
        PlanId.OFFICE: "Para escritorios com multiplos usuarios",
    }
    return descriptions.get(plan, "")
