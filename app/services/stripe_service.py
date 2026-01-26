"""
Stripe integration service for payment processing

Planos configurados:
- FREE: 0 BRL, 50 requisicoes/mes
- PROFESSIONAL: 29.90 BRL/mes, 500 requisicoes/mes
- ENTERPRISE: 99.90 BRL/mes, requisicoes ilimitadas
"""
import stripe
from datetime import datetime
from typing import Any
from enum import Enum

import structlog

from app.config import settings
from app.models.license import PlanId, ProductType

logger = structlog.get_logger()

# Initialize Stripe
stripe.api_key = settings.stripe_secret_key


# ============================================================================
# CONFIGURACAO DE PLANOS E PRECOS
# ============================================================================

class PlanConfig:
    """Configuracao de um plano."""
    def __init__(
        self,
        plan_id: PlanId,
        name: str,
        price_monthly_cents: int,
        price_yearly_cents: int,
        requests_per_month: int,  # -1 = ilimitado
        features: list[str],
    ):
        self.plan_id = plan_id
        self.name = name
        self.price_monthly_cents = price_monthly_cents
        self.price_yearly_cents = price_yearly_cents
        self.requests_per_month = requests_per_month
        self.features = features


# Configuracao dos planos
PLAN_CONFIGS: dict[PlanId, PlanConfig] = {
    PlanId.FREE: PlanConfig(
        plan_id=PlanId.FREE,
        name="Free",
        price_monthly_cents=0,
        price_yearly_cents=0,
        requests_per_month=50,
        features=[
            "50 requisicoes/mes",
            "Acesso basico a API",
            "Suporte por email",
        ],
    ),
    PlanId.PROFESSIONAL: PlanConfig(
        plan_id=PlanId.PROFESSIONAL,
        name="Professional",
        price_monthly_cents=2990,  # R$ 29,90
        price_yearly_cents=29900,  # R$ 299,00 (2 meses gratis)
        requests_per_month=500,
        features=[
            "500 requisicoes/mes",
            "Acesso completo a API",
            "Suporte prioritario",
            "Webhooks personalizados",
            "Dashboard de uso",
        ],
    ),
    PlanId.ENTERPRISE: PlanConfig(
        plan_id=PlanId.ENTERPRISE,
        name="Enterprise",
        price_monthly_cents=9990,  # R$ 99,90
        price_yearly_cents=99900,  # R$ 999,00 (2 meses gratis)
        requests_per_month=-1,  # Ilimitado
        features=[
            "Requisicoes ilimitadas",
            "Acesso completo a API",
            "Suporte dedicado 24/7",
            "Webhooks personalizados",
            "Dashboard avancado",
            "API dedicada",
            "SLA garantido",
            "Integracao customizada",
        ],
    ),
}


# Price IDs mapping - Configurados via variaveis de ambiente ou hardcoded
# Formato: price_XXXXXXX
# Para configurar via ENV: STRIPE_PRICE_PROFESSIONAL_MONTHLY=price_xxx
import os

def _get_price_id(key: str, default: str = "") -> str:
    """Get price ID from environment variable or return default."""
    env_key = f"STRIPE_PRICE_{key.upper()}"
    return os.getenv(env_key, default)

PRICE_IDS: dict[str, dict[str, str]] = {
    # Produto principal (Iudex API)
    "default": {
        # FREE nao precisa de price_id (nao tem cobranca)
        "professional_monthly": _get_price_id("PROFESSIONAL_MONTHLY", "price_1Stbk5CvEJFyzDT1jUZofSDM"),
        "professional_yearly": _get_price_id("PROFESSIONAL_YEARLY", "price_1Stbk6CvEJFyzDT1gucqDDtL"),
        "enterprise_monthly": _get_price_id("ENTERPRISE_MONTHLY", "price_1Stbk7CvEJFyzDT1Ol3ucCbI"),
        "enterprise_yearly": _get_price_id("ENTERPRISE_YEARLY", "price_1Stbk8CvEJFyzDT1ZUGDqo8u"),
    },
}


# Limites de requisicoes por plano (usado para validacao)
PLAN_REQUEST_LIMITS: dict[PlanId, int] = {
    PlanId.FREE: 50,
    PlanId.PROFESSIONAL: 500,
    PlanId.ENTERPRISE: -1,  # Ilimitado
    PlanId.OFFICE: 500,  # Compatibilidade
}


