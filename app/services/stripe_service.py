"""
Stripe integration service for payment processing
"""
import stripe
from datetime import datetime
from typing import Any

import structlog

from app.config import settings
from app.models.license import PlanId, ProductType

logger = structlog.get_logger()

# Initialize Stripe
stripe.api_key = settings.stripe_secret_key


# Price IDs mapping - replace with actual Stripe price IDs
PRICE_IDS: dict[str, dict[str, str]] = {
    "tribunais-mcp": {
        "professional_monthly": "price_tribunais_pro_monthly",
        "professional_yearly": "price_tribunais_pro_yearly",
        "office_monthly": "price_tribunais_office_monthly",
        "office_yearly": "price_tribunais_office_yearly",
    },
    "sei-mcp": {
        "professional_monthly": "price_sei_pro_monthly",
        "professional_yearly": "price_sei_pro_yearly",
        "office_monthly": "price_sei_office_monthly",
        "office_yearly": "price_sei_office_yearly",
    },
    "bundle": {
        "professional_monthly": "price_bundle_pro_monthly",
        "professional_yearly": "price_bundle_pro_yearly",
        "office_monthly": "price_bundle_office_monthly",
        "office_yearly": "price_bundle_office_yearly",
    },
}


class StripeService:
    """Service for Stripe payment operations."""

    @staticmethod
    async def create_customer(email: str, name: str | None = None) -> stripe.Customer:
        """Create a new Stripe customer."""
        try:
            customer = stripe.Customer.create(
                email=email,
                name=name,
                metadata={"source": "iudex-licensing"},
            )
            logger.info("stripe_customer_created", email=email, customer_id=customer.id)
            return customer
        except stripe.StripeError as e:
            logger.error("stripe_customer_creation_failed", email=email, error=str(e))
            raise

    @staticmethod
    async def get_or_create_customer(email: str) -> stripe.Customer:
        """Get existing customer or create a new one."""
        try:
            # Search for existing customer
            customers = stripe.Customer.search(query=f'email:"{email}"')
            if customers.data:
                return customers.data[0]

            # Create new customer if not found
            return await StripeService.create_customer(email)
        except stripe.StripeError as e:
            logger.error("stripe_get_customer_failed", email=email, error=str(e))
            raise

    @staticmethod
    def get_price_id(product: ProductType, plan: PlanId, interval: str) -> str | None:
        """Get the Stripe price ID for a product/plan combination."""
        product_prices = PRICE_IDS.get(product.value, {})
        price_key = f"{plan.value}_{interval}"
        return product_prices.get(price_key)

    @staticmethod
    async def create_checkout_session(
        email: str,
        product: ProductType,
        plan: PlanId,
        interval: str,
        success_url: str,
        cancel_url: str,
        client_reference_id: str | None = None,
    ) -> stripe.checkout.Session:
        """Create a Stripe Checkout session for subscription."""
        try:
            # Get or create customer
            customer = await StripeService.get_or_create_customer(email)

            # Get price ID
            price_id = StripeService.get_price_id(product, plan, interval)
            if not price_id:
                raise ValueError(f"No price configured for {product.value}/{plan.value}/{interval}")

            # Create checkout session
            session = stripe.checkout.Session.create(
                customer=customer.id,
                mode="subscription",
                payment_method_types=["card", "boleto"],
                line_items=[
                    {
                        "price": price_id,
                        "quantity": 1,
                    }
                ],
                success_url=success_url,
                cancel_url=cancel_url,
                client_reference_id=client_reference_id,
                subscription_data={
                    "trial_period_days": settings.trial_days if plan != PlanId.FREE else None,
                    "metadata": {
                        "product": product.value,
                        "plan": plan.value,
                    },
                },
                metadata={
                    "product": product.value,
                    "plan": plan.value,
                },
                allow_promotion_codes=True,
                billing_address_collection="required",
                locale="pt-BR",
            )

            logger.info(
                "checkout_session_created",
                email=email,
                product=product.value,
                plan=plan.value,
                session_id=session.id,
            )
            return session

        except stripe.StripeError as e:
            logger.error(
                "checkout_session_failed",
                email=email,
                product=product.value,
                plan=plan.value,
                error=str(e),
            )
            raise

    @staticmethod
    async def create_portal_session(
        customer_id: str,
        return_url: str,
    ) -> stripe.billing_portal.Session:
        """Create a Stripe Customer Portal session."""
        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url,
            )
            logger.info("portal_session_created", customer_id=customer_id)
            return session
        except stripe.StripeError as e:
            logger.error("portal_session_failed", customer_id=customer_id, error=str(e))
            raise

    @staticmethod
    async def get_subscription(subscription_id: str) -> stripe.Subscription:
        """Get subscription details from Stripe."""
        try:
            return stripe.Subscription.retrieve(subscription_id)
        except stripe.StripeError as e:
            logger.error("get_subscription_failed", subscription_id=subscription_id, error=str(e))
            raise

    @staticmethod
    async def cancel_subscription(
        subscription_id: str,
        at_period_end: bool = True,
    ) -> stripe.Subscription:
        """Cancel a subscription."""
        try:
            if at_period_end:
                subscription = stripe.Subscription.modify(
                    subscription_id,
                    cancel_at_period_end=True,
                )
            else:
                subscription = stripe.Subscription.cancel(subscription_id)

            logger.info(
                "subscription_canceled",
                subscription_id=subscription_id,
                at_period_end=at_period_end,
            )
            return subscription
        except stripe.StripeError as e:
            logger.error("cancel_subscription_failed", subscription_id=subscription_id, error=str(e))
            raise

    @staticmethod
    def verify_webhook_signature(payload: bytes, signature: str) -> dict[str, Any]:
        """Verify and parse a Stripe webhook event."""
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
        """Parse subscription data from a webhook event."""
        subscription = event["data"]["object"]

        # Extract period dates
        current_period_start = datetime.fromtimestamp(subscription["current_period_start"])
        current_period_end = datetime.fromtimestamp(subscription["current_period_end"])

        # Get metadata
        metadata = subscription.get("metadata", {})

        return {
            "subscription_id": subscription["id"],
            "customer_id": subscription["customer"],
            "status": subscription["status"],
            "plan": metadata.get("plan", "professional"),
            "product": metadata.get("product", "tribunais-mcp"),
            "current_period_start": current_period_start,
            "current_period_end": current_period_end,
            "cancel_at_period_end": subscription.get("cancel_at_period_end", False),
            "canceled_at": (
                datetime.fromtimestamp(subscription["canceled_at"])
                if subscription.get("canceled_at")
                else None
            ),
        }
