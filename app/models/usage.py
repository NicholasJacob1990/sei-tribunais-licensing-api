"""
Usage tracking model for monitoring API usage per license
"""
from datetime import datetime, date
from uuid import uuid4

from sqlalchemy import DateTime, Date, Integer, String, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UsageRecord(Base):
    """
    Usage record for tracking daily operations per license.

    Tracks the number of operations performed each day to enforce
    rate limits based on the subscription plan.
    """

    __tablename__ = "usage_records"

    # Primary key
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    # License reference
    license_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("licenses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Usage date
    usage_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        default=date.today,
    )

    # Operation counts
    operations_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Operation type breakdown (optional, for analytics)
    search_operations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    download_operations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    automation_operations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Product tracking
    product: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="tribunais-mcp",
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
    license: Mapped["License"] = relationship(
        "License",
        back_populates="usage_records",
    )

    # Indexes for efficient queries
    __table_args__ = (
        Index("idx_usage_license_date", "license_id", "usage_date", unique=True),
        Index("idx_usage_date", "usage_date"),
    )

    def __repr__(self) -> str:
        return f"<UsageRecord {self.license_id} - {self.usage_date}: {self.operations_count} ops>"

    def increment(self, count: int = 1, operation_type: str | None = None) -> None:
        """Increment the operation count."""
        self.operations_count += count

        if operation_type == "search":
            self.search_operations += count
        elif operation_type == "download":
            self.download_operations += count
        elif operation_type == "automation":
            self.automation_operations += count


# Import here to avoid circular imports
from app.models.license import License
