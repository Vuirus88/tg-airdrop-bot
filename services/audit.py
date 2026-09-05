"""Small append-only audit helper shared by bot and web workflows."""
import logging

from db.models import AuditEvent

logger = logging.getLogger(__name__)


def add_audit_event(
    session,
    action: str,
    *,
    project_id: int | None = None,
    actor_type: str = "system",
    actor_id: str | int | None = None,
    success: bool = True,
    detail: str | None = None,
    workspace_id: int = 1,
) -> None:
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            project_id=project_id,
            actor_type=actor_type,
            actor_id=str(actor_id) if actor_id is not None else None,
            action=action,
            success=success,
            detail=(detail or "")[:2000] or None,
        )
    )
