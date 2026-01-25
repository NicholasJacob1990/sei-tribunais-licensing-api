"""
Stripe webhook endpoints for handling subscription events
"""
import stripe
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.database import get_db
from app.services.stripe_service import StripeService
from app.services.license_service import LicenseService

logger = structlog.get_logger()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Handle Stripe webhook events.

    This endpoint receives events from Stripe about subscription changes
    and updates the local license database accordingly.
    """
    # Get raw body for signature verification
    payload = await request.body()
    signature = request.headers.get("stripe-signature")

    if not signature:
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
    logger.info("webhook_received", event_type=event_type)

    license_service = LicenseService(db)

    try:
        # Handle subscription events
        if event_type == "customer.subscription.created":
            await handle_subscription_created(event, license_service)

        elif event_type == "customer.subscription.updated":
            await handle_subscription_updated(event, license_service)

        elif event_type == "customer.subscription.deleted":
            await handle_subscription_deleted(event, license_service)

        elif event_type == "customer.subscription.trial_will_end":
            await handle_trial_will_end(event, license_service)

        elif event_type == "invoice.payment_succeeded":
            await handle_payment_succeeded(event, license_service)

        elif event_type == "invoice.payment_failed":
            await handle_payment_failed(event, license_service)

        elif event_type == "checkout.session.completed":
            await handle_checkout_completed(event, license_service)

        else:
            logger.info("webhook_event_ignored", event_type=event_type)

        return {"status": "ok", "event_type": event_type}

    except Exception as e:
        logger.error("webhook_processing_error", event_type=event_type, error=str(e))
        raise HTTPException(status_code=500, detail=f"Error processing webhook: {str(e)}")


async def handle_subscription_created(event: dict, service: LicenseService) -> None:
    """Handle new subscription creation."""
    data = StripeService.parse_subscription_event(event)

    # Get customer email
    customer = stripe.Customer.retrieve(data["customer_id"])
    email = customer.email

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


async def handle_subscription_updated(event: dict, service: LicenseService) -> None:
    """Handle subscription updates (plan changes, renewals, etc.)."""
    data = StripeService.parse_subscription_event(event)

    # Get customer email
    customer = stripe.Customer.retrieve(data["customer_id"])
    email = customer.email

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
    )


async def handle_subscription_deleted(event: dict, service: LicenseService) -> None:
    """Handle subscription cancellation."""
    data = StripeService.parse_subscription_event(event)

    license = await service.get_by_stripe_subscription(data["subscription_id"])
    if license:
        from app.models.license import LicenseStatus
        license.status = LicenseStatus.CANCELED
        license.canceled_at = data["canceled_at"]

        logger.info(
            "subscription_deleted",
            email=license.email,
            subscription_id=data["subscription_id"],
        )


async def handle_trial_will_end(event: dict, service: LicenseService) -> None:
    """Handle trial ending notification (3 days before)."""
    subscription = event["data"]["object"]
    customer_id = subscription["customer"]

    license = await service.get_by_stripe_customer(customer_id)
    if license:
        # Could trigger email notification here
        logger.info(
            "trial_will_end",
            email=license.email,
            days_remaining=3,
        )


async def handle_payment_succeeded(event: dict, service: LicenseService) -> None:
    """Handle successful payment."""
    invoice = event["data"]["object"]
    customer_id = invoice["customer"]

    license = await service.get_by_stripe_customer(customer_id)
    if license:
        logger.info(
            "payment_succeeded",
            email=license.email,
            amount=invoice.get("amount_paid"),
        )


async def handle_payment_failed(event: dict, service: LicenseService) -> None:
    """Handle failed payment."""
    invoice = event["data"]["object"]
    customer_id = invoice["customer"]

    license = await service.get_by_stripe_customer(customer_id)
    if license:
        from app.models.license import LicenseStatus
        license.status = LicenseStatus.PAST_DUE

        logger.warning(
            "payment_failed",
            email=license.email,
            amount=invoice.get("amount_due"),
        )


async def handle_checkout_completed(event: dict, service: LicenseService) -> None:
    """Handle completed checkout session."""
    session = event["data"]["object"]

    if session.get("mode") == "subscription":
        customer_id = session["customer"]
        subscription_id = session["subscription"]
        email = session.get("customer_email") or session.get("customer_details", {}).get("email")

        metadata = session.get("metadata", {})

        logger.info(
            "checkout_completed",
            email=email,
            subscription_id=subscription_id,
            product=metadata.get("product"),
            plan=metadata.get("plan"),
        )
