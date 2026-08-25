"""Classification helpers for post-close task-memory review rules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

_WORK_CATEGORIES = frozenset({"code", "config", "docs", "refactor", "test"})
_NO_COMMIT_CATEGORIES = frozenset({"manual", "planning", "research"})
_EXEMPT_REASONS = frozenset(
    {"duplicate", "already_implemented", "wont_fix", "obsolete", "out_of_repo"}
)


class TaskLookup(Protocol):
    def get_task(self, task_id: str) -> Any: ...

    def list_tasks(self, *, parent_task_id: str, limit: int = 50) -> list[Any]: ...


def _close_payload(event_data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    output = event_data.get("tool_output")
    if not isinstance(output, Mapping):
        return None
    nested = output.get("result")
    if isinstance(nested, Mapping) and nested.get("closed") is True:
        return nested
    return output


def _closure_id(task: Any) -> str | None:
    task_id = getattr(task, "id", None)
    closed_at = getattr(task, "closed_at", None)
    if not isinstance(task_id, str) or closed_at is None:
        return None
    return f"{task_id}:{closed_at.isoformat()}"


def classify_memory_review_close(
    task_manager: TaskLookup | None,
    event_data: Mapping[str, Any],
    tool_input: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return queue data for one completed worked leaf, else ``None``."""
    if task_manager is None:
        return None
    payload = _close_payload(event_data)
    if payload is None or payload.get("closed") is not True:
        return None
    if payload.get("success") is False:
        return None

    summary = str(tool_input.get("changes_summary") or "").strip()
    if not summary:
        return None
    task_id = payload.get("task_id") or tool_input.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    try:
        task = task_manager.get_task(task_id)
    except (LookupError, ValueError):
        return None
    if task is None:
        return None

    reason = str(getattr(task, "closed_reason", None) or tool_input.get("reason") or "completed")
    reason = reason.casefold()
    if reason != "completed" or reason in _EXEMPT_REASONS:
        return None
    if str(getattr(task, "task_type", "")).casefold() == "epic":
        return None
    try:
        if task_manager.list_tasks(parent_task_id=task.id, limit=1):
            return None
    except (LookupError, ValueError):
        return None

    category = str(getattr(task, "category", "") or "").casefold()
    if category in _WORK_CATEGORIES:
        payload_commits = payload.get("commit_shas")
        has_commits = bool(getattr(task, "commits", None)) or (
            isinstance(payload_commits, list) and bool(payload_commits)
        )
        if not has_commits:
            return None
    elif category not in _NO_COMMIT_CATEGORIES:
        return None

    closure_id = _closure_id(task)
    if closure_id is None:
        return None
    task_ref = f"#{task.seq_num}" if getattr(task, "seq_num", None) else task.id
    return {
        "closure_id": closure_id,
        "task_id": task.id,
        "task_ref": task_ref,
        "changes_summary": summary,
    }


def queue_memory_review_close(
    task_manager: TaskLookup | None,
    event_data: Mapping[str, Any],
    tool_input: Mapping[str, Any],
    variables: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Append one classified closure while preserving delivery deduplication."""
    stored = variables.get("_memory_pending_task_reviews", [])
    pending = [dict(item) for item in stored if isinstance(item, Mapping)]
    candidate = classify_memory_review_close(task_manager, event_data, tool_input)
    if candidate is None:
        return pending

    closure_id = candidate["closure_id"]
    if any(item.get("closure_id") == closure_id for item in pending):
        return pending
    reviewed = variables.get("_memory_task_review_records", [])
    if isinstance(reviewed, list) and any(
        isinstance(item, Mapping) and item.get("closure_id") == closure_id for item in reviewed
    ):
        return pending
    return [*pending, candidate]
