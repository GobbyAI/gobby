"""Hook-boundary session reference resolution for Gobby MCP tool inputs."""

from __future__ import annotations

from gobby.hooks.events import HookEvent, HookEventType
from gobby.hooks.session_types import HookSessionManager
from gobby.utils.session_refs import try_resolve_session_field

# Variable tools intentionally keep their own session_id scope; resolving it
# here would redirect cross-session variable reads/writes.
VARIABLE_TOOLS = {"mcp__gobby__set_variable", "mcp__gobby__get_variable"}


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
