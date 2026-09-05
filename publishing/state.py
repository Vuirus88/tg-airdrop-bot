"""Database state transitions for safe, repeatable publication."""
from sqlalchemy import update

from db.models import Project, ProjectStatus


async def claim_project_for_publication(session, project_id: int) -> bool:
    """Atomically claim a review item so bot and web cannot publish it twice."""
    result = await session.execute(
        update(Project)
        .where(
            Project.id == project_id,
            Project.status.in_([ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED]),
        )
        .values(status=ProjectStatus.APPROVED)
    )
    return result.rowcount == 1


async def finish_project_publication(
    session, project_id: int, telegram_success: bool
) -> None:
    project = await session.get(Project, project_id)
    if project:
        project.status = (
            ProjectStatus.PUBLISHED
            if telegram_success
            else ProjectStatus.APPROVED
        )
        await session.commit()


async def archive_project_for_review(session, project_id: int) -> bool:
    """Archive only an unpublished review item; stale buttons cannot delete history."""
    result = await session.execute(
        update(Project)
        .where(
            Project.id == project_id,
            Project.status.in_([ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED]),
        )
        .values(status=ProjectStatus.DELETED)
    )
    return result.rowcount == 1
