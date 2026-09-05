"""Persistent review queue and terminal archive operations."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from db.models import Draft, Project, ProjectStatus, PublishedPost, ReviewRequest


PENDING_STATUSES = (ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED)


async def pending_projects(session) -> list[Project]:
    result = await session.execute(
        select(Project)
        .options(selectinload(Project.drafts))
        .where(Project.status.in_(PENDING_STATUSES))
        .order_by(Project.created_at, Project.id)
    )
    return list(result.scalars().all())


def queue_position(projects: list[Project], project_id: int) -> tuple[int, int] | None:
    for index, project in enumerate(projects):
        if project.id == project_id:
            return index + 1, len(projects)
    return None


def adjacent_project_id(projects: list[Project], project_id: int, direction: int) -> int | None:
    if not projects:
        return None
    current = next((index for index, project in enumerate(projects) if project.id == project_id), None)
    if current is None:
        return projects[0].id
    target = current + direction
    if target < 0 or target >= len(projects):
        return None
    return projects[target].id


async def archived_projects(session, kind: str) -> list[Project]:
    status = ProjectStatus.PUBLISHED if kind == "published" else ProjectStatus.DELETED
    result = await session.execute(
        select(Project)
        .options(selectinload(Project.drafts))
        .where(Project.status == status)
        .order_by(Project.updated_at.desc(), Project.id.desc())
    )
    return list(result.scalars().all())


async def clear_archive(session, kind: str) -> int:
    """Delete terminal archive rows while retaining active queue items."""
    status = ProjectStatus.PUBLISHED if kind == "published" else ProjectStatus.DELETED
    project_ids = list(
        (await session.execute(select(Project.id).where(Project.status == status))).scalars().all()
    )
    if not project_ids:
        return 0
    await session.execute(delete(PublishedPost).where(PublishedPost.project_id.in_(project_ids)))
    await session.execute(delete(Draft).where(Draft.project_id.in_(project_ids)))
    await session.execute(delete(ReviewRequest).where(ReviewRequest.project_id.in_(project_ids)))
    result = await session.execute(delete(Project).where(Project.id.in_(project_ids)))
    return result.rowcount or 0