class StripeService:
    """Service for Stripe payment operations."""

    # ========================================================================
    # METODOS DE CONSULTA DE PLANOS
    # ========================================================================

    @staticmethod
    def get_plan_config(plan: PlanId) -> PlanConfig | None:
        """Retorna a configuracao de um plano."""
        return PLAN_CONFIGS.get(plan)

    @staticmethod
    def get_all_plans() -> list[dict]:
        """Retorna todos os planos disponiveis."""
        return [
            {
                "id": config.plan_id.value,
                "name": config.name,
                "price_monthly": config.price_monthly_cents,
                "price_yearly": config.price_yearly_cents,
                "requests_per_month": config.requests_per_month,
                "features": config.features,
                "currency": "BRL",
            }
            for config in PLAN_CONFIGS.values()
        ]

    @staticmethod
    def get_request_limit(plan: PlanId) -> int:
        """Retorna o limite de requisicoes para um plano."""
        return PLAN_REQUEST_LIMITS.get(plan, 50)

    # ========================================================================
    # METODOS DE CLIENTE
    # ========================================================================

    @staticmethod
    async def create_customer(
        email: str,
        name: str | None = None,
        metadata: dict | None = None,
    ) -> stripe.Customer:
        """Create a new Stripe customer."""
        try:
            customer_metadata = {"source": "iudex-licensing"}
            if metadata:
                customer_metadata.update(metadata)

            customer = stripe.Customer.create(
                email=email,
                name=name,
                metadata=customer_metadata,
            )
            logger.info("stripe_customer_created", email=email, customer_id=customer.id)
            return customer
        except stripe.StripeError as e:
            logger.error("stripe_customer_creation_failed", email=email, error=str(e))
            raise

    @staticmethod
    async def get_or_create_customer(
        email: str,
        name: str | None = None,
    ) -> stripe.Customer:
        """Get existing customer or create a new one."""
        try:
            # Search for existing customer
            customers = stripe.Customer.search(query=f'email:"{email}"')
            if customers.data:
                return customers.data[0]

            # Create new customer if not found
            return await StripeService.create_customer(email, name)
        except stripe.StripeError as e:
            logger.error("stripe_get_customer_failed", email=email, error=str(e))
            raise

    @staticmethod
    async def get_customer(customer_id: str) -> stripe.Customer | None:
        """Retrieve a customer by ID."""
        try:
            return stripe.Customer.retrieve(customer_id)
        except stripe.InvalidRequestError:
            return None
        except stripe.StripeError as e:
            logger.error("stripe_get_customer_failed", customer_id=customer_id, error=str(e))
            raise

    @staticmethod
    async def update_customer(
        customer_id: str,
        email: str | None = None,
        name: str | None = None,
        metadata: dict | None = None,
    ) -> stripe.Customer:
        """Update a Stripe customer."""
        try:
            update_data = {}
            if email:
                update_data["email"] = email
            if name:
                update_data["name"] = name
            if metadata:
                update_data["metadata"] = metadata

            customer = stripe.Customer.modify(customer_id, **update_data)
            logger.info("stripe_customer_updated", customer_id=customer_id)
            return customer
        except stripe.StripeError as e:
            logger.error("stripe_customer_update_failed", customer_id=customer_id, error=str(e))
            raise

    # ========================================================================
    # METODOS DE PRICE ID
    # ========================================================================

    @staticmethod
    def get_price_id(
        plan: PlanId,
        interval: str,
        product: ProductType | str | None = None,
    ) -> str | None:
        """
        Get the Stripe price ID for a plan/interval combination.

        Args:
            plan: PlanId enum (professional, enterprise)
            interval: "monthly" ou "yearly"
            product: ProductType ou string opcional (default usa 'default')

        Returns:
            Price ID string ou None se nao encontrado
        """
        # FREE nao tem price_id
        if plan == PlanId.FREE:
            return None

        # Determina o produto
        if product is None:
            product_key = "default"
        elif isinstance(product, ProductType):
            product_key = product.value
        else:
            product_key = str(product)

        # Busca nos PRICE_IDS
        product_prices = PRICE_IDS.get(product_key, PRICE_IDS.get("default", {}))
        price_key = f"{plan.value}_{interval}"
        return product_prices.get(price_key)

    # ========================================================================
    # METODOS DE CHECKOUT
    # ========================================================================

    @staticmethod
    async def create_checkout_session(
        email: str,
        plan: PlanId,
        interval: str = "monthly",
        success_url: str | None = None,
        cancel_url: str | None = None,
        client_reference_id: str | None = None,
        product: ProductType | str | None = None,
        trial_days: int | None = None,
        allow_promotion_codes: bool = True,
        customer_name: str | None = None,
    ) -> stripe.checkout.Session:
        """
        Create a Stripe Checkout session for subscription.

        Args:
            email: Email do cliente
            plan: PlanId (professional ou enterprise)
            interval: "monthly" ou "yearly"
            success_url: URL de redirecionamento apos sucesso
            cancel_url: URL de redirecionamento se cancelar
            client_reference_id: ID de referencia do cliente
            product: Produto opcional (para multiplos produtos)
            trial_days: Dias de trial (None = usa config padrao)
            allow_promotion_codes: Permitir codigos de desconto
            customer_name: Nome do cliente

        Returns:
            Stripe Checkout Session

        Raises:
            ValueError: Se plano FREE ou price_id nao configurado
            stripe.StripeError: Erro na API do Stripe
        """
        # FREE nao usa checkout - e criado automaticamente
        if plan == PlanId.FREE:
            raise ValueError("Plano FREE nao requer checkout. Use o endpoint de registro.")

        try:
            # Get or create customer
            customer = await StripeService.get_or_create_customer(email, customer_name)

            # Get price ID
            price_id = StripeService.get_price_id(plan, interval, product)
            if not price_id:
                raise ValueError(
                    f"Price ID nao configurado para {plan.value}/{interval}. "
                    "Configure os IDs no Stripe Dashboard e atualize PRICE_IDS."
                )

            # URLs padrao
            if not success_url:
                success_url = f"{settings.frontend_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
            if not cancel_url:
                cancel_url = f"{settings.frontend_url}/checkout/cancel"

            # Determina trial
            if trial_days is None:
                trial_days = settings.trial_days if plan == PlanId.PROFESSIONAL else 0

            # Metadata
            product_value = product.value if isinstance(product, ProductType) else (product or "default")
            metadata = {
                "product": product_value,
                "plan": plan.value,
                "interval": interval,
            }

            # Subscription data
            subscription_data = {
                "metadata": metadata,
            }
            if trial_days and trial_days > 0:
                subscription_data["trial_period_days"] = trial_days

            # Create checkout session
            session = stripe.checkout.Session.create(
                customer=customer.id,
                mode="subscription",
                payment_method_types=["card"],
                line_items=[
                    {
                        "price": price_id,
                        "quantity": 1,
                    }
                ],
                success_url=success_url,
                cancel_url=cancel_url,
                client_reference_id=client_reference_id,
                subscription_data=subscription_data,
                metadata=metadata,
                allow_promotion_codes=allow_promotion_codes,
                billing_address_collection="required",
                locale="pt-BR",
                tax_id_collection={"enabled": True},
                customer_update={
                    "address": "auto",
                    "name": "auto",
                },
            )

            logger.info(
                "checkout_session_created",
                email=email,
                plan=plan.value,
                interval=interval,
                session_id=session.id,
                checkout_url=session.url,
            )
            return session

        except stripe.StripeError as e:
            logger.error(
                "checkout_session_failed",
                email=email,
                plan=plan.value,
                error=str(e),
            )
            raise

    @staticmethod
    async def create_checkout_session_for_upgrade(
        customer_id: str,
        current_subscription_id: str,
        new_plan: PlanId,
        interval: str = "monthly",
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> stripe.checkout.Session:
        """
        Create a checkout session for plan upgrade.

        O Stripe trata automaticamente o proration (ajuste de valor).
        """
        try:
            price_id = StripeService.get_price_id(new_plan, interval)
            if not price_id:
                raise ValueError(f"Price ID nao configurado para {new_plan.value}/{interval}")

            if not success_url:
                success_url = f"{settings.frontend_url}/upgrade/success?session_id={{CHECKOUT_SESSION_ID}}"
            if not cancel_url:
                cancel_url = f"{settings.frontend_url}/upgrade/cancel"

            # Para upgrade, modifica a subscription existente
            session = stripe.checkout.Session.create(
                customer=customer_id,
                mode="subscription",
                payment_method_types=["card", "boleto", "pix"],
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=success_url,
                cancel_url=cancel_url,
                subscription_data={
                    "metadata": {
                        "plan": new_plan.value,
                        "upgraded_from": current_subscription_id,
                    },
                },
                locale="pt-BR",
            )

            logger.info(
                "upgrade_checkout_created",
                customer_id=customer_id,
                new_plan=new_plan.value,
                session_id=session.id,
            )
            return session

        except stripe.StripeError as e:
            logger.error("upgrade_checkout_failed", customer_id=customer_id, error=str(e))
            raise

    @staticmethod
    async def update_subscription_plan(
        subscription_id: str,
        new_plan: PlanId,
        interval: str = "monthly",
        proration_behavior: str = "create_prorations",
    ) -> stripe.Subscription:
        """
        Update subscription to a new plan (upgrade/downgrade).

        Args:
            subscription_id: ID da subscription atual
            new_plan: Novo plano
            interval: "monthly" ou "yearly"
            proration_behavior: "create_prorations", "none", "always_invoice"

        Returns:
            Updated subscription
        """
        try:
            price_id = StripeService.get_price_id(new_plan, interval)
            if not price_id:
                raise ValueError(f"Price ID nao configurado para {new_plan.value}/{interval}")

            # Get current subscription
            subscription = stripe.Subscription.retrieve(subscription_id)

            # Update to new price
            updated = stripe.Subscription.modify(
                subscription_id,
                items=[{
                    "id": subscription["items"]["data"][0].id,
                    "price": price_id,
                }],
                proration_behavior=proration_behavior,
                metadata={
                    "plan": new_plan.value,
                    "interval": interval,
                },
            )

            logger.info(
                "subscription_plan_updated",
                subscription_id=subscription_id,
                new_plan=new_plan.value,
            )
            return updated

        except stripe.StripeError as e:
            logger.error("subscription_update_failed", subscription_id=subscription_id, error=str(e))
            raise

    # ========================================================================
    # METODOS DE PORTAL E SUBSCRIPTION
    # ========================================================================

    @staticmethod
    async def create_portal_session(
        customer_id: str,
        return_url: str | None = None,
    ) -> stripe.billing_portal.Session:
        """
        Create a Stripe Customer Portal session.

        O portal permite ao cliente:
        - Atualizar metodo de pagamento
        - Ver faturas
        - Cancelar assinatura
        - Trocar de plano
        """
        try:
            if not return_url:
                return_url = f"{settings.frontend_url}/account"

            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url,
            )
            logger.info("portal_session_created", customer_id=customer_id, url=session.url)
            return session
        except stripe.StripeError as e:
            logger.error("portal_session_failed", customer_id=customer_id, error=str(e))
            raise

    @staticmethod
    async def get_subscription(subscription_id: str) -> stripe.Subscription:
        """Get subscription details from Stripe."""
        try:
            return stripe.Subscription.retrieve(
                subscription_id,
                expand=["customer", "default_payment_method"],
            )
        except stripe.StripeError as e:
            logger.error("get_subscription_failed", subscription_id=subscription_id, error=str(e))
            raise

    @staticmethod
    async def list_customer_subscriptions(customer_id: str) -> list[stripe.Subscription]:
        """List all subscriptions for a customer."""
        try:
            subscriptions = stripe.Subscription.list(
                customer=customer_id,
                status="all",
                expand=["data.default_payment_method"],
            )
            return list(subscriptions.data)
        except stripe.StripeError as e:
            logger.error("list_subscriptions_failed", customer_id=customer_id, error=str(e))
            raise

    @staticmethod
    async def cancel_subscription(
        subscription_id: str,
        at_period_end: bool = True,
        cancellation_reason: str | None = None,
    ) -> stripe.Subscription:
        """
        Cancel a subscription.

        Args:
            subscription_id: ID da subscription
            at_period_end: True = cancela no fim do periodo, False = imediatamente
            cancellation_reason: Motivo do cancelamento (para analytics)
        """
        try:
            if at_period_end:
                subscription = stripe.Subscription.modify(
                    subscription_id,
                    cancel_at_period_end=True,
                    metadata={"cancellation_reason": cancellation_reason} if cancellation_reason else None,
                )
            else:
                subscription = stripe.Subscription.cancel(
                    subscription_id,
                    cancellation_details={
                        "comment": cancellation_reason,
                    } if cancellation_reason else None,
                )

            logger.info(
                "subscription_canceled",
                subscription_id=subscription_id,
                at_period_end=at_period_end,
                reason=cancellation_reason,
            )
            return subscription
        except stripe.StripeError as e:
            logger.error("cancel_subscription_failed", subscription_id=subscription_id, error=str(e))
            raise

    @staticmethod
    async def reactivate_subscription(subscription_id: str) -> stripe.Subscription:
        """
        Reactivate a subscription that was scheduled to cancel.

        Apenas funciona se cancel_at_period_end=True e ainda nao expirou.
        """
        try:
            subscription = stripe.Subscription.modify(
                subscription_id,
                cancel_at_period_end=False,
            )
            logger.info("subscription_reactivated", subscription_id=subscription_id)
            return subscription
        except stripe.StripeError as e:
            logger.error("reactivate_subscription_failed", subscription_id=subscription_id, error=str(e))
            raise

    @staticmethod
    async def pause_subscription(
        subscription_id: str,
        resume_at: datetime | None = None,
    ) -> stripe.Subscription:
        """
        Pause a subscription.

        O cliente nao sera cobrado durante a pausa.
        """
        try:
            pause_data = {"behavior": "void"}
            if resume_at:
                pause_data["resumes_at"] = int(resume_at.timestamp())

            subscription = stripe.Subscription.modify(
                subscription_id,
                pause_collection=pause_data,
            )
            logger.info("subscription_paused", subscription_id=subscription_id)
            return subscription
        except stripe.StripeError as e:
            logger.error("pause_subscription_failed", subscription_id=subscription_id, error=str(e))
            raise

    @staticmethod
    async def resume_subscription(subscription_id: str) -> stripe.Subscription:
        """Resume a paused subscription."""
        try:
            subscription = stripe.Subscription.modify(
                subscription_id,
                pause_collection="",
            )
            logger.info("subscription_resumed", subscription_id=subscription_id)
            return subscription
        except stripe.StripeError as e:
            logger.error("resume_subscription_failed", subscription_id=subscription_id, error=str(e))
            raise

    # ========================================================================
    # METODOS DE WEBHOOK
    # ========================================================================

    @staticmethod
    def verify_webhook_signature(payload: bytes, signature: str) -> dict[str, Any]:
        """
        Verify and parse a Stripe webhook event.

        Args:
            payload: Raw request body
            signature: Stripe-Signature header

        Returns:
            Parsed event dictionary

        Raises:
            stripe.SignatureVerificationError: Assinatura invalida
            ValueError: Payload invalido
        """
        try:
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                settings.stripe_webhook_secret,
            )
            return event
        except stripe.SignatureVerificationError as e:
            logger.error("webhook_signature_invalid", error=str(e))
            raise
        except ValueError as e:
            logger.error("webhook_payload_invalid", error=str(e))
            raise

    @staticmethod
    def parse_subscription_event(event: dict[str, Any]) -> dict[str, Any]:
        """
        Parse subscription data from a webhook event.

        Eventos suportados:
        - customer.subscription.created
        - customer.subscription.updated
        - customer.subscription.deleted
        """
        subscription = event["data"]["object"]

        # Extract period dates
        current_period_start = datetime.fromtimestamp(subscription["current_period_start"])
        current_period_end = datetime.fromtimestamp(subscription["current_period_end"])

        # Get metadata
        metadata = subscription.get("metadata", {})

        # Get plan from metadata or from price
        plan = metadata.get("plan", "professional")

        # Try to get product from metadata
        product = metadata.get("product", "default")

        # Get price info if available
        price_info = None
        if subscription.get("items", {}).get("data"):
            item = subscription["items"]["data"][0]
            price_info = {
                "price_id": item.get("price", {}).get("id"),
                "amount": item.get("price", {}).get("unit_amount"),
                "currency": item.get("price", {}).get("currency"),
                "interval": item.get("price", {}).get("recurring", {}).get("interval"),
            }

        return {
            "subscription_id": subscription["id"],
            "customer_id": subscription["customer"],
            "status": subscription["status"],
            "plan": plan,
            "product": product,
            "current_period_start": current_period_start,
            "current_period_end": current_period_end,
            "cancel_at_period_end": subscription.get("cancel_at_period_end", False),
            "canceled_at": (
                datetime.fromtimestamp(subscription["canceled_at"])
                if subscription.get("canceled_at")
                else None
            ),
            "trial_start": (
                datetime.fromtimestamp(subscription["trial_start"])
                if subscription.get("trial_start")
                else None
            ),
            "trial_end": (
                datetime.fromtimestamp(subscription["trial_end"])
                if subscription.get("trial_end")
                else None
            ),
            "price_info": price_info,
        }

    @staticmethod
    def parse_checkout_session_event(event: dict[str, Any]) -> dict[str, Any]:
        """
        Parse checkout session data from webhook event.

        Evento: checkout.session.completed
        """
        session = event["data"]["object"]

        metadata = session.get("metadata", {})

        return {
            "session_id": session["id"],
            "customer_id": session.get("customer"),
            "subscription_id": session.get("subscription"),
            "email": session.get("customer_email") or session.get("customer_details", {}).get("email"),
            "name": session.get("customer_details", {}).get("name"),
            "mode": session.get("mode"),
            "payment_status": session.get("payment_status"),
            "status": session.get("status"),
            "plan": metadata.get("plan", "professional"),
            "product": metadata.get("product", "default"),
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "client_reference_id": session.get("client_reference_id"),
        }

    @staticmethod
    def parse_invoice_event(event: dict[str, Any]) -> dict[str, Any]:
        """
        Parse invoice data from webhook event.

        Eventos: invoice.paid, invoice.payment_failed
        """
        invoice = event["data"]["object"]

        return {
            "invoice_id": invoice["id"],
            "customer_id": invoice.get("customer"),
            "subscription_id": invoice.get("subscription"),
            "email": invoice.get("customer_email"),
            "status": invoice.get("status"),
            "amount_paid": invoice.get("amount_paid"),
            "amount_due": invoice.get("amount_due"),
            "currency": invoice.get("currency"),
            "paid": invoice.get("paid", False),
            "billing_reason": invoice.get("billing_reason"),
            "invoice_pdf": invoice.get("invoice_pdf"),
            "hosted_invoice_url": invoice.get("hosted_invoice_url"),
            "period_start": (
                datetime.fromtimestamp(invoice["period_start"])
                if invoice.get("period_start")
                else None
            ),
            "period_end": (
                datetime.fromtimestamp(invoice["period_end"])
                if invoice.get("period_end")
                else None
            ),
        }

    # ========================================================================
    # METODOS AUXILIARES
    # ========================================================================

    @staticmethod
    async def list_invoices(
        customer_id: str,
        limit: int = 10,
    ) -> list[stripe.Invoice]:
        """List invoices for a customer."""
        try:
            invoices = stripe.Invoice.list(
                customer=customer_id,
                limit=limit,
            )
            return list(invoices.data)
        except stripe.StripeError as e:
            logger.error("list_invoices_failed", customer_id=customer_id, error=str(e))
            raise

    @staticmethod
    async def get_upcoming_invoice(customer_id: str) -> stripe.Invoice | None:
        """Get the upcoming invoice for a customer."""
        try:
            return stripe.Invoice.upcoming(customer=customer_id)
        except stripe.InvalidRequestError:
            # No upcoming invoice
            return None
        except stripe.StripeError as e:
            logger.error("get_upcoming_invoice_failed", customer_id=customer_id, error=str(e))
            raise

    @staticmethod
    async def create_usage_record(
        subscription_item_id: str,
        quantity: int,
        timestamp: datetime | None = None,
        action: str = "increment",
    ) -> stripe.SubscriptionItem:
        """
        Record usage for metered billing.

        Usado se voce decidir cobrar por uso em vez de planos fixos.
        """
        try:
            usage_record = stripe.SubscriptionItem.create_usage_record(
                subscription_item_id,
                quantity=quantity,
                timestamp=int((timestamp or datetime.utcnow()).timestamp()),
                action=action,
            )
            logger.info(
                "usage_recorded",
                subscription_item_id=subscription_item_id,
                quantity=quantity,
            )
            return usage_record
        except stripe.StripeError as e:
            logger.error("create_usage_record_failed", error=str(e))
            raise
