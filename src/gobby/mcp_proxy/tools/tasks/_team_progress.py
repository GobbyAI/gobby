"""The team tools: stage spans, stall flags and admitted bindings for a team lead."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from gobby.agents.team_admission_records import team_binding
from gobby.agents.team_lifecycle import seat_team
from gobby.agents.team_progress import TaskProgress, TeamProgress, team_progress
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.hub.protocol import HubDatabase

_TEAM_ID_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "team_id": {
            "type": "string",
            "description": "Team to read; omit to read the calling seat's team",
        },
    },
}


def create_team_progress_registry(ctx: RegistryContext) -> InternalToolRegistry:
    registry = InternalToolRegistry(
        name="gobby-tasks-progress",
        description="Team stage timing, stall flags and team bindings",
    )

    def team_progress_tool(team_id: str | None = None) -> dict[str, Any]:
        """Report each task a team claimed: stage spans, claim-to-close time and stall flags.

        Args:
            team_id: Team to read. Omit it to read the calling seat's own team.

        Returns:
            The team's tasks, or an error dict when the team or the caller's team is unknown.
        """
        resolved = _resolve_team(ctx, team_id)
        if isinstance(resolved, dict):
            return resolved
        db = ctx.task_manager.db
        if team_binding(db, resolved) is None:
            return {"error": f"Team {resolved} not found", "error_code": "team_not_found"}
        return _payload(team_progress(db, resolved, now=datetime.now(UTC)))

    def get_team_tool(team_id: str | None = None) -> dict[str, Any]:
        """Read a team's admitted binding: its task, branch, worktree, mode and reviewer seats.

        Args:
            team_id: Team to read. Omit it to read the calling seat's own team.

        Returns:
            The team binding, or an error dict when the team or the caller's team is unknown.
        """
        resolved = _resolve_team(ctx, team_id)
        if isinstance(resolved, dict):
            return resolved
        return team_binding_payload(ctx.task_manager.db, resolved)

    registry.register(
        name="team_progress",
        description=(
            "Report a team's task progress: each stage's span in seconds, claim-to-close time, "
            "and flags (handoff_unacknowledged, handoff_pending, holder_not_live, "
            "idle_on_open_work)."
        ),
        input_schema=_TEAM_ID_INPUT,
        func=team_progress_tool,
    )
    registry.register(
        name="get_team",
        description=(
            "Read a team's admitted binding: its task, branch, worktree, mode, parent team "
            "and reviewer seats."
        ),
        input_schema=_TEAM_ID_INPUT,
        func=get_team_tool,
    )
    return registry


def _resolve_team(ctx: RegistryContext, team_id: str | None) -> str | dict[str, Any]:
    """The team id to read, or the error dict explaining why the caller has no team to read."""
    if team_id is not None:
        return team_id
    from gobby.utils.session_context import get_current_session_id

    session_id = get_current_session_id()
    if not session_id:
        return {"error": "Session context is required", "error_code": "session_required"}
    try:
        caller = ctx.resolve_session_id(session_id)
    except ValueError as e:
        return {"error": f"Cannot resolve session '{session_id}': {e}"}
    seat = seat_team(ctx.task_manager.db, caller)
    if seat is None:
        return {
            "error": "The caller is not on a team; pass team_id",
            "error_code": "team_required",
        }
    return seat.team_id


def team_binding_payload(db: HubDatabase, team_id: str) -> dict[str, Any]:
    """A team's admitted binding as a payload, or the team_not_found error for an unknown team."""
    binding = team_binding(db, team_id)
    if binding is None:
        return {"error": f"Team {team_id} not found", "error_code": "team_not_found"}
    return asdict(binding)


def _payload(progress: TeamProgress) -> dict[str, Any]:
    return {
        "team_id": progress.team_id,
        "tasks": [_task_payload(task) for task in progress.tasks],
    }


def _task_payload(task: TaskProgress) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "seq_num": task.seq_num,
        "stages": [{"stage": span.stage, "seconds": span.seconds} for span in task.stages],
        "claim_to_close_seconds": task.claim_to_close_seconds,
        "flags": sorted(task.flags),
    }
