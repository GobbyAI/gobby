"""
Workflow variable tools (set_variable, get_variable).
"""

import logging
from typing import Any

from gobby.mcp_proxy.tools.workflows._resolution import (
    resolve_session_id,
    resolve_session_task_value,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sessions import LocalSessionManager
from gobby.workflows.state_manager import (
    SessionVariableManager,
    WorkflowInstanceManager,
)

logger = logging.getLogger(__name__)


def _coerce_value(value: str | int | float | bool | None) -> str | int | float | bool | None:
    """Coerce string representations of booleans/null/numbers to native types.

    MCP schema collapses union types (str|int|float|bool|None) to "string",
    so agents send "true"/"false" as strings. Without coercion, "false" is
    truthy and breaks workflow gate conditions like pending_memory_review.
    """
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
    session_manager: LocalSessionManager,
    db: DatabaseProtocol,
    name: str,
    value: str | int | float | bool | None,
    session_id: str | None = None,
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
        session_manager: LocalSessionManager instance
        db: LocalDatabase instance
        name: Variable name (e.g., "session_epic", "is_worktree")
        value: Variable value (string, number, boolean, or null)
        session_id: Session reference (accepts #N, N, UUID, or prefix)
        workflow: Optional workflow name to scope the variable to
        instance_manager: Optional WorkflowInstanceManager for workflow-scoped writes
        session_var_manager: Optional SessionVariableManager for session-scoped writes

    Returns:
        Success status and updated variables
    """
    # Require explicit session_id to prevent cross-session bleed
    if not session_id:
        return {
            "ok": False,
            "error": "session_id is required. Pass the session ID explicitly to prevent cross-session variable bleed.",
        }

    # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

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
                "ok": False,
                "error": f"Failed to resolve session_task value '{value}': {e}",
            }

    # Workflow-scoped: write to workflow_instances.variables
    if workflow:
        if not instance_manager:
            return {"ok": False, "error": "Workflow-scoped variables require instance_manager"}
        instance = instance_manager.get_instance(resolved_session_id, workflow)
        if not instance:
            return {
                "ok": False,
                "error": f"No workflow instance '{workflow}' found for session",
            }
        instance.variables[name] = value
        instance_manager.save_instance(instance)
        return {"ok": True, "value": value, "scope": "workflow", "workflow": workflow}

    # Session-scoped: write to session_variables table
    if not session_var_manager:
        from gobby.workflows.state_manager import SessionVariableManager

        session_var_manager = SessionVariableManager(db)

    session_var_manager.set_variable(resolved_session_id, name, value)
    return {"ok": True, "value": value, "scope": "session"}


def get_variable(
    session_manager: LocalSessionManager,
    db: DatabaseProtocol,
    name: str | None = None,
    session_id: str | None = None,
    workflow: str | None = None,
    instance_manager: WorkflowInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """
    Get variable(s) scoped to a workflow instance or session.

    When `workflow` is provided, reads from that workflow instance's variables.
    When `workflow` is not provided, reads from session-scoped shared variables.

    Args:
        session_manager: LocalSessionManager instance
        db: LocalDatabase instance
        name: Variable name to get (if None, returns all variables)
        session_id: Session reference (accepts #N, N, UUID, or prefix)
        workflow: Optional workflow name to scope the read to
        instance_manager: Optional WorkflowInstanceManager for workflow-scoped reads
        session_var_manager: Optional SessionVariableManager for session-scoped reads

    Returns:
        Variable value(s) and session info
    """
    # Require explicit session_id to prevent cross-session bleed
    if not session_id:
        return {
            "ok": False,
            "error": "session_id is required. Pass the session ID explicitly to prevent cross-session variable bleed.",
        }

    # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    # Workflow-scoped: read from workflow_instances.variables
    if workflow:
        if not instance_manager:
            return {"ok": False, "error": "Workflow-scoped variables require instance_manager"}
        instance = instance_manager.get_instance(resolved_session_id, workflow)
        if not instance:
            return {
                "ok": False,
                "error": f"No workflow instance '{workflow}' found for session",
            }
        variables = instance.variables
        if name:
            return {
                "ok": True,
                "session_id": resolved_session_id,
                "variable": name,
                "value": variables.get(name),
                "exists": name in variables,
                "scope": "workflow",
                "workflow": workflow,
            }
        return {
            "ok": True,
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
            "ok": True,
            "session_id": resolved_session_id,
            "variable": name,
            "value": variables.get(name),
            "exists": name in variables,
            "scope": "session",
        }
    return {
        "ok": True,
        "session_id": resolved_session_id,
        "variables": variables,
        "scope": "session",
    }


def set_session_variable(
    session_manager: LocalSessionManager,
    session_var_manager: SessionVariableManager,
    name: str,
    value: str | int | float | bool | None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """
    Set a session-scoped shared variable (visible to all workflows).

    Args:
        session_manager: LocalSessionManager instance
        session_var_manager: SessionVariableManager instance
        name: Variable name
        value: Variable value
        session_id: Session reference (accepts #N, N, UUID, or prefix)

    Returns:
        Success status
    """
    if not session_id:
        return {
            "ok": False,
            "error": "session_id is required.",
        }

    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    value = _coerce_value(value)
    session_var_manager.set_variable(resolved_session_id, name, value)
    return {"ok": True, "value": value, "scope": "session"}
