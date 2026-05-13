"""Read-only MCP tools for task stage manifests."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._stage_views import stage_registry_entry_view, stage_state_view

STAGE_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "stage_name": {"type": "string"},
        "display_name": {"type": ["string", "null"]},
        "display_label": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"]},
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
        "reviewer_agent_selector_json": {"type": ["string", "null"]},
        "review_policy": {"type": "string"},
        "dispatch_type": {"type": ["string", "null"]},
        "dispatch_target": {"type": ["string", "null"]},
        "dispatch_inputs_json": {"type": ["string", "null"]},
        "position_hint": {"type": "integer"},
        "requires_human": {"type": "boolean"},
        "is_terminal": {"type": "boolean"},
        "default_max_work_attempts": {"type": "integer"},
        "default_max_review_rounds": {"type": "integer"},
        "bundled_hash": {"type": ["string", "null"]},
        "deleted_at": {"type": ["string", "null"]},
        "is_edited": {"type": "boolean"},
    },
    "required": ["name", "display_label", "review_policy", "position_hint"],
}

STAGE_REGISTRY_MUTATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "stage": STAGE_REGISTRY_ENTRY_SCHEMA,
    },
    "required": ["ok", "stage"],
}

TASK_TYPE_DEFAULT_STAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stage_name": {"type": "string"},
        "position": {"type": "integer"},
    },
    "required": ["stage_name", "position"],
}


def _resolve_task(ctx: RegistryContext, task_id: str) -> str:
    return resolve_task_id_for_mcp(ctx.task_manager, task_id)


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
            stage_state_view(row)
            for row in ctx.task_manager.stage_states.list_for_task(resolved_id)
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

    def list_stages_registry(include_deleted: bool = False) -> dict[str, Any]:
        """Return all stage registry entries."""
        entries = [
            stage_registry_entry_view(row)
            for row in ctx.task_manager.stages_registry.list_all(include_deleted=include_deleted)
        ]
        return {"ok": True, "entries": entries}

    registry.register(
        name="list_stages_registry",
        description="Return all stage registry entries.",
        input_schema={
            "type": "object",
            "properties": {"include_deleted": {"type": "boolean", "default": False}},
            "required": [],
        },
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

    def update_stage(name: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update editable stage registry metadata."""
        entry = ctx.task_manager.stages_registry.update_stage(name, updates)
        return {"ok": True, "stage": stage_registry_entry_view(entry)}

    registry.register(
        name="update_stage",
        description="Update editable stage registry metadata. Stage names are immutable.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "updates": {"type": "object"},
            },
            "required": ["name", "updates"],
        },
        output_schema=STAGE_REGISTRY_MUTATION_SCHEMA,
        func=update_stage,
    )

    def restore_stage(name: str) -> dict[str, Any]:
        """Restore a bundled stage row."""
        entry = ctx.task_manager.stages_registry.restore_stage(name)
        return {"ok": True, "stage": stage_registry_entry_view(entry)}

    registry.register(
        name="restore_stage",
        description="Restore a bundled stage registry row.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        output_schema=STAGE_REGISTRY_MUTATION_SCHEMA,
        func=restore_stage,
    )

    def delete_stage(name: str) -> dict[str, Any]:
        """Soft-delete an unused stage registry row."""
        entry = ctx.task_manager.stages_registry.delete_stage(name)
        return {"ok": True, "stage": stage_registry_entry_view(entry)}

    registry.register(
        name="delete_stage",
        description="Soft-delete an unused stage registry row.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        output_schema=STAGE_REGISTRY_MUTATION_SCHEMA,
        func=delete_stage,
    )

    def set_task_type_defaults(
        task_type: str,
        stages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Replace a task type's default stage manifest."""
        normalized: list[tuple[str, int]] = []
        for index, item in enumerate(stages):
            stage_name = item.get("stage_name")
            if not isinstance(stage_name, str) or not stage_name.strip():
                return {
                    "ok": False,
                    "error": "invalid_default_stage",
                    "message": f"stages[{index}].stage_name must be a non-empty string",
                }
            position = item.get("position")
            if not isinstance(position, int) or isinstance(position, bool):
                return {
                    "ok": False,
                    "error": "invalid_default_stage",
                    "message": f"stages[{index}].position must be an integer",
                }
            normalized.append((stage_name.strip(), position))
        try:
            ctx.task_manager.stages_registry.set_default_stages(task_type, normalized)
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_default_stage",
                "message": str(exc),
            }
        return {
            "ok": True,
            "task_type": task_type,
            "stages": [
                {"stage_name": stage_name, "position": position}
                for stage_name, position in ctx.task_manager.stages_registry.list_default_stages(
                    task_type
                )
            ],
        }

    registry.register(
        name="set_task_type_defaults",
        description="Replace a task type's default stage manifest.",
        input_schema={
            "type": "object",
            "properties": {
                "task_type": {"type": "string"},
                "stages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": TASK_TYPE_DEFAULT_STAGE_SCHEMA["properties"],
                        "required": TASK_TYPE_DEFAULT_STAGE_SCHEMA["required"],
                    },
                },
            },
            "required": ["task_type", "stages"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "task_type": {"type": "string"},
                "stages": {"type": "array", "items": TASK_TYPE_DEFAULT_STAGE_SCHEMA},
                "error": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["ok"],
        },
        func=set_task_type_defaults,
    )

    def get_task_type_defaults(task_type: str) -> dict[str, Any]:
        """Return the default stage manifest for a task type."""
        defaults = ctx.task_manager.stages_registry.list_default_stages(task_type)
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
                    "items": TASK_TYPE_DEFAULT_STAGE_SCHEMA,
                },
            },
            "required": ["ok"],
        },
        func=get_task_type_defaults,
    )

    return registry
