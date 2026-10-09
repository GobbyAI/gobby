"""The operator valve that vouches for a session's composer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.terminals.composer_ledger import ComposerReleaseError, release_composer
from gobby.utils.session_context import get_current_session_id, resolve_session_ref

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager
    from gobby.storage.terminals import TerminalManager


def register_release_composer_tool(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    *,
    terminal_manager: TerminalManager | None,
) -> None:
    """Register the valve that starts another session's composer clean."""

    @registry.tool(
        name="release_composer",
        description=(
            "Vouch that another session's composer is empty so automatic writes resume. "
            "Use after confirming the pane holds no draft or limit menu: a seat spawned "
            "before the composer ledger, or one held after a provider usage limit. "
            "A session cannot release its own composer."
        ),
    )
    async def release_composer_tool(session_id: str) -> dict[str, Any]:
        if terminal_manager is None:
            return {
                "success": False,
                "error": "Daemon-tracked terminal service is unavailable",
                "error_code": "terminal_service_unavailable",
            }
        try:
            target_id = resolve_session_ref(session_manager, session_id)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "error_code": "session_not_found"}
        try:
            terminal_id = release_composer(
                terminal_manager, target_id, caller_session_id=get_current_session_id()
            )
        except ComposerReleaseError as exc:
            return {"success": False, "error": str(exc), "error_code": exc.code}
        return {"success": True, "session_id": target_id, "terminal_id": terminal_id}
