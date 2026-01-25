"""
License management service
"""
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.license import License, LicenseStatus, PlanId, ProductType

logger = structlog.get_logger()


# Plan limits configuration
PLAN_LIMITS = {
    PlanId.FREE: {"operations_per_day": 50, "users": 1},
    PlanId.PROFESSIONAL: {"operations_per_day": -1, "users": 1},  # -1 = unlimited
    PlanId.OFFICE: {"operations_per_day": -1, "users": 5},
    PlanId.ENTERPRISE: {"operations_per_day": -1, "users": -1},
}


class LicenseService:
    """Service for managing licenses."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_email(
        self,
        email: str,
        product: ProductType,
    ) -> License | None:
        """Get a license by email and product."""
        stmt = select(License).where(
            License.email == email,
            License.product == product,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_stripe_customer(self, customer_id: str) -> License | None:
        """Get a license by Stripe customer ID."""
        stmt = select(License).where(License.stripe_customer_id == customer_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_stripe_subscription(self, subscription_id: str) -> License | None:
        """Get a license by Stripe subscription ID."""
        stmt = select(License).where(License.stripe_subscription_id == subscription_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_trial(
        self,
        email: str,
        product: ProductType,
    ) -> License:
        """Create a new trial license."""
        # Check if license already exists
        existing = await self.get_by_email(email, product)
        if existing:
            raise ValueError(f"License already exists for {email} - {product.value}")

        # Calculate trial end date
        trial_end = datetime.utcnow() + timedelta(days=settings.trial_days)

        license = License(
            email=email,
            product=product,
            plan=PlanId.FREE,
            status=LicenseStatus.TRIALING,
            current_period_start=datetime.utcnow(),
            current_period_end=trial_end,
        )

        self.db.add(license)
        await self.db.flush()

        logger.info(
            "trial_license_created",
            email=email,
            product=product.value,
            license_id=license.id,
        )

        return license

    async def create_or_update_from_stripe(
        self,
        email: str,
        customer_id: str,
        subscription_id: str,
        status: str,
        plan: str,
        product: str,
        current_period_start: datetime,
        current_period_end: datetime,
        cancel_at_period_end: bool = False,
        canceled_at: datetime | None = None,
    ) -> License:
        """Create or update a license from Stripe webhook data."""
        # Try to find existing license
        license = await self.get_by_stripe_subscription(subscription_id)

        if not license:
            # Try by email and product
            product_type = ProductType(product)
            license = await self.get_by_email(email, product_type)

        if license:
            # Update existing license
            license.stripe_customer_id = customer_id
            license.stripe_subscription_id = subscription_id
            license.status = LicenseStatus(status)
            license.plan = PlanId(plan)
            license.current_period_start = current_period_start
            license.current_period_end = current_period_end
            license.cancel_at_period_end = cancel_at_period_end
            license.canceled_at = canceled_at
            license.updated_at = datetime.utcnow()

            logger.info(
                "license_updated_from_stripe",
                email=email,
                license_id=license.id,
                status=status,
                plan=plan,
            )
        else:
            # Create new license
            license = License(
                email=email,
                product=ProductType(product),
                plan=PlanId(plan),
                status=LicenseStatus(status),
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                current_period_start=current_period_start,
                current_period_end=current_period_end,
                cancel_at_period_end=cancel_at_period_end,
                canceled_at=canceled_at,
            )
            self.db.add(license)

            logger.info(
                "license_created_from_stripe",
                email=email,
                product=product,
                plan=plan,
                status=status,
            )

        await self.db.flush()
        return license

    async def validate(
        self,
        email: str,
        product: ProductType,
    ) -> dict:
        """Validate a license and return validation result."""
        license = await self.get_by_email(email, product)

        if not license:
            return {
                "valid": False,
                "license": None,
                "plan": None,
                "message": "Licenca nao encontrada. Inicie seu teste gratuito.",
                "can_start_trial": True,
            }

        # Check if license is active
        now = datetime.utcnow()
        is_expired = license.current_period_end < now

        if not license.is_active or is_expired:
            return {
                "valid": False,
                "license": self._license_to_dict(license),
                "plan": license.plan.value,
                "message": self._get_status_message(license.status, is_expired),
                "can_start_trial": False,
            }

        # Calculate remaining days
        days_remaining = (license.current_period_end - now).days

        # Get plan limits
        limits = PLAN_LIMITS.get(license.plan, PLAN_LIMITS[PlanId.FREE])

        return {
            "valid": True,
            "license": self._license_to_dict(license),
            "plan": license.plan.value,
            "limits": limits,
            "days_remaining": days_remaining,
            "message": self._get_success_message(license, days_remaining),
            "can_start_trial": False,
        }

    def _license_to_dict(self, license: License) -> dict:
        """Convert license to dictionary."""
        return {
            "id": license.id,
            "email": license.email,
            "plan": license.plan.value,
            "product": license.product.value,
            "status": license.status.value,
            "current_period_start": license.current_period_start.isoformat(),
            "current_period_end": license.current_period_end.isoformat(),
            "cancel_at_period_end": license.cancel_at_period_end,
        }

    def _get_status_message(self, status: LicenseStatus, is_expired: bool) -> str:
        """Get message for license status."""
        if is_expired:
            return "Sua licenca expirou. Renove para continuar usando."

        messages = {
            LicenseStatus.PAST_DUE: "Pagamento pendente. Atualize seu metodo de pagamento.",
            LicenseStatus.CANCELED: "Assinatura cancelada.",
            LicenseStatus.UNPAID: "Pagamento nao realizado. Verifique sua forma de pagamento.",
            LicenseStatus.INCOMPLETE: "Pagamento incompleto. Finalize sua assinatura.",
            LicenseStatus.INCOMPLETE_EXPIRED: "Sua assinatura expirou. Crie uma nova.",
            LicenseStatus.PAUSED: "Assinatura pausada.",
        }

        return messages.get(status, "Licenca invalida.")

    def _get_success_message(self, license: License, days_remaining: int) -> str:
        """Get success message for active license."""
        if license.status == LicenseStatus.TRIALING:
            return f"Teste gratuito: {days_remaining} dias restantes"

        if license.cancel_at_period_end:
            return f"Assinatura ativa. Cancela em {days_remaining} dias."

        return "Licenca ativa"
