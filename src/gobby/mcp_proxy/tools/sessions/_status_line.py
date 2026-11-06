"""set_status_line: a session sets or clears the one-line status it shows about its work."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import STATUS_LINE_MAX_LENGTH
from gobby.storage.sessions._session_metadata_update import normalize_status_line


def _refused(error: str, error_code: str) -> dict[str, Any]:
    return {"success": False, "error": error, "error_code": error_code}


def register_status_line_tools(
    registry: InternalToolRegistry, session_manager: SessionManager
) -> None:
    """Register set_status_line; it writes only the calling session's own row."""

    @registry.tool(
        name="set_status_line",
        description=(
            "Set or clear the one-line status this session shows about its current work. "
            f"Pass null or a blank line to clear it. At most {STATUS_LINE_MAX_LENGTH} "
            "characters. Only the calling session's own line can be set."
        ),
    )
    async def set_status_line(
        status_line: str | None, session_id: str | None = None
    ) -> dict[str, Any]:
        """Set (or clear, with null or blank) the calling session's status line.

        `session_id` may name only the caller itself; any other session is refused.
        """
        from gobby.utils.session_context import get_current_session_id

        caller_ref = get_current_session_id()
        caller = None
        if caller_ref:
            try:
                caller = session_manager.get(session_manager.resolve_session_reference(caller_ref))
            except ValueError:
                caller = None
        if caller is None:
            return _refused(
                "set_status_line needs a session context; no calling session resolved.",
                "status_line_caller_unknown",
            )
        if session_id is not None:
            try:
                named = session_manager.resolve_session_reference(session_id, caller.project_id)
            except ValueError:
                named = None
            if named != caller.id:
                return _refused(
                    "set_status_line can set only the calling session's own line.",
                    "status_line_target_forbidden",
                )
        try:
            written = await asyncio.to_thread(
                session_manager.set_status_line, caller.id, status_line
            )
        except ValueError as error:
            return _refused(str(error), "status_line_too_long")
        if not written:
            return _refused(
                "The calling session's row was not found.", "status_line_caller_unknown"
            )
        text = normalize_status_line(status_line)
        return {"success": True, "session_id": caller.id, "status_line": text}
