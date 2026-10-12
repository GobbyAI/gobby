"""Task discovery query registration and bounded fleet projections."""

import json
from datetime import datetime
from typing import Any, TypedDict

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._formatters import task_discovery_payload
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import Task, TaskNotFoundError
from gobby.tasks.close_receipts import list_close_receipts
from gobby.utils.datetime import datetime_to_iso


class TaskStateFilters(TypedDict, total=False):
    claimed: bool
    escalated: bool
    updated_since: datetime
    candidate: bool
    labels: list[str]


def _bound_compact_row(
    row: list[Any],
    columns: list[str],
    task_id: str,
    ref_errors: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Omit large values with full-fetch pointers before eliding a whole row."""
    page: dict[str, Any] = {"columns": columns, "rows": [row], "count": 1, "next_offset": None}
    if ref_errors:
        page["ref_errors"] = ref_errors
    if len(json.dumps(page, default=str, indent=2).encode()) < 14_000:
        return row

    ref = row[0]
    pointer = f"Value elided; get_task(task_id='{task_id}', brief=false)"
    for column in sorted(
        (1, 7, 8, 11), key=lambda index: len(json.dumps(row[index], default=str)), reverse=True
    ):
        if column == 11:
            row[column] = [receipt[:3] + [{"_elided": pointer}] for receipt in row[column]]
        elif column == 8:
            row[column] = [[blocker[0], pointer] for blocker in row[column]]
        elif row[column] is not None:
            row[column] = pointer
        if len(json.dumps(page, default=str, indent=2).encode()) < 14_000:
            return row
    return [ref, f"Row elided; {pointer}", *([None] * (len(columns) - 2))]


def compact_tasks(
    tasks: list[Task],
    ctx: RegistryContext,
    offset: int,
    ref_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return every task field needed for fleet sections in a bounded page."""
    candidate_times = ctx.task_manager.get_candidate_sent_times([task.id for task in tasks])
    columns = [
        "ref",
        "title",
        "path",
        "type",
        "claim_owner",
        "closed_at",
        "escalated_at",
        "escalation_reason",
        "blocked_by",
        "stage",
        "candidate_sent_at",
        "receipts",
        "latest_linked_commit",
    ]
    result: dict[str, Any] = {"columns": columns, "rows": [], "count": 0, "next_offset": None}
    if ref_errors:
        result["ref_errors"] = ref_errors
        if len(json.dumps(result, default=str, indent=2).encode()) >= 14_000:
            return {"error": "Ref errors exceed compact page budget; reduce the refs batch"}
    rows: list[list[Any]] = result["rows"]
    for index, task in enumerate(tasks, start=offset):
        blockers = [
            ctx.task_manager.get_task(task_id) for task_id in sorted(task.active_blocked_by)
        ]
        row = [
            f"#{task.seq_num}" if task.seq_num else task.id,
            task.title,
            task.path_cache,
            task.task_type,
            task.claimed_by_session_id,
            datetime_to_iso(task.closed_at),
            datetime_to_iso(task.escalated_at),
            task.escalation_reason,
            [
                [f"#{blocker.seq_num}" if blocker.seq_num else blocker.id, blocker.title]
                for blocker in blockers
            ],
            task_discovery_payload(task)["state"],
            datetime_to_iso(candidate_times.get(task.id)),
            [
                [receipt.kind, receipt.commit_sha, receipt.recorded_at, receipt.facts]
                for receipt in list_close_receipts(ctx.task_manager.db, task.id)
            ],
            task.commits[-1] if task.commits else None,
        ]
        row = _bound_compact_row(row, columns, task.id, ref_errors if not rows else None)
        rows.append(row)
        result["count"] = len(rows)
        if len(json.dumps(result, default=str, indent=2).encode()) >= 14_000:
            rows.pop()
            result["count"] = len(rows)
            result["next_offset"] = index
            if not rows:
                return {
                    "error": "Ref errors leave no room for a compact row; reduce the refs batch"
                }
            break
    return result


def register_task_listing(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    def list_tasks(
        current_stage_state: str | list[str] | None = None,
        priority: int | None = None,
        task_type: str | None = None,
        label: str | None = None,
        labels: list[str] | None = None,
        closed: bool | None = None,
        parent_task_id: str | None = None,
        title_like: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str | None = None,
        all_projects: bool = False,
        project: str | None = None,
        claimed: bool | None = None,
        escalated: bool | None = None,
        updated_since: str | None = None,
        refs: list[str] | None = None,
        view: str = "default",
        candidate: bool | None = None,
    ) -> dict[str, Any]:
        """List tasks with optional filters."""
        try:
            project_id = ctx.resolve_project_filter(project, all_projects)
        except ValueError as e:
            return {"error": str(e), "tasks": [], "count": 0}

        if view not in {"default", "compact"} or offset < 0 or limit < 1:
            return {"error": "Use view=default|compact, offset >= 0, limit >= 1"}
        try:
            since = datetime.fromisoformat(updated_since) if updated_since else None
            if since is not None and since.tzinfo is None:
                raise ValueError("updated_since requires a timezone")
        except ValueError as exc:
            return {"error": str(exc)}
        if view == "compact":
            limit = min(limit, 100)
        if refs is not None:
            if len(refs) > 100:
                return {"error": "Resolve at most 100 refs per call"}
            tasks = []
            positions: list[int] = []
            ref_errors: list[dict[str, Any]] = []
            for position, ref in enumerate(refs[offset : offset + limit], start=offset):
                try:
                    task = ctx.task_manager.get_task(
                        resolve_task_id_for_mcp(
                            ctx.task_manager,
                            ref,
                            project_id,
                        )
                    )
                except (TaskNotFoundError, ValueError) as exc:
                    ref_errors.append({"ref": ref, "error": str(exc), "offset": position})
                else:
                    tasks.append(task)
                    positions.append(position)
            result = (
                compact_tasks(tasks, ctx, offset, ref_errors)
                if view == "compact"
                else {"tasks": [task_discovery_payload(t) for t in tasks], "count": len(tasks)}
            )
            next_offset = result.get("next_offset")
            if isinstance(next_offset, int):
                next_offset = positions[next_offset - offset]
                result["next_offset"] = next_offset
                if ref_errors:
                    result["ref_errors"] = [
                        error for error in ref_errors if error["offset"] < next_offset
                    ]
            elif offset + limit < len(refs):
                result["next_offset"] = offset + limit
            if ref_errors and view == "default":
                result["ref_errors"] = ref_errors
            return result

        # Resolve parent_task_id if it's a reference format
        if parent_task_id:
            try:
                parent_task_id = resolve_task_id_for_mcp(
                    ctx.task_manager, parent_task_id, project_id
                )
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid parent_task_id: {e}", "tasks": [], "count": 0}

        # Handle comma-separated current-stage state strings.
        current_stage_filter: str | list[str] | None = current_stage_state
        if isinstance(current_stage_state, str) and "," in current_stage_state:
            current_stage_filter = [s.strip() for s in current_stage_state.split(",")]

        closed_filter: dict[str, Any] = {"closed": closed} if closed is not None else {}
        state_filters: TaskStateFilters = {}
        if claimed is not None:
            state_filters["claimed"] = claimed
        if escalated is not None:
            state_filters["escalated"] = escalated
        if since is not None:
            state_filters["updated_since"] = since
        if candidate is not None:
            state_filters["candidate"] = candidate
        if labels is not None:
            state_filters["labels"] = labels
        tasks = ctx.task_manager.list_tasks(
            current_stage_state=current_stage_filter,
            priority=priority,
            task_type=task_type,
            label=label,
            parent_task_id=parent_task_id,
            title_like=title_like,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            project_id=project_id,
            **state_filters,
            **closed_filter,
        )
        if view == "compact":
            result = compact_tasks(tasks, ctx, offset)
            if result.get("next_offset") is None and len(tasks) == limit:
                result["next_offset"] = offset + limit
        else:
            result = {"tasks": [task_discovery_payload(t) for t in tasks], "count": len(tasks)}
        if parent_task_id:
            result["open_count"] = ctx.task_manager.count_tasks(
                current_stage_state=current_stage_filter,
                priority=priority,
                task_type=task_type,
                label=label,
                labels=labels,
                parent_task_id=parent_task_id,
                title_like=title_like,
                project_id=project_id,
                closed=False,
            )
        return result

    registry.register(
        name="list_tasks",
        description="List tasks with optional filters.",
        input_schema={
            "type": "object",
            "properties": {
                "current_stage_state": {
                    "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "Filter by current stage state. Can be a single state, array of states, or comma-separated string.",
                    "default": None,
                },
                "priority": {
                    "type": "integer",
                    "description": "Filter by priority",
                    "default": None,
                },
                "task_type": {
                    "type": "string",
                    "description": "Filter by task type",
                    "default": None,
                },
                "label": {
                    "type": "string",
                    "description": "Filter by label presence",
                    "default": None,
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Match any exact label; empty matches none. Combined with label using AND.",
                },
                "closed": {
                    "type": "boolean",
                    "description": "true = closed only, false = open only, omitted = both",
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Filter by parent task: #N, N (seq_num), path (1.2.3), or UUID",
                    "default": None,
                },
                "title_like": {
                    "type": "string",
                    "description": "Filter by title (fuzzy match)",
                    "default": None,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of tasks to return",
                    "default": 50,
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of tasks to skip for pagination",
                    "default": 0,
                },
                "sort_by": {
                    "type": "string",
                    "description": "Task sort field; omitted uses hierarchy order",
                    "default": None,
                },
                "all_projects": {
                    "type": "boolean",
                    "description": "If true, list tasks from all projects instead of just the current project",
                    "default": False,
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name or UUID (e.g., '_personal')",
                    "default": None,
                },
                "claimed": {"type": "boolean", "description": "Filter by current ownership"},
                "candidate": {"type": "boolean", "description": "Explicit CANDIDATE event sent"},
                "escalated": {"type": "boolean", "description": "Filter by escalation state"},
                "updated_since": {"type": "string", "description": "ISO timestamp with timezone"},
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Resolve up to 100 refs in supplied order instead of filters",
                },
                "view": {
                    "type": "string",
                    "enum": ["default", "compact"],
                    "default": "default",
                    "description": "Compact columns/rows pages include ownership and timestamps",
                },
            },
        },
        func=list_tasks,
        read_only=True,
    )
