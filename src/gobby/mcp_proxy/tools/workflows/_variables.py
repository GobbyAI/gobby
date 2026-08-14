"""
Workflow variable tools.

Runtime: set_variable, get_variable (session/workflow-scoped).
Definitions: create_variable, update_variable, delete_variable, export_variable,
             list_variables, get_variable_definition (DB-backed CRUD).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from gobby.mcp_proxy.tools.workflows._resolution import (
    resolve_session_id,
    resolve_session_task_value,
)
from gobby.sessions.compact_markers import SKILL_LIST_VARIABLE_NAMES
from gobby.storage.definitions.variables import (
    SessionVariableDefaultManager,
    SessionVariableDefaultRow,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import VariableDefinitionBody
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager

logger = logging.getLogger(__name__)


def _coerce_value(
    value: str | int | float | bool | list[Any] | dict[str, Any] | None,
) -> str | int | float | bool | list[Any] | dict[str, Any] | None:
    """Coerce string representations of booleans/null/numbers to native types.

    MCP schema collapses union types (str|int|float|bool|None) to "string",
    so agents send "true"/"false" as strings. Without coercion, "false" is
    truthy and breaks workflow gate conditions like require_task_before_edit.
    """
    # Lists and dicts pass through without coercion
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in ("true", "false"):
            return stripped == "true"
        if stripped in ("null", "none"):
            return None
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                pass
    return value


def _is_valid_skill_list(value: object) -> bool:
    """Return whether value is a JSON skill-name array."""
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def set_variable(
    session_manager: SessionManager,
    db: HubDatabase,
    name: str,
    value: str | int | float | bool | list[Any] | dict[str, Any] | None,
    session_id: str,
    scope: Literal["session", "step"] = "session",
    instance_manager: AgentStepInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """
    Set a variable scoped to the session or the session's agent-step instance.

    When `scope` is ``step``, writes to the single typed instance via
    ``merge_variables``. When `scope` is ``session``, writes to session-scoped
    shared variables (via SessionVariableManager).
    """

    # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if name in SKILL_LIST_VARIABLE_NAMES and not _is_valid_skill_list(value):
        return {
            "success": False,
            "error": (f"Variable '{name}' requires a JSON array of non-empty skill names."),
        }

    # Coerce value types
    value = _coerce_value(value)

    if is_reserved_workflow_variable(name):
        return {
            "success": False,
            "error": f"{name} is managed by the workflow runtime and cannot be set directly.",
        }

    # Resolve session_task references (#N or N) to UUIDs upfront
    if name == "session_task" and isinstance(value, str):
        try:
            value = resolve_session_task_value(value, resolved_session_id, session_manager, db)
        except (ValueError, KeyError) as e:
            logger.warning(
                "Failed to resolve session_task value '%s' for session %s: %s",
                value,
                resolved_session_id,
                e,
            )
            return {
                "success": False,
                "error": f"Failed to resolve session_task value '{value}': {e}",
            }

    if scope == "step":
        if instance_manager is None:
            instance_manager = AgentStepInstanceManager(db)
        if instance_manager.merge_variables(resolved_session_id, {name: value}) is None:
            return {
                "success": False,
                "error": "No agent-step instance found for session",
            }
        return {"success": True, "value": value, "scope": "step"}

    # Session-scoped: write to session_variables table
    if not session_var_manager:
        from gobby.workflows.state_manager import SessionVariableManager

        session_var_manager = SessionVariableManager(db)

    session_var_manager.set_variable(resolved_session_id, name, value)
    return {"success": True, "value": value, "scope": "session"}


def get_variable(
    session_manager: SessionManager,
    db: HubDatabase,
    name: str | None = None,
    session_id: str = "",
    scope: Literal["session", "step"] = "session",
    instance_manager: AgentStepInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """
    Get variable(s) scoped to the session or the session's agent-step instance.

    When `scope` is ``step``, reads from the single typed instance. When
    `scope` is ``session``, reads from session-scoped shared variables.
    """
    # Require explicit session_id to prevent cross-session bleed
    if not session_id:
        return {
            "success": False,
            "error": "session_id is required. Pass the session ID explicitly to prevent cross-session variable bleed.",
        }

    # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if scope == "step":
        if instance_manager is None:
            instance_manager = AgentStepInstanceManager(db)
        instance = instance_manager.get_for_session(resolved_session_id)
        if instance is None:
            return {
                "success": False,
                "error": "No agent-step instance found for session",
            }
        variables = instance.variables
        if name:
            return {
                "success": True,
                "session_id": resolved_session_id,
                "variable": name,
                "value": variables.get(name),
                "exists": name in variables,
                "scope": "step",
            }
        return {
            "success": True,
            "session_id": resolved_session_id,
            "variables": variables,
            "scope": "step",
        }

    # Session-scoped: read from session_variables table
    if not session_var_manager:
        from gobby.workflows.state_manager import SessionVariableManager

        session_var_manager = SessionVariableManager(db)

    variables = session_var_manager.get_variables(resolved_session_id)
    if name:
        return {
            "success": True,
            "session_id": resolved_session_id,
            "variable": name,
            "value": variables.get(name),
            "exists": name in variables,
            "scope": "session",
        }
    return {
        "success": True,
        "session_id": resolved_session_id,
        "variables": variables,
        "scope": "session",
    }


def save_variable_template(
    db: HubDatabase,
    name: str,
    definition: dict[str, Any],
    *,
    make_global: bool = False,
) -> dict[str, Any]:
    """Save a variable definition as a YAML template for persistence.

    Writes to .gobby/workflows/variables/ (project) or
    ~/.gobby/workflows/variables/ (global).

    Args:
        db: Database connection
        name: Variable name
        definition: Variable definition dict (type, default, description)
        make_global: Write to global ~/.gobby/workflows/ instead of project

    Returns:
        Dict with success and path to written file
    """
    from pathlib import Path

    from gobby.utils.dev import is_dev_mode
    from gobby.workflows.template_writer import write_variable_template

    project_path = Path.cwd()
    if is_dev_mode(project_path):
        return {"success": False, "error": "Auto-export disabled in dev mode"}

    if make_global:
        from gobby.paths import get_global_variables_dir

        output_dir = get_global_variables_dir()
    else:
        from gobby.paths import get_project_variables_dir

        output_dir = get_project_variables_dir(project_path)

    try:
        path = write_variable_template(
            name=name,
            definition=definition,
            output_dir=output_dir,
        )
        logger.info("Saved variable template '%s' to %s", name, path)
        return {"success": True, "path": str(path)}
    except Exception as e:
        return {"success": False, "error": f"Failed to write variable template: {e}"}


# ═══════════════════════════════════════════════════════════════════════════
# Variable definition CRUD (DB-backed, session_variable_defaults)
# ═══════════════════════════════════════════════════════════════════════════


def _variable_summary(row: SessionVariableDefaultRow) -> dict[str, Any]:
    """Build a summary dict for a variable definition row."""
    return {
        "id": row.id,
        "name": row.name,
        "variable": row.name,
        "value": row.default_value,
        "description": row.description,
        "enabled": row.enabled,
        "source": row.source,
        "tags": row.tags,
        "project_id": row.project_id,
    }


def _export_row(row: SessionVariableDefaultRow) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        name=row.name,
        workflow_type="variable",
        definition_json=json.dumps(
            {
                "variable": row.name,
                "value": row.default_value,
                "description": row.description,
            }
        ),
        tags=row.tags,
    )


def list_variables(
    def_manager: SessionVariableDefaultManager,
    enabled: bool | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """List variable definitions with optional filters.

    Args:
        def_manager: Definition storage manager
        enabled: Filter by enabled status
        project_id: Filter by project ID

    Returns:
        Dict with success, variables list, and count
    """
    rows = def_manager.list_all(enabled=enabled, project_id=project_id)
    variables = [_variable_summary(r) for r in rows]
    return {"success": True, "variables": variables, "count": len(variables)}


def get_variable_definition(
    def_manager: SessionVariableDefaultManager,
    name: str,
) -> dict[str, Any]:
    """Get a variable definition by name.

    Args:
        def_manager: Definition storage manager
        name: Variable definition name

    Returns:
        Dict with success and variable detail, or error if not found
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Variable '{name}' not found"}

    return {"success": True, "variable": _variable_summary(row)}


