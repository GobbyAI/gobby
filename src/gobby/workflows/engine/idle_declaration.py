"""The one native write a standing seat's idle step admits (#24405).

An idle step lets its seat stop after declaring ``seat_idle``. Native
``set_variable`` otherwise skips step allowlists as an infrastructure tool; at an
idle step it meets the step's ``allowed_tools`` instead, and the guard admits the
declaration itself: ``seat_idle`` set to true, in session scope, on the caller's
own session. A step that does not list ``set_variable`` refuses every other write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.definitions import WorkflowStep

IDLE_VARIABLE = "seat_idle"
SET_VARIABLE_TOOL = "mcp__gobby__set_variable"


def gates_idle_write(step: WorkflowStep, canonical_tool: str) -> bool:
    """Whether an idle step holds native set_variable to its allowlist."""
    return step.idle and canonical_tool == SET_VARIABLE_TOOL


def _is_true(value: Any) -> bool:
    """Match set_variable's coercion: true or the string "true"."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def is_idle_declaration(
    step: WorkflowStep,
    canonical_tool: str,
    tool_input: Any,
    session_id: str,
    session_manager: SessionManager | None,
) -> bool:
    """Whether this native call is the idle step's own-session idle declaration."""
    if not gates_idle_write(step, canonical_tool):
        return False
    if not isinstance(tool_input, dict) or session_manager is None:
        return False
    if tool_input.get("name") != IDLE_VARIABLE or not _is_true(tool_input.get("value")):
        return False
    if tool_input.get("scope", "session") != "session":
        return False
    target = tool_input.get("session_id")
    if not isinstance(target, str) or not target:
        return False
    caller = session_manager.get(session_id)
    if caller is None:
        return False
    try:
        return session_manager.resolve_session_reference(target, caller.project_id) == session_id
    except ValueError:
        return False
