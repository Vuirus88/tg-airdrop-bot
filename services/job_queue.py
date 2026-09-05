"""Small SQLite-backed job queue for the single-process free-first deployment."""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import BackgroundJob


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_job(
    session,
    job_type: str,
    payload: dict,
    *,
    workspace_id: int = 1,
    max_attempts: int = 3,
) -> BackgroundJob:
    job = BackgroundJob(
        workspace_id=workspace_id,
        job_type=job_type,
        payload_json=json.dumps(payload, ensure_ascii=False),
        status="queued",
        max_attempts=max(1, max_attempts),
    )
    session.add(job)
    await session.flush()
    return job


async def claim_next_job(session) -> BackgroundJob | None:
    """Claim one due job; the MVP has one worker, so SQLite locking is sufficient."""
    now = utcnow()
    result = await session.execute(
        select(BackgroundJob)
        .where(
            BackgroundJob.status == "queued",
            BackgroundJob.available_at <= now,
        )
        .order_by(BackgroundJob.created_at)
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if not job:
        return None
    job.status = "running"
    job.attempts += 1
    job.locked_at = now
    await session.commit()
    return job


async def complete_job(session, job_id: int) -> None:
    job = await session.get(BackgroundJob, job_id)
    if not job:
        return
    job.status = "completed"
    job.completed_at = utcnow()
    job.locked_at = None
    await session.commit()


async def fail_job(session, job_id: int, error: str) -> bool:
    """Retry with backoff while attempts remain; return whether it was requeued."""
    job = await session.get(BackgroundJob, job_id)
    if not job:
        return False
    job.last_error = error[:2000]
    job.locked_at = None
    if job.attempts < job.max_attempts:
        job.status = "queued"
        job.available_at = utcnow() + timedelta(seconds=min(60, 2 ** job.attempts))
        requeued = True
    else:
        job.status = "failed"
        requeued = False
    await session.commit()
    return requeued


async def recover_stale_jobs(max_age_minutes: int = 15) -> int:
    """Return jobs left running after a process crash back to the queue."""
    from db.database import get_session

    cutoff = utcnow() - timedelta(minutes=max_age_minutes)
    recovered = 0
    async with get_session() as session:
        result = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.status == "running",
                BackgroundJob.locked_at < cutoff,
            )
        )
        for job in result.scalars():
            job.status = "queued"
            job.locked_at = None
            job.available_at = utcnow()
            recovered += 1
        if recovered:
            await session.commit()
    return recovered
