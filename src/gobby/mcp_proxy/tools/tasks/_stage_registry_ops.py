"""Mutating MCP tools for the task stage registry."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_read import (
    STAGE_REGISTRY_ENTRY_SCHEMA,
    TASK_TYPE_DEFAULT_STAGE_SCHEMA,
)
from gobby.storage.tasks._stage_registry import (
    EDITABLE_STAGE_UPDATE_FIELDS,
    StageRegistryNotFoundError,
)
from gobby.storage.tasks._stage_views import stage_registry_entry_view

STAGE_REGISTRY_MUTATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "stage": STAGE_REGISTRY_ENTRY_SCHEMA,
        "error": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["ok"],
}


def _stage_mutation_error(error: ValueError, operation: str) -> dict[str, Any]:
    error_code = (
        "stage_not_found"
        if isinstance(error, StageRegistryNotFoundError)
        else f"invalid_stage_{operation}"
    )
    return {"ok": False, "error": error_code, "message": str(error)}


def create_stage_registry_ops_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create stage-registry mutation tools for gobby-tasks-ops."""
    registry = InternalToolRegistry(
        name="gobby-tasks-stage-registry-ops",
        description="Task stage registry mutation tools",
    )

    def update_stage(name: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Update editable stage registry metadata."""
        try:
            entry = ctx.task_manager.stages_registry.update_stage(name, updates)
        except ValueError as error:
            return _stage_mutation_error(error, "update")
        return {"ok": True, "stage": stage_registry_entry_view(entry)}

    registry.register(
        name="update_stage",
        description="Update editable stage registry metadata. Stage names are immutable.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "updates": {
                    "type": "object",
                    "properties": {field: {} for field in EDITABLE_STAGE_UPDATE_FIELDS},
                    "additionalProperties": False,
                },
            },
            "required": ["name", "updates"],
        },
        output_schema=STAGE_REGISTRY_MUTATION_SCHEMA,
        func=update_stage,
    )

    def restore_stage(name: str) -> dict[str, Any]:
        """Restore a bundled stage row."""
        try:
            entry = ctx.task_manager.stages_registry.restore_stage(name)
        except ValueError as error:
            return _stage_mutation_error(error, "restore")
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
        try:
            entry = ctx.task_manager.stages_registry.delete_stage(name)
        except ValueError as error:
            return _stage_mutation_error(error, "delete")
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

    return registry
