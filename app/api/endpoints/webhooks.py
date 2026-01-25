"""
Stripe webhook endpoints for handling subscription events

Webhooks configurados:
- checkout.session.completed - Checkout finalizado com sucesso
- customer.subscription.created - Nova assinatura criada
- customer.subscription.updated - Assinatura atualizada (upgrade/downgrade)
- customer.subscription.deleted - Assinatura cancelada
- invoice.paid - Fatura paga com sucesso
- invoice.payment_failed - Falha no pagamento

Para configurar no Stripe Dashboard:
1. Va em Developers > Webhooks
2. Adicione endpoint: https://sua-api.com/api/v1/webhooks/stripe
3. Selecione os eventos acima
4. Copie o Signing Secret para STRIPE_WEBHOOK_SECRET
"""
import stripe
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.database import get_db
from app.services.stripe_service import StripeService, PLAN_REQUEST_LIMITS
from app.services.license_service import LicenseService
from app.models.license import LicenseStatus, PlanId

logger = structlog.get_logger()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ============================================================================
# WEBHOOK ENDPOINT PRINCIPAL
# ============================================================================

@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle Stripe webhook events.

    Este endpoint recebe eventos do Stripe sobre mudancas em assinaturas
    e atualiza o banco de dados de licencas de acordo.

    **Eventos processados:**
    - `checkout.session.completed`: Cria/atualiza licenca apos pagamento
    - `customer.subscription.created`: Nova assinatura
    - `customer.subscription.updated`: Alteracao de plano ou status
    - `customer.subscription.deleted`: Cancelamento
    - `invoice.paid`: Confirma pagamento e renova periodo
    - `invoice.payment_failed`: Marca licenca como inadimplente

    **Seguranca:**
    - Verifica assinatura do webhook usando STRIPE_WEBHOOK_SECRET
    - Rejeita eventos com assinatura invalida
    """
    # Get raw body for signature verification
    payload = await request.body()
    signature = request.headers.get("stripe-signature")

    if not signature:
        logger.warning("webhook_missing_signature")
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        # Verify and parse the event
        event = StripeService.verify_webhook_signature(payload, signature)
    except stripe.SignatureVerificationError:
        logger.error("webhook_signature_invalid")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except ValueError:
        logger.error("webhook_payload_invalid")
        raise HTTPException(status_code=400, detail="Invalid payload")

    event_type = event["type"]
    event_id = event["id"]

    logger.info(
        "webhook_received",
        event_type=event_type,
        event_id=event_id,
    )

    license_service = LicenseService(db)

    try:
        # Route to appropriate handler
        handlers = {
            "checkout.session.completed": handle_checkout_completed,
            "customer.subscription.created": handle_subscription_created,
            "customer.subscription.updated": handle_subscription_updated,
            "customer.subscription.deleted": handle_subscription_deleted,
            "customer.subscription.trial_will_end": handle_trial_will_end,
            "invoice.paid": handle_invoice_paid,
            "invoice.payment_failed": handle_invoice_payment_failed,
            "customer.subscription.paused": handle_subscription_paused,
            "customer.subscription.resumed": handle_subscription_resumed,
        }

        handler = handlers.get(event_type)

        if handler:
            await handler(event, license_service, db)
            await db.commit()
            logger.info("webhook_processed", event_type=event_type, event_id=event_id)
        else:
            logger.info("webhook_event_ignored", event_type=event_type)

        return {"status": "ok", "event_type": event_type, "event_id": event_id}

    except Exception as e:
        logger.error(
            "webhook_processing_error",
            event_type=event_type,
            event_id=event_id,
            error=str(e),
        )
        # Nao faz rollback automatico - deixa o Stripe retentar
        raise HTTPException(status_code=500, detail=f"Error processing webhook: {str(e)}")


# ============================================================================
# WEBHOOK HANDLERS
# ============================================================================

async def handle_checkout_completed(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle completed checkout session.

    Este e o evento mais importante - confirma que o cliente pagou.
    Cria ou atualiza a licenca com os dados da assinatura.
    """
    data = StripeService.parse_checkout_session_event(event)

    # Ignora se nao for subscription
    if data["mode"] != "subscription":
        logger.info("checkout_not_subscription", mode=data["mode"])
        return

    # Ignora se pagamento nao foi concluido
    if data["payment_status"] != "paid":
        logger.info("checkout_not_paid", status=data["payment_status"])
        return

    email = data["email"]
    if not email:
        logger.error("checkout_no_email", session_id=data["session_id"])
        return

    # Busca dados da subscription criada
    subscription_id = data["subscription_id"]
    if subscription_id:
        subscription = await StripeService.get_subscription(subscription_id)
        sub_data = StripeService.parse_subscription_event({"data": {"object": subscription}})

        await service.create_or_update_from_stripe(
            email=email,
            customer_id=data["customer_id"],
            subscription_id=subscription_id,
            status=sub_data["status"],
            plan=data["plan"],
            product=data["product"],
            current_period_start=sub_data["current_period_start"],
            current_period_end=sub_data["current_period_end"],
        )

    logger.info(
        "checkout_completed",
        email=email,
        subscription_id=subscription_id,
        plan=data["plan"],
        amount=data["amount_total"],
    )


