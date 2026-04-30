"""Plan lifecycle event handlers."""

from __future__ import annotations

from typing import Any

from gobby.storage.plans import LocalPlanManager, PlanNotFoundError, PlanRecord

_TERMINAL_CLOSURE_REASONS = {"completed", "obsolete"}
_TERMINAL_LIFECYCLE_STAGES = {"closed-completed", "closed-obsolete"}


def on_epic_terminal(event: object, *, db: Any) -> PlanRecord | None:
    """Archive the plan linked to a terminal epic, when one exists."""
    if not _is_terminal_epic_event(event):
        return None

    task_ref = _event_value(event, "task_ref") or _event_value(event, "root_task_ref")
    if not isinstance(task_ref, str) or not task_ref.strip():
        return None

    project_id = _event_value(event, "project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        project_id = None

    try:
        return LocalPlanManager(db).archive_plan(
            task_ref,
            project_id=project_id,
            reason=_archive_reason(event),
        )
    except PlanNotFoundError:
        return None


def _is_terminal_epic_event(event: object) -> bool:
    lifecycle_stage = _event_value(event, "lifecycle_stage")
    if lifecycle_stage in _TERMINAL_LIFECYCLE_STAGES:
        return True

    status = _event_value(event, "status")
    closure_reason = _event_value(event, "closure_reason") or _event_value(event, "closed_reason")
    return status == "closed" and closure_reason in _TERMINAL_CLOSURE_REASONS


def _archive_reason(event: object) -> str:
    lifecycle_stage = _event_value(event, "lifecycle_stage")
    if isinstance(lifecycle_stage, str) and lifecycle_stage:
        return lifecycle_stage

    closure_reason = _event_value(event, "closure_reason") or _event_value(event, "closed_reason")
    if isinstance(closure_reason, str) and closure_reason:
        return f"closed-{closure_reason}"

    return "closed"


def _event_value(event: object, key: str) -> object | None:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


__all__ = ["on_epic_terminal"]
