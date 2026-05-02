"""Plan event handlers."""

from __future__ import annotations

from typing import Any

from gobby.storage.plans import LocalPlanManager, PlanNotFoundError, PlanRecord

_TERMINAL_CLOSURE_REASONS = {"completed", "obsolete"}


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
    except (FileNotFoundError, PlanNotFoundError):
        return None


def _is_terminal_epic_event(event: object) -> bool:
    event_type = _event_value(event, "event_type") or _event_value(event, "type")
    closure_reason = _event_value(event, "closure_reason") or _event_value(event, "closed_reason")
    return event_type == "task_closed" and closure_reason in _TERMINAL_CLOSURE_REASONS


def _archive_reason(event: object) -> str:
    closure_reason = _event_value(event, "closure_reason") or _event_value(event, "closed_reason")
    if isinstance(closure_reason, str) and closure_reason:
        return f"closed-{closure_reason}"

    return "closed"


def _event_value(event: object, key: str) -> object | None:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


__all__ = ["on_epic_terminal"]
