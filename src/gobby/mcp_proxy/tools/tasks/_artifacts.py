"""MCP artifact mutation tools for gobby-tasks-ops.

These tools are intentionally scoped to merge, expansion-qa, and holistic-reviewer
agents that need to record task artifact pointers or append audit sections.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.tasks import (
    TaskArtifactConstraintError,
    TaskArtifactManager,
    TaskArtifacts,
    TaskNotFoundError,
)

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

_ARTIFACT_MUTATION_FIELDS = frozenset(TaskArtifacts.__dataclass_fields__) - {
    "task_id",
    "updated_at",
}


def _artifact_field_schema(field: str) -> dict[str, Any]:
    if field == "expansion_attempts" or field.startswith("max_"):
        return {"type": ["integer", "null"]}
    return {"type": ["string", "null"]}


_ARTIFACT_FIELD_SCHEMAS = {
    field: _artifact_field_schema(field) for field in sorted(_ARTIFACT_MUTATION_FIELDS)
}


def _artifact_payload(artifacts: TaskArtifacts) -> dict[str, Any]:
    if artifacts.updated_at is None:
        return {}
    data = asdict(artifacts)
    return {key: value for key, value in data.items() if key in _ARTIFACT_MUTATION_FIELDS}


def _validate_artifact_fields(fields: set[str]) -> dict[str, Any] | None:
    unknown = fields - _ARTIFACT_MUTATION_FIELDS
    if not unknown:
        return None
    return {
        "ok": False,
        "error": "invalid_artifact_field",
        "message": f"Unknown task artifact field(s): {', '.join(sorted(unknown))}",
        "allowed_fields": sorted(_ARTIFACT_MUTATION_FIELDS),
    }


def _constraint_error_result(error: Exception) -> dict[str, Any]:
    if isinstance(error, TaskArtifactConstraintError):
        return {
            "ok": False,
            "error": "artifact_constraint",
            "predicate": error.predicate,
            "message": str(error),
        }
    return {
        "ok": False,
        "error": "artifact_check_constraint",
        "message": str(error),
    }


def _resolve_task(ctx: RegistryContext, task_id: str) -> str | dict[str, str]:
    try:
        return resolve_task_id_for_mcp(ctx.task_manager, task_id)
    except (TaskNotFoundError, ValueError) as error:
        return {"error": f"Invalid task_id: {error}"}


def _append_section_body(heading: str, body: str) -> str:
    normalized_heading = heading.strip()
    if not normalized_heading:
        raise ValueError("heading must not be blank")
    return f"## {normalized_heading}\n{body.rstrip('\n')}\n"


def create_ops_artifact_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create gobby-tasks-ops artifact mutation tools."""

    registry = InternalToolRegistry(
        name="gobby-tasks-artifacts-ops",
        description="Task artifact mutation tools for merge, expansion-qa, and holistic-reviewer agents",
    )
    artifact_manager = TaskArtifactManager(ctx.task_manager.db)

    def set_artifact(task_id: str, field: str, value: str | int | None) -> dict[str, Any]:
        """Set one task artifact field for merge/expansion QA/reviewer agents."""
        invalid = _validate_artifact_fields({field})
        if invalid:
            return invalid
        resolved_id = _resolve_task(ctx, task_id)
        if isinstance(resolved_id, dict):
            return resolved_id
        try:
            artifacts = artifact_manager.set_artifact(resolved_id, field, value)
        except (TaskArtifactConstraintError, psycopg.IntegrityError) as error:
            return _constraint_error_result(error)
        except ValueError as error:
            return {"ok": False, "error": "invalid_artifact_value", "message": str(error)}
        return {"ok": True, "task_id": resolved_id, "artifacts": _artifact_payload(artifacts)}

    registry.register(
        name="set_artifact",
        description=(
            "Set one task artifact pointer field. For merge, expansion-qa, and "
            "holistic-reviewer agents only."
        ),
        input_schema={
            "type": "object",
            "x-artifact-fields": _ARTIFACT_FIELD_SCHEMAS,
            "properties": {
                "task_id": {"type": "string", "description": "Task reference: #N, path, or UUID"},
                "field": {
                    "type": "string",
                    "description": "Artifact field to set",
                    "enum": sorted(_ARTIFACT_MUTATION_FIELDS),
                },
                "value": {
                    "description": "Artifact value; null clears the field",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "integer"},
                        {"type": "null"},
                    ],
                },
            },
            "required": ["task_id", "field", "value"],
        },
        func=set_artifact,
    )

    def set_artifacts_atomic(task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Set multiple artifact fields atomically for ops agents."""
        invalid = _validate_artifact_fields(set(fields))
        if invalid:
            return invalid
        resolved_id = _resolve_task(ctx, task_id)
        if isinstance(resolved_id, dict):
            return resolved_id
        try:
            artifacts = artifact_manager.set_artifacts_atomic(resolved_id, **fields)
        except (TaskArtifactConstraintError, psycopg.IntegrityError) as error:
            return _constraint_error_result(error)
        except ValueError as error:
            return {"ok": False, "error": "invalid_artifact_value", "message": str(error)}
        return {"ok": True, "task_id": resolved_id, "artifacts": _artifact_payload(artifacts)}

    registry.register(
        name="set_artifacts_atomic",
        description=(
            "Atomically set task artifact pointer fields. Constraint failures return "
            "structured errors for merge, expansion-qa, and holistic-reviewer agents."
        ),
        input_schema={
            "type": "object",
            "x-artifact-fields": _ARTIFACT_FIELD_SCHEMAS,
            "properties": {
                "task_id": {"type": "string", "description": "Task reference: #N, path, or UUID"},
                "fields": {
                    "type": "object",
                    "description": "Artifact fields to set atomically",
                    "properties": _ARTIFACT_FIELD_SCHEMAS,
                    "additionalProperties": False,
                },
            },
            "required": ["task_id", "fields"],
        },
        func=set_artifacts_atomic,
    )

    def clear_isolation_pair(task_id: str, family: str) -> dict[str, Any]:
        """Clear a worktree or clone isolation artifact pair."""
        resolved_id = _resolve_task(ctx, task_id)
        if isinstance(resolved_id, dict):
            return resolved_id
        try:
            artifacts = artifact_manager.clear_isolation_pair(resolved_id, family)
        except (TaskArtifactConstraintError, psycopg.IntegrityError) as error:
            return _constraint_error_result(error)
        except ValueError as error:
            return {"ok": False, "error": "invalid_isolation_family", "message": str(error)}
        return {"ok": True, "task_id": resolved_id, "artifacts": _artifact_payload(artifacts)}

    registry.register(
        name="clear_isolation_pair",
        description=(
            "Clear the worktree or clone artifact pair for merge, expansion-qa, "
            "and holistic-reviewer agents."
        ),
        input_schema={
            "type": "object",
            "x-artifact-fields": _ARTIFACT_FIELD_SCHEMAS,
            "properties": {
                "task_id": {"type": "string", "description": "Task reference: #N, path, or UUID"},
                "family": {
                    "type": "string",
                    "description": "Isolation artifact family to clear",
                    "enum": ["worktree", "clone"],
                },
            },
            "required": ["task_id", "family"],
        },
        func=clear_isolation_pair,
    )

    def append_description_section(task_id: str, heading: str, body: str) -> dict[str, Any]:
        """Append an idempotent markdown section to a task description."""
        resolved_id = _resolve_task(ctx, task_id)
        if isinstance(resolved_id, dict):
            return resolved_id
        try:
            section = _append_section_body(heading, body)
        except ValueError as error:
            return {"ok": False, "error": "invalid_description_section", "message": str(error)}

        with ctx.task_manager.db.transaction_immediate() as conn:
            row = conn.execute(
                "SELECT description FROM tasks WHERE id = ?",
                (resolved_id,),
            ).fetchone()
            if row is None:
                return {"error": f"Task {task_id} not found"}
            current_description = row["description"] or ""
            heading_pattern = re.compile(
                rf"^##\s+{re.escape(heading.strip())}\s*$",
                re.MULTILINE,
            )
            if heading_pattern.search(current_description):
                return {"ok": True, "task_id": resolved_id, "appended": False}

            prefix = current_description.rstrip()
            next_description = f"{prefix}\n\n{section}" if prefix else section
            conn.execute(
                """
                UPDATE tasks
                SET description = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_description, datetime.now(UTC).isoformat(), resolved_id),
            )
            ctx.task_manager._notify_listeners()
        return {"ok": True, "task_id": resolved_id, "appended": True}

    registry.register(
        name="append_description_section",
        description=(
            "Append an idempotent markdown section to tasks.description for merge, "
            "expansion-qa, and holistic-reviewer agents."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task reference: #N, path, or UUID"},
                "heading": {"type": "string", "description": "Markdown heading text"},
                "body": {"type": "string", "description": "Section body"},
            },
            "required": ["task_id", "heading", "body"],
        },
        func=append_description_section,
    )

    def get_artifacts(task_id: str) -> dict[str, Any]:
        """Return task artifact fields, or an empty dict when none are stored."""
        resolved_id = _resolve_task(ctx, task_id)
        if isinstance(resolved_id, dict):
            return resolved_id
        artifacts = artifact_manager.get_artifacts(resolved_id)
        return _artifact_payload(artifacts)

    registry.register(
        name="get_artifacts",
        description=(
            "Get task artifact pointer fields for merge, expansion-qa, and "
            "holistic-reviewer agents."
        ),
        input_schema={
            "type": "object",
            "x-artifact-fields": _ARTIFACT_FIELD_SCHEMAS,
            "properties": {
                "task_id": {"type": "string", "description": "Task reference: #N, path, or UUID"},
            },
            "required": ["task_id"],
        },
        func=get_artifacts,
    )

    return registry
