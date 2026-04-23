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
from typing import Any

from gobby.mcp_proxy.tools.workflows._resolution import (
    resolve_session_id,
    resolve_session_task_value,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sessions import SessionManager
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    WorkflowDefinitionRow,
)
from gobby.workflows.definitions import VariableDefinitionBody
from gobby.workflows.state_manager import (
    SessionVariableManager,
    WorkflowInstanceManager,
)

logger = logging.getLogger(__name__)


def _coerce_value(
    value: str | int | float | bool | list[Any] | dict[str, Any] | None,
) -> str | int | float | bool | list[Any] | dict[str, Any] | None:
    """Coerce string representations of booleans/null/numbers to native types.

    MCP schema collapses union types (str|int|float|bool|None) to "string",
    so agents send "true"/"false" as strings. Without coercion, "false" is
    truthy and breaks workflow gate conditions like memory_review_completed.
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


def set_variable(
    session_manager: SessionManager,
    db: DatabaseProtocol,
    name: str,
    value: str | int | float | bool | list[Any] | dict[str, Any] | None,
    session_id: str,
    workflow: str | None = None,
    instance_manager: WorkflowInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """
    Set a variable scoped to a workflow instance or session.

    When `workflow` is provided, writes to that workflow instance's variables.
    When `workflow` is not provided, writes to session-scoped shared variables
    (via SessionVariableManager).

    Args:
        session_manager: SessionManager instance
        db: LocalDatabase instance
        name: Variable name (e.g., "session_epic", "is_worktree")
        value: Variable value (string, number, boolean, or null)
        session_id: Session reference (accepts #N, N, UUID, or prefix). Required to prevent cross-session variable bleed.
        workflow: Optional workflow name to scope the variable to
        instance_manager: Optional WorkflowInstanceManager for workflow-scoped writes
        session_var_manager: Optional SessionVariableManager for session-scoped writes

    Returns:
        Success status and updated variables
    """

    # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    # Coerce value types
    value = _coerce_value(value)

    # Resolve session_task references (#N or N) to UUIDs upfront
    if name == "session_task" and isinstance(value, str):
        try:
            value = resolve_session_task_value(value, resolved_session_id, session_manager, db)
        except (ValueError, KeyError) as e:
            logger.warning(
                f"Failed to resolve session_task value '{value}' for session {resolved_session_id}: {e}"
            )
            return {
                "success": False,
                "error": f"Failed to resolve session_task value '{value}': {e}",
            }

    # Workflow-scoped: write to workflow_instances.variables
    if workflow:
        if not instance_manager:
            return {"success": False, "error": "Workflow-scoped variables require instance_manager"}
        instance = instance_manager.get_instance(resolved_session_id, workflow)
        if not instance:
            return {
                "success": False,
                "error": f"No workflow instance '{workflow}' found for session",
            }
        instance.variables[name] = value
        instance_manager.save_instance(instance)
        return {"success": True, "value": value, "scope": "workflow", "workflow": workflow}

    # Session-scoped: write to session_variables table
    if not session_var_manager:
        from gobby.workflows.state_manager import SessionVariableManager

        session_var_manager = SessionVariableManager(db)

    session_var_manager.set_variable(resolved_session_id, name, value)
    return {"success": True, "value": value, "scope": "session"}


def get_variable(
    session_manager: SessionManager,
    db: DatabaseProtocol,
    name: str | None = None,
    session_id: str = "",
    workflow: str | None = None,
    instance_manager: WorkflowInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """
    Get variable(s) scoped to a workflow instance or session.

    When `workflow` is provided, reads from that workflow instance's variables.
    When `workflow` is not provided, reads from session-scoped shared variables.

    Args:
        session_manager: SessionManager instance
        db: LocalDatabase instance
        name: Variable name to get (if None, returns all variables)
        session_id: Session reference (accepts #N, N, UUID, or prefix). Required to prevent cross-session variable bleed.
        workflow: Optional workflow name to scope the read to
        instance_manager: Optional WorkflowInstanceManager for workflow-scoped reads
        session_var_manager: Optional SessionVariableManager for session-scoped reads

    Returns:
        Variable value(s) and session info
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

    # Workflow-scoped: read from workflow_instances.variables
    if workflow:
        if not instance_manager:
            return {"success": False, "error": "Workflow-scoped variables require instance_manager"}
        instance = instance_manager.get_instance(resolved_session_id, workflow)
        if not instance:
            return {
                "success": False,
                "error": f"No workflow instance '{workflow}' found for session",
            }
        variables = instance.variables
        if name:
            return {
                "success": True,
                "session_id": resolved_session_id,
                "variable": name,
                "value": variables.get(name),
                "exists": name in variables,
                "scope": "workflow",
                "workflow": workflow,
            }
        return {
            "success": True,
            "session_id": resolved_session_id,
            "variables": variables,
            "scope": "workflow",
            "workflow": workflow,
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
    db: DatabaseProtocol,
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
        logger.info(f"Saved variable template '{name}' to {path}")
        return {"success": True, "path": str(path)}
    except Exception as e:
        return {"success": False, "error": f"Failed to write variable template: {e}"}


# ═══════════════════════════════════════════════════════════════════════════
# Variable definition CRUD (DB-backed, workflow_type='variable')
# ═══════════════════════════════════════════════════════════════════════════


def _variable_summary(row: WorkflowDefinitionRow) -> dict[str, Any]:
    """Build a summary dict for a variable definition row."""
    body = json.loads(row.definition_json)
    return {
        "id": row.id,
        "name": row.name,
        "variable": body.get("variable"),
        "value": body.get("value"),
        "description": body.get("description") or row.description,
        "enabled": row.enabled,
        "source": row.source,
        "tags": row.tags,
        "project_id": row.project_id,
    }


def list_variables(
    def_manager: LocalWorkflowDefinitionManager,
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
    rows = def_manager.list_all(workflow_type="variable", enabled=enabled, project_id=project_id)
    variables = [_variable_summary(r) for r in rows]
    return {"success": True, "variables": variables, "count": len(variables)}


def get_variable_definition(
    def_manager: LocalWorkflowDefinitionManager,
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
    if row is None or row.workflow_type != "variable":
        return {"success": False, "error": f"Variable '{name}' not found"}

    return {"success": True, "variable": _variable_summary(row)}


def create_variable(
    def_manager: LocalWorkflowDefinitionManager,
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
    # Validate with Pydantic
    try:
        body = VariableDefinitionBody(variable=name, value=value, description=description)
    except Exception as e:
        return {"success": False, "error": f"Validation failed: {e}"}

    # Name collision check
    from gobby.mcp_proxy.tools.workflows._auto_export import has_gobby_name_collision

    if has_gobby_name_collision(def_manager.db, name):
        return {
            "success": False,
            "error": f"Variable '{name}' conflicts with a bundled gobby template. Choose a different name.",
        }

    # Check for duplicate name
    existing = def_manager.get_by_name(name)
    if existing is not None:
        return {"success": False, "error": f"Variable '{name}' already exists"}

    # Hard-delete any soft-deleted variable that would block the UNIQUE constraint
    deleted_row = def_manager.get_by_name(name, include_deleted=True)
    if (
        deleted_row is not None
        and deleted_row.deleted_at
        and deleted_row.workflow_type == "variable"
    ):
        def_manager.hard_delete(deleted_row.id)

    row = def_manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="variable",
        description=description,
        enabled=True,
        source="installed",
        tags=["user"],
    )
    logger.info(f"Created variable '{name}' (id={row.id})")

    # Auto-export to YAML
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(row, project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning(f"Failed to auto-export variable '{name}': {e}")

    return {"success": True, "variable": _variable_summary(row)}


def update_variable(
    def_manager: LocalWorkflowDefinitionManager,
    name: str,
    value: Any = None,
    description: str | None = None,
    *,
    project_path: Path | None = None,
    make_global_template: bool = False,
) -> dict[str, Any]:
    """Update a variable definition by name.

    Merges changed fields into the existing definition_json.

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
    if row is None or row.workflow_type != "variable":
        return {"success": False, "error": f"Variable '{name}' not found"}

    body = json.loads(row.definition_json)
    fields: dict[str, Any] = {}

    if value is not None:
        body["value"] = value
    if description is not None:
        body["description"] = description
        fields["description"] = description

    fields["definition_json"] = json.dumps(body)
    updated = def_manager.update(row.id, **fields)
    logger.info(f"Updated variable '{name}'")

    # Auto-export updated YAML
    try:
        from gobby.mcp_proxy.tools.workflows._auto_export import auto_export_definition

        auto_export_definition(updated, project_path, make_global=make_global_template)
    except Exception as e:
        logger.warning(f"Failed to auto-export variable '{name}': {e}")

    return {"success": True, "variable": _variable_summary(updated)}


def delete_variable(
    def_manager: LocalWorkflowDefinitionManager,
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
    if row is None or row.workflow_type != "variable":
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

    # Remove YAML template file
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
        logger.warning(f"Failed to delete variable template '{name}': {e}")

    logger.info(f"Deleted variable '{name}' (id={row.id})")
    return {"success": True, "deleted": {"id": row.id, "name": row.name}}


def export_variable(
    def_manager: LocalWorkflowDefinitionManager,
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
    if row is None or row.workflow_type != "variable":
        return {"success": False, "error": f"Variable '{name}' not found"}

    body = json.loads(row.definition_json)
    doc = {
        "name": row.name,
        "type": "variable",
        "variable": body.get("variable", row.name),
        "value": body.get("value"),
    }
    if body.get("description"):
        doc["description"] = body["description"]

    yaml_content = yaml.dump(doc, default_flow_style=False, sort_keys=False)
    return {"success": True, "yaml_content": yaml_content}
