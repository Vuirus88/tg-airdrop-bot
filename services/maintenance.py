"""Conservative cleanup of technical records; content history is preserved."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from config import settings
from db.database import get_session
from db.models import AuditEvent, BackgroundJob, ReviewRequest


async def cleanup_old_data() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    removed = {"jobs": 0, "review_requests": 0, "audit_events": 0}
    async with get_session() as session:
        result = await session.execute(
            delete(BackgroundJob).where(
                BackgroundJob.status.in_(["completed", "failed"]),
                BackgroundJob.updated_at < now - timedelta(days=settings.JOB_RETENTION_DAYS),
            )
        )
        removed["jobs"] = result.rowcount or 0

        result = await session.execute(
            delete(ReviewRequest).where(
                ReviewRequest.status.in_(["completed", "cancelled"]),
                ReviewRequest.updated_at < now - timedelta(days=settings.REVIEW_REQUEST_RETENTION_DAYS),
            )
        )
        removed["review_requests"] = result.rowcount or 0

        result = await session.execute(
            delete(AuditEvent).where(
                AuditEvent.created_at < now - timedelta(days=settings.AUDIT_RETENTION_DAYS)
            )
        )
        removed["audit_events"] = result.rowcount or 0
        await session.commit()
    return removed
