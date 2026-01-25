"""
Usage tracking service for monitoring API operations
"""
from datetime import date, datetime
from typing import Optional

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.license import License, PlanId
from app.models.usage import UsageRecord

logger = structlog.get_logger()


# Plan limits
PLAN_LIMITS = {
    PlanId.FREE: 50,
    PlanId.PROFESSIONAL: -1,  # unlimited
    PlanId.OFFICE: -1,
    PlanId.ENTERPRISE: -1,
}


class UsageService:
    """Service for tracking and managing usage records."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_today_usage(self, license_id: str) -> UsageRecord | None:
        """Get today's usage record for a license."""
        today = date.today()
        stmt = select(UsageRecord).where(
            UsageRecord.license_id == license_id,
            UsageRecord.usage_date == today,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create_today_usage(
        self,
        license_id: str,
        product: str,
    ) -> UsageRecord:
        """Get or create today's usage record."""
        usage = await self.get_today_usage(license_id)

        if not usage:
            usage = UsageRecord(
                license_id=license_id,
                usage_date=date.today(),
                operations_count=0,
                product=product,
            )
            self.db.add(usage)
            await self.db.flush()

        return usage

    async def record_operation(
        self,
        license_id: str,
        product: str,
        operation_type: str | None = None,
        count: int = 1,
    ) -> dict:
        """Record an operation and check limits."""
        # Get license to check plan
        stmt = select(License).where(License.id == license_id)
        result = await self.db.execute(stmt)
        license = result.scalar_one_or_none()

        if not license:
            return {
                "allowed": False,
                "reason": "License not found",
                "remaining": 0,
            }

        # Get plan limit
        limit = PLAN_LIMITS.get(license.plan, 50)

        # Get or create usage record
        usage = await self.get_or_create_today_usage(license_id, product)

        # Check if unlimited
        if limit == -1:
            usage.increment(count, operation_type)
            await self.db.flush()
            return {
                "allowed": True,
                "remaining": -1,
                "used_today": usage.operations_count,
            }

        # Check limit
        if usage.operations_count >= limit:
            return {
                "allowed": False,
                "reason": f"Daily limit of {limit} operations reached",
                "remaining": 0,
                "used_today": usage.operations_count,
                "limit": limit,
            }

        # Record operation
        usage.increment(count, operation_type)
        await self.db.flush()

        remaining = max(0, limit - usage.operations_count)

        logger.info(
            "operation_recorded",
            license_id=license_id,
            operation_type=operation_type,
            used_today=usage.operations_count,
            remaining=remaining,
        )

        return {
            "allowed": True,
            "remaining": remaining,
            "used_today": usage.operations_count,
            "limit": limit,
        }

    async def check_limit(self, license_id: str) -> dict:
        """Check current usage against limit without recording."""
        # Get license
        stmt = select(License).where(License.id == license_id)
        result = await self.db.execute(stmt)
        license = result.scalar_one_or_none()

        if not license:
            return {
                "allowed": False,
                "reason": "License not found",
            }

        # Get plan limit
        limit = PLAN_LIMITS.get(license.plan, 50)

        # Get today's usage
        usage = await self.get_today_usage(license_id)
        used_today = usage.operations_count if usage else 0

        if limit == -1:
            return {
                "allowed": True,
                "remaining": -1,
                "used_today": used_today,
                "unlimited": True,
            }

        remaining = max(0, limit - used_today)

        return {
            "allowed": remaining > 0,
            "remaining": remaining,
            "used_today": used_today,
            "limit": limit,
            "unlimited": False,
        }

    async def get_usage_stats(
        self,
        license_id: str,
        days: int = 30,
    ) -> list[dict]:
        """Get usage statistics for the last N days."""
        from_date = date.today() - timedelta(days=days)

        stmt = (
            select(UsageRecord)
            .where(
                UsageRecord.license_id == license_id,
                UsageRecord.usage_date >= from_date,
            )
            .order_by(UsageRecord.usage_date.desc())
        )
        result = await self.db.execute(stmt)
        records = result.scalars().all()

        return [
            {
                "date": record.usage_date.isoformat(),
                "total": record.operations_count,
                "search": record.search_operations,
                "download": record.download_operations,
                "automation": record.automation_operations,
            }
            for record in records
        ]

    async def get_total_usage(self, license_id: str) -> int:
        """Get total operations for a license."""
        stmt = select(func.sum(UsageRecord.operations_count)).where(
            UsageRecord.license_id == license_id
        )
        result = await self.db.execute(stmt)
        total = result.scalar()
        return total or 0


from datetime import timedelta
