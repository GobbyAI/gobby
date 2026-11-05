"""Fail closed when a session's named agent definition no longer resolves."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookResponse
from gobby.workflows.selectors import binds_named_definition

if TYPE_CHECKING:
    from gobby.workflows.engine.core import RuleEngine

UNRESOLVED_DEFINITION_RULE = "unresolved-agent-definition"


def unresolved_definition_block(
    engine: RuleEngine,
    session_id: str,
    variables: dict[str, Any],
    project_id: str | None,
) -> HookResponse | None:
    """Block a tool call when the session's named definition is missing or disabled.

    The default baseline is for unbound sessions only. A seat assigned a named definition
    that was later removed or disabled must not inherit it.
    """
    agent_type = variables.get("_agent_type")
    if not binds_named_definition(agent_type):
        return None
    if engine._load_active_agent_definition(agent_type, project_id=project_id) is not None:
        return None
    reason = (
        f"Rule enforced by Gobby: [{UNRESOLVED_DEFINITION_RULE}]\n"
        f"This session's agent definition '{agent_type}' is missing or disabled, so it has "
        "no tool authority. Restore or re-enable the definition from the web UI or another "
        "session, or reassign this seat. Ending the turn stays allowed."
    )
    reason = engine._record_enforcement_denial(
        session_id=session_id,
        rule=UNRESOLVED_DEFINITION_RULE,
        target=f"definition:{agent_type}",
        reason=reason,
    )
    return HookResponse(decision="block", reason=reason)
