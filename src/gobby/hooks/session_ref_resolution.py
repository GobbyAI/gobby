"""Hook-boundary session reference resolution for Gobby MCP tool inputs."""

from __future__ import annotations

import logging

from gobby.hooks.events import HookEvent, HookEventType
from gobby.hooks.session_types import HookSessionManager
from gobby.utils.session_refs import try_resolve_session_field

logger = logging.getLogger(__name__)

# Variable tools intentionally keep their own session_id scope; resolving it
# here would redirect cross-session variable reads/writes.
VARIABLE_TOOLS = {"mcp__gobby__set_variable", "mcp__gobby__get_variable"}


def foreign_originator_reason(
    event: HookEvent,
    session_manager: HookSessionManager | None,
) -> str | None:
    """Return why a Gobby MCP call naming another session as its originator is refused.

    A session may originate calls only under its own session_ref: the wrapper session_id
    names the caller, so a ref naming another session, or no session at all, is refused.
    The variable tools' session_id names their target, and a target inside ``arguments``
    is never the caller.
    """
    if event.event_type != HookEventType.BEFORE_TOOL or session_manager is None:
        return None
    tool_name = (event.data or {}).get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__gobby__"):
        return None
    if tool_name in VARIABLE_TOOLS:
        return None
    own = event.metadata.get("_platform_session_id")
    tool_input = event.data.get("tool_input")
    if not isinstance(own, str) or not own or not isinstance(tool_input, dict):
        return None
    ref = tool_input.get("session_id")
    if not isinstance(ref, str) or not ref.strip() or ref == own:
        return None
    try:
        resolved: str | None = session_manager.resolve_session_reference(
            ref.strip(), event.project_id
        )
    except Exception as exc:
        logger.debug("Originator session ref %r did not resolve: %s", ref, exc)
        resolved = None
    if resolved == own:
        return None
    return (
        f"session_id {ref!r} does not name this session ({own}). A session may originate "
        "Gobby MCP calls only as itself: pass your own session_id or omit it, and name "
        "another session only inside arguments."
    )


def resolve_session_refs_in_tool_input(
    event: HookEvent,
    session_manager: HookSessionManager | None,
) -> None:
    """Resolve #N/numeric session refs to UUIDs in Gobby MCP tool inputs."""
    if event.event_type != HookEventType.BEFORE_TOOL:
        return

    tool_name = (event.data or {}).get("tool_name", "")
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__gobby__"):
        return

    tool_input = event.data.get("tool_input")
    if not isinstance(tool_input, dict) or not tool_input:
        return

    project_id = event.project_id

    if tool_name not in VARIABLE_TOOLS:
        try_resolve_session_field(
            tool_input,
            "session_id",
            session_manager=session_manager,
            project_id=project_id,
        )

    if tool_name == "mcp__gobby__call_tool":
        arguments = tool_input.get("arguments")
        if isinstance(arguments, dict):
            try_resolve_session_field(
                arguments,
                "session_id",
                session_manager=session_manager,
                project_id=project_id,
            )
