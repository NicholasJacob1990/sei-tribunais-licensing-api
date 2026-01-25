"""
License model for storing subscription information
"""
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import DateTime, Enum as SQLEnum, String, Boolean, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.usage import UsageRecord


class LicenseStatus(str, Enum):
    """License status enum matching Stripe subscription status."""
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    UNPAID = "unpaid"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAUSED = "paused"


class ProductType(str, Enum):
    """Product type enum."""
    SEI_MCP = "sei-mcp"
    TRIBUNAIS_MCP = "tribunais-mcp"
    BUNDLE = "bundle"


class PlanId(str, Enum):
    """Plan ID enum."""
    FREE = "free"
    PROFESSIONAL = "professional"
    OFFICE = "office"
    ENTERPRISE = "enterprise"


class License(Base):
    """
    License model representing a user's subscription.

    Stores Stripe customer and subscription IDs for managing billing,
    along with current plan and status information.
    """

    __tablename__ = "licenses"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # User identification
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Product and plan
    product: Mapped[ProductType] = mapped_column(
        SQLEnum(ProductType),
        nullable=False,
        default=ProductType.TRIBUNAIS_MCP,
    )
    plan: Mapped[PlanId] = mapped_column(
        SQLEnum(PlanId),
        nullable=False,
        default=PlanId.FREE,
    )

    # Status
    status: Mapped[LicenseStatus] = mapped_column(
        SQLEnum(LicenseStatus),
        nullable=False,
        default=LicenseStatus.TRIALING,
    )

    # Stripe references
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        index=True,
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
        index=True,
    )

    # Billing period
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # Cancellation
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # Relationships
    usage_records: Mapped[list["UsageRecord"]] = relationship(
        "UsageRecord",
        back_populates="license",
        cascade="all, delete-orphan",
    )

    # Indexes
    __table_args__ = (
        Index("idx_license_email_product", "email", "product"),
        Index("idx_license_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<License {self.email} - {self.plan.value} ({self.status.value})>"

    @property
    def is_active(self) -> bool:
        """Check if the license is currently active."""
        return self.status in (LicenseStatus.ACTIVE, LicenseStatus.TRIALING)

    @property
    def is_trial(self) -> bool:
        """Check if the license is in trial period."""
        return self.status == LicenseStatus.TRIALING

    @property
    def days_remaining(self) -> int:
        """Calculate days remaining in the current period."""
        if self.current_period_end:
            delta = self.current_period_end - datetime.utcnow()
            return max(0, delta.days)
        return 0
