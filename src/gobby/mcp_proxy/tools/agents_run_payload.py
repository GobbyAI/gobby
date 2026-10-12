"""Run list payloads for list_agent_runs and list_running_agents."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from gobby.storage.session_tasks import SessionTaskManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
    from gobby.storage.agents import AgentRun


def compact_running_agents(
    runs: list[AgentRun],
    ctx: AgentsRegistryContext,
    offset: int,
) -> dict[str, Any]:
    """Bound each page without losing seats, claim titles, or timestamps."""
    if ctx.db is None or ctx.session_manager is None:
        return {"success": False, "error": "Compact view requires database and session manager"}
    claims = SessionTaskManager(ctx.db).get_current_claims(
        [run.child_session_id for run in runs if run.child_session_id]
    )
    columns = [
        "session",
        "role",
        "lane",
        "seat",
        "worktree",
        "status",
        "wait_kind",
        "last_progress_at",
        "progress_age_seconds",
        "stall_suspected",
        "claims",
    ]
    result: dict[str, Any] = {
        "success": True,
        "columns": columns,
        "rows": [],
        "count": 0,
        "total": len(runs),
        "next_offset": None,
        "claim_columns": ["ref", "title", "claimed_at", "path"],
        "claims_source": "current task owner; first recorded session claim",
    }
    rows: list[list[Any]] = result["rows"]
    for index, run in enumerate(sorted(runs, key=lambda run: run.id)[offset:], start=offset):
        payload = _list_run_payload(run)
        session = ctx.session_manager.get(run.child_session_id) if run.child_session_id else None
        worktree = (
            ctx.worktree_storage.get(run.worktree_id)
            if ctx.worktree_storage is not None and run.worktree_id
            else None
        )
        seat = payload["seat"]
        owned_worktrees = sorted(
            {
                claim["worktree"]
                for claim in claims.get(run.child_session_id or "", [])
                if claim["worktree"]
            }
        )
        row = [
            f"#{session.seq_num}" if session and session.seq_num else run.child_session_id,
            run.agent_name,
            payload["lane"],
            seat["title"] if seat else None,
            worktree.worktree_path if worktree else owned_worktrees or None,
            run.child_status or run.status,
            run.wait_kind,
            payload["last_progress_at"],
            run.progress_age_seconds,
            run.stall_suspected,
            [
                [claim["ref"], claim["title"], claim["claimed_at"], claim["path"]]
                for claim in claims.get(run.child_session_id or "", [])
            ],
        ]
        rows.append(row)
        result["count"] = len(rows)
        # Leave margin for proxy metadata and pretty-print whitespace.
        if len(json.dumps(result, default=str, indent=2).encode()) >= 14_000:
            rows.pop()
            result["count"] = len(rows)
            result["next_offset"] = index
            if not rows:
                return {
                    "success": False,
                    "error": "One seat exceeds compact page budget",
                    "session": row[0],
                }
            break
    return result


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
        "lane": metadata.get("lane"),
    }


def _seat(placement: object) -> dict[str, Any] | None:
    """The workspace and raw placement title a placed run launched into, else None."""
    if not isinstance(placement, Mapping):
        return None
    workspace = placement.get("workspace_id")
    title = placement.get("title")
    if not workspace or not title:
        return None
    return {"workspace": workspace, "title": title}
