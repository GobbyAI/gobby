"""Authorization guard for the stop-gate-affecting live-session label."""

from __future__ import annotations

from collections.abc import Iterable

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.utils.session_context import get_current_session_id

LIVE_SESSION_LABEL = "live-session"


def live_session_label_change_error(
    ctx: RegistryContext,
    previous_labels: Iterable[str] | None,
    next_labels: Iterable[str] | None,
    *,
    session_id: str | None = None,
) -> str | None:
    """Return an authorization error when live-session membership changes."""
    before = LIVE_SESSION_LABEL in set(previous_labels or ())
    after = LIVE_SESSION_LABEL in set(next_labels or ())
    if before == after:
        return None

    session_ref = session_id or get_current_session_id()
    if not session_ref:
        return "Changing the live-session label requires an active session context."
    try:
        resolved_session_id = ctx.resolve_session_id(session_ref)
        session = ctx.session_manager.get(resolved_session_id)
    except (KeyError, LookupError, ValueError):
        session = None
    if session is None:
        return "Changing the live-session label requires a resolvable session."
    if getattr(session, "session_type", None) != "terminal":
        return "Changing the live-session label requires an interactive terminal session."
    if getattr(session, "agent_run_id", None) or getattr(session, "agent_depth", 0):
        return "Spawned and automated sessions cannot change the live-session label."

    try:
        variables = ctx.session_var_manager.get_variables(resolved_session_id)
    except (KeyError, LookupError, ValueError):
        return "Changing the live-session label requires readable session state."
    loaded_skills = variables.get("loaded_skills")
    if not isinstance(loaded_skills, list) or "live-session" not in loaded_skills:
        return "Load the live-session skill before changing the live-session label."
    return None
