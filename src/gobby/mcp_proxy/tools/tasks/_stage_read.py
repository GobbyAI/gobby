"""Read-only MCP tools for task stage manifests."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError

STAGE_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "stage_name": {"type": "string"},
        "position": {"type": "integer"},
        "state": {"type": "string"},
        "review_policy": {"type": "string"},
        "reviewer_agent": {"type": ["string", "null"]},
        "entered_at": {"type": ["string", "null"]},
        "entered_by_session_id": {"type": ["string", "null"]},
        "completed_at": {"type": ["string", "null"]},
        "completed_by_session_id": {"type": ["string", "null"]},
        "completed_commit_sha": {"type": ["string", "null"]},
        "work_attempt_count": {"type": "integer"},
        "review_round_count": {"type": "integer"},
        "max_work_attempts": {"type": ["integer", "null"]},
        "max_review_rounds": {"type": ["integer", "null"]},
        "artifact_refs": {"type": ["object", "null"]},
        "notes": {"type": ["string", "null"]},
        "updated_at": {"type": "string"},
    },
    "required": ["task_id", "stage_name", "position", "state", "review_policy"],
}

STAGE_REGISTRY_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "display_label": {"type": "string"},
        "description": {"type": "string"},
        "category": {"type": "string"},
        "default_agent": {"type": ["string", "null"]},
        "reviewer_agent": {"type": ["string", "null"]},
        "review_policy": {"type": "string"},
        "position_hint": {"type": "integer"},
        "requires_human": {"type": "boolean"},
        "is_terminal": {"type": "boolean"},
        "default_max_work_attempts": {"type": "integer"},
        "default_max_review_rounds": {"type": "integer"},
    },
    "required": ["name", "display_label", "review_policy", "position_hint"],
}


def _resolve_task(ctx: RegistryContext, task_id: str) -> str:
    return resolve_task_id_for_mcp(ctx.task_manager, task_id)


def _state_view(row: Any) -> dict[str, Any]:
    return {
        "task_id": row.task_id,
        "stage_name": row.stage_name,
        "position": row.position,
        "state": row.state,
        "review_policy": row.review_policy,
        "reviewer_agent": row.reviewer_agent,
        "entered_at": row.entered_at,
        "entered_by_session_id": row.entered_by_session_id,
        "completed_at": row.completed_at,
        "completed_by_session_id": row.completed_by_session_id,
        "completed_commit_sha": row.completed_commit_sha,
        "work_attempt_count": row.work_attempt_count,
        "review_round_count": row.review_round_count,
        "max_work_attempts": row.max_work_attempts,
        "max_review_rounds": row.max_review_rounds,
        "artifact_refs": row.artifact_refs,
        "notes": row.notes,
        "updated_at": row.updated_at,
    }


def _registry_entry_view(row: Any) -> dict[str, Any]:
    return {
        "name": row.name,
        "display_label": row.display_label,
        "description": row.description,
        "category": row.category,
        "default_agent": row.default_agent,
        "reviewer_agent": row.reviewer_agent,
        "review_policy": row.review_policy,
        "position_hint": row.position_hint,
        "requires_human": row.requires_human,
        "is_terminal": row.is_terminal,
        "default_max_work_attempts": row.default_max_work_attempts,
        "default_max_review_rounds": row.default_max_review_rounds,
    }


def create_stage_read_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create read-only task stage manifest tools for gobby-tasks."""

    registry = InternalToolRegistry(
        name="gobby-tasks-stage-read",
        description="Read-only task stage manifest tools",
    )

    def get_task_stages(task_id: str) -> dict[str, Any]:
        """Return a task's stage manifest in position order."""
        try:
            resolved_id = _resolve_task(ctx, task_id)
        except (TaskNotFoundError, ValueError) as error:
            return {"ok": False, "error": "invalid_task_id", "message": str(error)}
        stages = [
            _state_view(row) for row in ctx.task_manager.stage_states().list_for_task(resolved_id)
        ]
        return {"ok": True, "task_id": resolved_id, "stages": stages}

    registry.register(
        name="get_task_stages",
        description="Return a task's stage manifest in position order.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task reference: #N, path, or UUID"},
            },
            "required": ["task_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "task_id": {"type": "string"},
                "stages": {"type": "array", "items": STAGE_STATE_SCHEMA},
            },
            "required": ["ok", "task_id", "stages"],
        },
        func=get_task_stages,
    )

    def list_stages_registry() -> dict[str, Any]:
        """Return all stage registry entries."""
        entries = [
            _registry_entry_view(row) for row in ctx.task_manager.stages_registry().list_all()
        ]
        return {"ok": True, "entries": entries}

    registry.register(
        name="list_stages_registry",
        description="Return all stage registry entries.",
        input_schema={"type": "object", "properties": {}, "required": []},
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "entries": {"type": "array", "items": STAGE_REGISTRY_ENTRY_SCHEMA},
            },
            "required": ["ok", "entries"],
        },
        func=list_stages_registry,
    )

    def get_task_type_defaults(task_type: str) -> dict[str, Any]:
        """Return the default stage manifest for a task type."""
        defaults = ctx.task_manager.stages_registry().list_default_stages(task_type)
        if not defaults:
            return {
                "ok": False,
                "error": "unknown_task_type",
                "message": f"No default stage manifest for task_type '{task_type}'",
            }
        return {
            "ok": True,
            "task_type": task_type,
            "stages": [
                {"stage_name": stage_name, "position": position}
                for stage_name, position in defaults
            ],
        }

    registry.register(
        name="get_task_type_defaults",
        description="Return the default stage manifest for a task type.",
        input_schema={
            "type": "object",
            "properties": {"task_type": {"type": "string"}},
            "required": ["task_type"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "task_type": {"type": "string"},
                "stages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "stage_name": {"type": "string"},
                            "position": {"type": "integer"},
                        },
                    },
                },
            },
            "required": ["ok"],
        },
        func=get_task_type_defaults,
    )

    return registry
