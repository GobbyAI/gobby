"""Run list payloads for list_agent_runs and list_running_agents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _list_run_payload(run: Any) -> dict[str, Any]:
    """Return identity and coordinator decision fields for agent run lists."""
    metadata = getattr(run, "resume_metadata_json", None)
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        **run.liveness_payload(),
        "run_id": run.id,
        "task_ref": metadata.get("task_ref") or getattr(run, "task_id", None),
        "agent_name": getattr(run, "agent_name", None),
        "status": run.status,
        "started_at": getattr(run, "started_at", None),
        "branch_name": metadata.get("branch_name"),
        "tool_calls_count": getattr(run, "tool_calls_count", 0),
        "turns_used": getattr(run, "turns_used", 0),
        "seat": _seat(metadata.get("placement")),
    }


def _seat(placement: object) -> dict[str, Any] | None:
    """The workspace and canonical title a placed run launched into, else None."""
    if not isinstance(placement, Mapping):
        return None
    workspace = placement.get("workspace_id")
    title = placement.get("title")
    if not workspace or not title:
        return None
    return {"workspace": workspace, "title": title}
