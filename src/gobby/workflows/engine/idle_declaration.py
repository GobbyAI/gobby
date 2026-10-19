"""Native variable authority and the idle declaration predicate.

An idle step lets its seat stop after declaring ``seat_idle``. Native
``set_variable`` otherwise skips step allowlists as an infrastructure tool; at an
idle step it meets the step's ``allowed_tools`` instead, and the guard admits the
declaration itself: ``seat_idle`` set to true, in session scope, on the caller's
own session. Only assistant, orchestrator and default definitions retain native
writes; other seats declare idleness through a readiness-message rule effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.workflows.enforcement.blocking import canonical_gobby_tool_name

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.definitions import WorkflowStep

IDLE_VARIABLE = "seat_idle"
SET_VARIABLE_TOOL = "mcp__gobby__set_variable"
NATIVE_VARIABLE_ROLES = frozenset({"assistant", "orchestrator", "default"})


def native_variable_write_error(
    tool_name: str,
    tool_input: Any,
    caller_id: str | None,
    variables: dict[str, Any],
    sessions: SessionManager | None,
) -> str | None:
    """Refuse native writes outside the authenticated caller's own allowed role."""
    if canonical_gobby_tool_name(tool_name) != SET_VARIABLE_TOOL:
        return None
    prefix = "Rule enforced by Gobby: [native-variable-authority]\n"
    role = variables.get("_agent_type")
    if "_agent_type" in variables and (
        not isinstance(role, str) or role not in NATIVE_VARIABLE_ROLES
    ):
        return prefix + f"Native set_variable is blocked for agent definition {role!r}."
    if not caller_id or sessions is None:
        return prefix + "Native set_variable requires a resolved calling session."
    caller = sessions.get(caller_id)
    if caller is None:
        return prefix + "Native set_variable requires a resolved calling session."
    if "_agent_type" not in variables:
        # An ordinary session without a seat is default. A bound seat missing its
        # protected role is corrupt authority, never an implicit default grant.
        from gobby.storage.agents import LocalAgentRunManager
        from gobby.workflows.step_instances import AgentStepInstanceManager

        instance = AgentStepInstanceManager(sessions.db).get_for_session(caller_id)
        active_run = LocalAgentRunManager(sessions.db).get_by_session(caller_id)
        if active_run is not None or (instance is not None and instance.enabled):
            return prefix + "The calling seat has no protected active agent definition."
    target = tool_input.get("session_id") if isinstance(tool_input, dict) else None
    if not isinstance(target, str) or not target:
        return prefix + "Native set_variable writes only the calling session."
    try:
        resolved = sessions.resolve_session_reference(target, caller.project_id)
    except ValueError:
        return prefix + "Native set_variable writes only the calling session."
    if resolved != caller_id:
        return prefix + "Native set_variable cannot write another session."
    return None


def reserved_variable_block_guidance(variable_name: str, step_name: str | None) -> str:
    """Recovery guidance for the existing runtime-variable refusal."""
    step_scope = f" in the '{step_name}' step" if step_name else ""
    return (
        f"\nAbandon only this write to variable '{variable_name}'{step_scope}; "
        "set_variable remains available for other permitted variables. Continue with "
        "an allowed operation and do not retry this variable write. Repeating an "
        "identical denial three times ends an autonomous run in a terminal blocked state."
    )


def readiness_message_completed(
    sessions: SessionManager | None, caller_id: str, tool_output: Any
) -> bool:
    """Recognize only this caller's persisted readiness send to another session."""
    if sessions is None or not caller_id:
        return False
    from gobby.workflows.engine.seat_messages import completion_messages

    return any(
        message.content.partition("\n")[0] == "EVENT=SEAT_READY NOTE=idle"
        for message in completion_messages(sessions.db, caller_id, "send_message", {}, tool_output)
    )


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
