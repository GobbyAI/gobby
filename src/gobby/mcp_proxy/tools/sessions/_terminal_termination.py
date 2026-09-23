"""Explicit daemon-tracked terminal termination for gobby-sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.terminals.actor_scope import OPERATOR_ACTOR, SESSION_ACTOR_PREFIX
from gobby.terminals.termination import (
    TerminalTerminationError,
)
from gobby.terminals.termination import (
    terminate_terminal as terminate_daemon_terminal,
)
from gobby.utils.session_context import get_request_principal, get_session_context

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager


def register_terminate_terminal_tool(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    *,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> None:
    """Register the explicit operation that can terminate external terminals."""

    @registry.tool(
        name="terminate_terminal",
        description=(
            "Explicitly terminate a daemon-tracked terminal by terminal id or root-session "
            "reference. This can terminate an externally owned terminal; close_pane, "
            "close_tab, and close_workspace only kill terminals they own."
        ),
    )
    async def terminate_terminal(reference: str) -> dict[str, Any]:
        if terminal_manager is None or terminal_runtime_registry is None:
            return {
                "success": False,
                "error": "Daemon-tracked terminal service is unavailable",
                "error_code": "terminal_service_unavailable",
            }

        try:
            try:
                principal = await get_request_principal()
            except LookupError as exc:
                raise TerminalTerminationError(
                    "forbidden", "Terminal termination requires authenticated request context"
                ) from exc
            if principal is not None:
                raise TerminalTerminationError(
                    "forbidden",
                    "Terminal termination is only available to a session or the local operator",
                )
            context = get_session_context()
            actor = (
                OPERATOR_ACTOR if context is None else f"{SESSION_ACTOR_PREFIX}{context.session_id}"
            )
            exited = await terminate_daemon_terminal(
                terminal_manager,
                terminal_runtime_registry,
                session_manager,
                actor=actor,
                reference=reference,
            )
        except TerminalTerminationError as exc:
            return {"success": False, "error": str(exc), "error_code": exc.code}
        return {"success": True, "terminal_id": exited.id, "state": exited.state}
