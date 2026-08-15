"""Query tools for agent-step instances."""

from typing import Any

from gobby.mcp_proxy.tools.workflows._resolution import resolve_session_id
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager


def get_step_status(
    session_manager: SessionManager,
    session_id: str | None = None,
    instance_manager: AgentStepInstanceManager | None = None,
    session_var_manager: SessionVariableManager | None = None,
) -> dict[str, Any]:
    """Report the session's typed agent-step instance and session variables."""
    if not session_id:
        return {
            "success": False,
            "has_workflow": False,
            "error": "session_id is required. Pass the session ID explicitly to prevent cross-session variable bleed.",
        }

    try:
        resolved_session_id = resolve_session_id(session_manager, session_id)
    except ValueError as e:
        return {"success": False, "has_workflow": False, "error": str(e)}

    session_vars = (
        session_var_manager.get_variables(resolved_session_id) if session_var_manager else {}
    )

    if instance_manager is None:
        instance = None
    else:
        instance = instance_manager.get_for_session(resolved_session_id)

    if instance is None:
        return {
            "success": True,
            "has_workflow": False,
            "session_id": resolved_session_id,
            "session_variables": session_vars,
        }

    return {
        "success": True,
        "has_workflow": True,
        "session_id": resolved_session_id,
        "agent_name": instance.agent_name,
        "current_step": instance.current_step,
        "steps": [step.name for step in instance.snapshot.steps],
        "exit_condition": instance.snapshot.exit_condition,
        "variables": instance.variables,
        "session_variables": session_vars,
    }
