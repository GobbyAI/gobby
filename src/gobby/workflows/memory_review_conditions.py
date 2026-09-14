"""Classification helpers for post-close task-memory review rules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

# Category only decides whether commits are required. ``VALID_CATEGORIES``
# covers every non-null value, so any other category (including an unset one)
# queues without a commit requirement.
_WORK_CATEGORIES = frozenset({"code", "config", "docs", "refactor", "test"})


class TaskLookup(Protocol):
    def get_task(self, task_id: str) -> Any: ...

    def list_tasks(self, *, parent_task_id: str, limit: int = 50) -> list[Any]: ...


def _closure_id(task: Any) -> str | None:
    task_id = getattr(task, "id", None)
    closed_at = getattr(task, "closed_at", None)
    if not isinstance(task_id, str) or closed_at is None:
        return None
    return f"{task_id}:{closed_at.isoformat()}"


def classify_memory_review_close(
    task_manager: TaskLookup | None,
    *,
    task_id: str,
    changes_summary: str,
    reason: str,
    commit_shas: list[str],
) -> dict[str, str] | None:
    """Return queue data for one completed worked leaf, else ``None``."""
    if task_manager is None:
        return None
    summary = changes_summary.strip()
    if not summary or not task_id:
        return None
    try:
        task = task_manager.get_task(task_id)
    except (LookupError, ValueError):
        return None
    if task is None:
        return None

    reason = str(getattr(task, "closed_reason", None) or reason or "completed")
    if reason.casefold() != "completed":
        return None
    if str(getattr(task, "task_type", "")).casefold() == "epic":
        return None
    try:
        if task_manager.list_tasks(parent_task_id=task.id, limit=1):
            return None
    except (LookupError, ValueError):
        return None

    category = str(getattr(task, "category", None) or "").casefold()
    if category in _WORK_CATEGORIES:
        has_commits = bool(getattr(task, "commits", None)) or bool(commit_shas)
        if not has_commits:
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


def pending_memory_reviews(variables: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return unique unreviewed task references in queue order."""
    pending = variables.get("_memory_pending_task_reviews")
    if not isinstance(pending, list):
        return []
    reviewed = variables.get("_memory_task_review_records")
    reviewed_ids = (
        {
            item.get("closure_id")
            for item in reviewed
            if isinstance(item, Mapping) and isinstance(item.get("closure_id"), str)
        }
        if isinstance(reviewed, list)
        else set()
    )
    remaining: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in pending:
        if not isinstance(item, Mapping) or item.get("closure_id") in reviewed_ids:
            continue
        task_id = item.get("task_id")
        task_ref = item.get("task_ref")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(task_ref, str)
            or not task_ref
        ):
            continue
        identity = (task_id, task_ref)
        if identity in seen:
            continue
        seen.add(identity)
        remaining.append({"task_id": task_id, "task_ref": task_ref})
    return remaining


def pending_memory_reviews_complete(variables: Mapping[str, Any]) -> bool:
    """Return whether every queued closure already has a review record.

    A batch reviewed before its block is delivered needs no delivery, so the
    caller can release the stop/compact gate instead of re-requesting the
    reviews (#21062).
    """
    pending = variables.get("_memory_pending_task_reviews")
    reviewed = variables.get("_memory_task_review_records")
    if not isinstance(pending, list) or not pending or not isinstance(reviewed, list):
        return False
    reviewed_ids = {item.get("closure_id") for item in reviewed if isinstance(item, Mapping)}
    return all(
        isinstance(item, Mapping) and item.get("closure_id") in reviewed_ids for item in pending
    )