def create_variable(
    def_manager: SessionVariableDefaultManager,
    name: str,
    value: Any,
    description: str | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """Create a new variable definition.

    Validates with VariableDefinitionBody before inserting.
    Auto-exports to YAML for persistence.

    Args:
        def_manager: Definition storage manager
        name: Variable name (must be unique)
        value: Default value for the variable
        description: Optional description
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success and created variable, or error
    """
    try:
        VariableDefinitionBody(variable=name, value=value, description=description)
    except Exception as e:
        return {"success": False, "error": f"Validation failed: {e}"}

    existing = def_manager.get_by_name(name)
    if existing is not None:
        if "gobby" in (existing.tags or []):
            return {
                "success": False,
                "error": (
                    f"Variable '{name}' conflicts with a bundled gobby template. "
                    "Choose a different name."
                ),
            }
        return {"success": False, "error": f"Variable '{name}' already exists"}

    deleted_row = def_manager.get_by_name(name, include_deleted=True)
    if deleted_row is not None and deleted_row.deleted_at:
        def_manager.hard_delete(deleted_row.id)

    row = def_manager.create(
        name=name,
        default_value=value,
        description=description,
        enabled=True,
        source="installed",
        tags=["user"],
    )
    logger.info("Created variable '%s' (id=%s)", name, row.id)

    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(_export_row(row), project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning("Failed to auto-export variable '%s': %s", name, e)

    return {"success": True, "variable": _variable_summary(row)}


def update_variable(
    def_manager: SessionVariableDefaultManager,
    name: str,
    value: Any = None,
    description: str | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """Update a variable definition by name.

    Writes typed columns through SessionVariableDefaultManager.update.

    Args:
        def_manager: Definition storage manager
        name: Variable name
        value: New default value (None = keep existing)
        description: New description (None = keep existing)
        project_path: Project root for auto-export
        make_global_template: If True, export to ~/.gobby/workflows/ instead

    Returns:
        Dict with success and updated variable, or error
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Variable '{name}' not found"}

    fields: dict[str, Any] = {}
    if value is not None:
        fields["default_value"] = value
    if description is not None:
        fields["description"] = description
    if not fields:
        return {"success": True, "variable": _variable_summary(row)}

    updated = def_manager.update(row.id, **fields)
    logger.info("Updated variable '%s'", name)

    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(_export_row(updated), project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning("Failed to auto-export variable '%s': %s", name, e)

    return {"success": True, "variable": _variable_summary(updated)}


def delete_variable(
    def_manager: SessionVariableDefaultManager,
    name: str,
    force: bool = False,
    *,
    project_path: Path | None = None,
) -> dict[str, Any]:
    """Delete a variable definition by name (soft-delete).

    Bundled variables are protected unless force=True.

    Args:
        def_manager: Definition storage manager
        name: Variable name
        force: Override bundled protection
        project_path: Project root for YAML cleanup

    Returns:
        Dict with success, or error if not found/protected
    """
    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Variable '{name}' not found"}

    if "gobby" in (row.tags or []) and not force:
        return {
            "success": False,
            "error": (
                f"Variable '{name}' is bundled and will be re-created on restart. "
                "Use force=True to delete anyway."
            ),
        }

    deleted = def_manager.delete(row.id)
    if not deleted:
        return {"success": False, "error": f"Failed to delete variable '{name}'"}

    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_delete_definition

        is_user = bool(row.tags and "user" in row.tags)
        auto_delete_definition(
            name,
            "variable",
            project_path,
            delete_global=is_user,
        )
    except Exception as e:
        logger.warning("Failed to delete variable template '%s': %s", name, e)

    logger.info("Deleted variable '%s' (id=%s)", name, row.id)
    return {"success": True, "deleted": {"id": row.id, "name": row.name}}


def export_variable(
    def_manager: SessionVariableDefaultManager,
    name: str,
) -> dict[str, Any]:
    """Export a variable definition as YAML.

    Args:
        def_manager: Definition storage manager
        name: Variable name

    Returns:
        Dict with success and yaml_content, or error if not found
    """
    import yaml

    row = def_manager.get_by_name(name)
    if row is None:
        return {"success": False, "error": f"Variable '{name}' not found"}

    doc = {
        "name": row.name,
        "type": "variable",
        "variable": row.name,
        "value": row.default_value,
    }
    if row.description:
        doc["description"] = row.description

    yaml_content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
    return {"success": True, "yaml_content": yaml_content}
