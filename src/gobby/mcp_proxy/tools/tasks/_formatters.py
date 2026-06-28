"""Allowlisted task payloads for MCP agent workflows."""

from __future__ import annotations

from enum import Enum
from typing import Any

from gobby.tasks.state_semantics import serialize_task_state

MATCH_PREVIEW_MAX_LENGTH = 160


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    fields = getattr(value, "__dict__", {})
    if isinstance(fields, dict) and name in fields:
        return fields[name]
    return getattr(value, name, default)


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _task_ref(task: Any) -> str:
    seq_num = _field(task, "seq_num")
    if seq_num:
        return f"#{seq_num}"
    task_id = str(_field(task, "id", ""))
    return task_id[:8]


def _task_ref_from_seq(task: Any, seq_num: Any) -> str:
    if seq_num not in (None, ""):
        return f"#{seq_num}"
    return _task_ref(task)


def task_state_payload(task: Any) -> dict[str, Any]:
    """Return compact task state for MCP list/search/card payloads."""
    state = serialize_task_state(task)
    return {
        "current_stage": state.get("current_stage"),
        "is_closed": state.get("is_closed", False),
        "closed_at": state.get("closed_at"),
        "is_claimed": state.get("is_claimed", False),
        "is_blocked": state.get("is_blocked", False),
        "is_escalated": state.get("is_escalated", False),
    }


def task_discovery_payload(task: Any) -> dict[str, Any]:
    """Return compact task row for cheap discovery tools."""
    task_dict = task.to_dict() if callable(getattr(task, "to_dict", None)) else {}
    seq_num = task_dict.get("seq_num", _field(task, "seq_num"))
    return {
        "ref": _task_ref_from_seq(task, seq_num),
        "id": task_dict.get("id", _field(task, "id")),
        "seq_num": seq_num,
        "title": task_dict.get("title", _field(task, "title")),
        "task_type": task_dict.get("task_type", _field(task, "task_type")),
        "category": task_dict.get("category", _field(task, "category")),
        "priority": task_dict.get("priority", _field(task, "priority")),
        "path_cache": task_dict.get("path_cache", _field(task, "path_cache")),
        "updated_at": task_dict.get("updated_at", _field(task, "updated_at")),
        "state": task_state_payload(task),
    }


def match_preview(task: Any, *, max_length: int = MATCH_PREVIEW_MAX_LENGTH) -> str:
    """Return a bounded, whitespace-normalized description preview."""
    description = _field(task, "description")
    if not description:
        return ""

    preview = " ".join(str(description).split())
    if len(preview) <= max_length:
        return preview
    return f"{preview[: max_length - 3].rstrip()}..."


def task_search_payload(task: Any, score: float) -> dict[str, Any]:
    """Return compact search row with relevance metadata."""
    payload = task_discovery_payload(task)
    payload["score"] = round(score, 4)
    payload["match_preview"] = match_preview(task)
    return payload


def dependency_payload(dep: Any, linked_task_id: str, linked_task: Any | None) -> dict[str, Any]:
    """Return a compact dependency row pointing at the linked task."""
    if linked_task is None:
        return {
            "ref": linked_task_id[:8],
            "id": linked_task_id,
            "title": None,
            "state": None,
            "dep_type": _plain(_field(dep, "dep_type")),
        }

    return {
        "ref": _task_ref(linked_task),
        "id": _field(linked_task, "id"),
        "title": _field(linked_task, "title"),
        "state": task_state_payload(linked_task),
        "dep_type": _plain(_field(dep, "dep_type")),
    }


def task_summary_payload(
    task: Any, dependencies: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Return actionable get_task(brief=True) task card."""
    seq_num = _field(task, "seq_num")
    return {
        "ref": _task_ref_from_seq(task, seq_num),
        "id": _field(task, "id"),
        "seq_num": seq_num,
        "title": _field(task, "title"),
        "task_type": _field(task, "task_type"),
        "category": _field(task, "category"),
        "priority": _field(task, "priority"),
        "path_cache": _field(task, "path_cache"),
        "description": _field(task, "description"),
        "validation_criteria": _field(task, "validation_criteria"),
        "labels": _field(task, "labels"),
        "parent_task_id": _field(task, "parent_task_id"),
        "created_at": _field(task, "created_at"),
        "updated_at": _field(task, "updated_at"),
        "state": task_state_payload(task),
        "dependencies": dependencies,
        "allow_automation": _field(task, "allow_automation", False),
        "unattended": _field(task, "unattended", False),
        "isolation": _plain(_field(task, "isolation")),
        "assigned_agent": _field(task, "assigned_agent"),
        "implementation_domain": _field(task, "implementation_domain"),
        "additional_skills": _field(task, "additional_skills"),
    }