async def handle_subscription_created(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle new subscription creation.

    Cria licenca para nova assinatura. Pode ser chamado antes ou
    depois do checkout.session.completed dependendo do metodo de pagamento.
    """
    data = StripeService.parse_subscription_event(event)

    # Get customer email
    customer = stripe.Customer.retrieve(data["customer_id"])
    email = customer.email

    if not email:
        logger.error("subscription_no_email", customer_id=data["customer_id"])
        return

    await service.create_or_update_from_stripe(
        email=email,
        customer_id=data["customer_id"],
        subscription_id=data["subscription_id"],
        status=data["status"],
        plan=data["plan"],
        product=data["product"],
        current_period_start=data["current_period_start"],
        current_period_end=data["current_period_end"],
    )

    logger.info(
        "subscription_created",
        email=email,
        plan=data["plan"],
        status=data["status"],
    )


async def handle_subscription_updated(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle subscription updates.

    Chamado quando:
    - Cliente troca de plano (upgrade/downgrade)
    - Assinatura e renovada
    - Status muda (trialing -> active, etc)
    - Cliente agenda cancelamento
    """
    data = StripeService.parse_subscription_event(event)

    # Get customer email
    customer = stripe.Customer.retrieve(data["customer_id"])
    email = customer.email

    if not email:
        logger.error("subscription_update_no_email", customer_id=data["customer_id"])
        return

    await service.create_or_update_from_stripe(
        email=email,
        customer_id=data["customer_id"],
        subscription_id=data["subscription_id"],
        status=data["status"],
        plan=data["plan"],
        product=data["product"],
        current_period_start=data["current_period_start"],
        current_period_end=data["current_period_end"],
        cancel_at_period_end=data["cancel_at_period_end"],
        canceled_at=data["canceled_at"],
    )

    logger.info(
        "subscription_updated",
        email=email,
        plan=data["plan"],
        status=data["status"],
        cancel_at_period_end=data["cancel_at_period_end"],
    )


async def handle_subscription_deleted(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle subscription cancellation/deletion.

    A assinatura foi definitivamente cancelada (nao apenas agendada).
    Marca a licenca como cancelada.
    """
    data = StripeService.parse_subscription_event(event)

    license = await service.get_by_stripe_subscription(data["subscription_id"])

    if license:
        license.status = LicenseStatus.CANCELED
        license.canceled_at = data["canceled_at"]

        logger.info(
            "subscription_deleted",
            email=license.email,
            subscription_id=data["subscription_id"],
        )
    else:
        logger.warning(
            "subscription_deleted_not_found",
            subscription_id=data["subscription_id"],
        )


async def handle_trial_will_end(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle trial ending notification (3 days before).

    Util para enviar email lembrando o cliente de adicionar
    metodo de pagamento.
    """
    subscription = event["data"]["object"]
    customer_id = subscription["customer"]

    license = await service.get_by_stripe_customer(customer_id)

    if license:
        # Aqui voce pode disparar um email de lembrete
        # ou uma notificacao push
        logger.info(
            "trial_will_end",
            email=license.email,
            days_remaining=3,
            subscription_id=subscription["id"],
        )


async def handle_invoice_paid(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle successful payment.

    Confirma que o pagamento foi processado. Atualiza o periodo
    da licenca se necessario.
    """
    data = StripeService.parse_invoice_event(event)

    # Ignora invoices que nao sao de subscription
    if not data["subscription_id"]:
        return

    license = await service.get_by_stripe_subscription(data["subscription_id"])

    if license:
        # Atualiza status para ativo se estava past_due
        if license.status == LicenseStatus.PAST_DUE:
            license.status = LicenseStatus.ACTIVE

        # Atualiza periodo se fornecido
        if data["period_start"] and data["period_end"]:
            license.current_period_start = data["period_start"]
            license.current_period_end = data["period_end"]

        logger.info(
            "invoice_paid",
            email=license.email,
            amount=data["amount_paid"],
            currency=data["currency"],
            billing_reason=data["billing_reason"],
        )


async def handle_invoice_payment_failed(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle failed payment.

    Marca a licenca como inadimplente (past_due).
    O Stripe tentara novamente automaticamente conforme configurado.
    """
    data = StripeService.parse_invoice_event(event)

    # Ignora invoices que nao sao de subscription
    if not data["subscription_id"]:
        return

    license = await service.get_by_stripe_subscription(data["subscription_id"])

    if license:
        license.status = LicenseStatus.PAST_DUE

        logger.warning(
            "invoice_payment_failed",
            email=license.email,
            amount_due=data["amount_due"],
            currency=data["currency"],
        )

        # Aqui voce pode disparar um email avisando o cliente
        # sobre a falha no pagamento


async def handle_subscription_paused(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle subscription paused.

    Marca a licenca como pausada. O cliente nao sera cobrado
    mas tambem nao pode usar a API.
    """
    data = StripeService.parse_subscription_event(event)

    license = await service.get_by_stripe_subscription(data["subscription_id"])

    if license:
        license.status = LicenseStatus.PAUSED

        logger.info(
            "subscription_paused",
            email=license.email,
            subscription_id=data["subscription_id"],
        )


async def handle_subscription_resumed(
    event: dict,
    service: LicenseService,
    db: AsyncSession,
) -> None:
    """
    Handle subscription resumed.

    Reativa a licenca apos pausa.
    """
    data = StripeService.parse_subscription_event(event)

    license = await service.get_by_stripe_subscription(data["subscription_id"])

    if license:
        license.status = LicenseStatus.ACTIVE

        logger.info(
            "subscription_resumed",
            email=license.email,
            subscription_id=data["subscription_id"],
        )


# ============================================================================
# WEBHOOK DE TESTE
# ============================================================================

@router.post("/stripe/test")
async def test_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Endpoint de teste para webhooks (apenas desenvolvimento).

    Processa eventos sem verificar assinatura.
    NAO USE EM PRODUCAO!
    """
    from app.config import settings

    if settings.is_production:
        raise HTTPException(status_code=403, detail="Disabled in production")

    payload = await request.json()

    event_type = payload.get("type", "unknown")
    logger.info("test_webhook_received", event_type=event_type)

    return {
        "status": "ok",
        "message": "Test webhook received",
        "event_type": event_type,
    }
