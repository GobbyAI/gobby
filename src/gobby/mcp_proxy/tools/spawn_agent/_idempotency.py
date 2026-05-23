"""Helpers for idempotent spawn-agent retry responses."""

from __future__ import annotations

from typing import Any


def _string_attr(value: Any, name: str) -> str | None:
    attr = getattr(value, name, None)
    return attr if isinstance(attr, str) and attr else None


def active_task_spawn_response(active_run: Any, task_ref: str | None) -> dict[str, Any]:
    """Return an observable response for a task that already has an active run."""
    run_id = _string_attr(active_run, "id")
    return {
        "success": True,
        "skipped": True,
        "run_id": run_id,
        "status": _string_attr(active_run, "status"),
        "child_session_id": _string_attr(active_run, "child_session_id"),
        "parent_session_id": _string_attr(active_run, "parent_session_id"),
        "task_id": _string_attr(active_run, "task_id"),
        "agent_name": _string_attr(active_run, "agent_name"),
        "message": f"Agent already running for task {task_ref or 'unknown'}",
    }


def non_actionable_task_spawn_response(
    task: Any,
    *,
    task_ref: str | None,
    resolved_task_id: str,
) -> dict[str, Any]:
    """Return a pre-launch refusal for closed/escalated/done task state."""
    seq_num = getattr(task, "seq_num", None)
    display_ref = f"#{seq_num}" if isinstance(seq_num, int) else task_ref or resolved_task_id
    return {
        "success": False,
        "skipped": True,
        "task_id": resolved_task_id,
        "error": f"Task {display_ref} is not actionable; refusing to spawn agent",
    }
