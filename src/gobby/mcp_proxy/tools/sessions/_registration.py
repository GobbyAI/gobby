"""Session registration tools for hookless clients.

Provides MCP tools for clients that don't use Gobby's hook system
(e.g., Agent SDK apps) to register their sessions.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from gobby.sessions.terminal_binding import repair_native_terminal_binding
from gobby.storage.machines import MachineNotRegisteredError
from gobby.storage.sessions._title_defaults import manual_title_source
from gobby.storage.sessions._update_sentinel import UNSET

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager


def register_registration_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
) -> None:
    """
    Register session registration tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: SessionManager instance for session operations
    """

    @registry.tool(
        name="register_session",
        description="""Register a session with Gobby. For hookless clients (Agent SDK, etc.)
that don't trigger SessionStart hooks.

Idempotent: calling with the same external_id + source + project_id
returns the existing session instead of creating a duplicate.

machine_id and project_id are auto-resolved from the local environment if omitted.""",
    )
    async def register_session(
        external_id: str,
        source: str,
        machine_id: str | None = None,
        project_id: str | None = None,
        title: str | None = None,
        git_branch: str | None = None,
        parent_session_id: str | None = None,
        agent_depth: int = 0,
        sandbox_enabled: bool | None = None,
        native_terminal_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Register a session for a hookless client.

        Args:
            external_id: Client's session ID (required)
            source: CLI type - claude, codex, grok, agent-sdk, etc. (required)
            machine_id: Machine identifier (auto-resolved if omitted)
            project_id: Project ID (auto-resolved from .gobby/project.json if omitted)
            title: Session title
            git_branch: Git branch name
            parent_session_id: Parent session ID for handoff chains
            agent_depth: Depth in agent hierarchy (0 = root session)
            sandbox_enabled: Whether the session runtime was launched sandboxed
            native_terminal_id: Operator-only native pane selector. The daemon verifies its
                foreground CLI and shell generation; clients cannot supply process identity.

        Returns:
            Session details including session_id, session_ref (<project>#N), and status
        """
        from gobby.utils.machine_id import get_machine_id
        from gobby.utils.project_context import get_project_context
        from gobby.utils.session_context import get_current_session_id, get_request_principal

        if session_manager is None:
            return {"error": "Session manager not available"}

        # Auto-resolve machine_id
        resolved_machine_id = machine_id
        if not resolved_machine_id:
            resolved_machine_id = get_machine_id()

        # Auto-resolve project_id
        resolved_project_id = project_id
        if not resolved_project_id:
            project_ctx = get_project_context()
            resolved_project_id = project_ctx.get("id") if project_ctx else None
        if not resolved_project_id:
            return {
                "error": "Could not determine project_id — run 'gobby init' or pass it explicitly"
            }

        ambient_session_ref = get_current_session_id()
        ambient_session = None
        if ambient_session_ref:
            try:
                ambient_session_id = await asyncio.to_thread(
                    session_manager.resolve_session_reference,
                    ambient_session_ref,
                    resolved_project_id,
                )
            except ValueError:
                ambient_session_id = None
            ambient_session = (
                await asyncio.to_thread(session_manager.get, ambient_session_id)
                if ambient_session_id is not None
                else None
            )
            if ambient_session is None:
                return {
                    "error": "Current MCP session could not be resolved.",
                    "error_code": "ambient_session_not_found",
                }
            if ambient_session.source == source:
                identity_matches = (
                    ambient_session.external_id == external_id
                    and ambient_session.project_id == resolved_project_id
                )
                if not identity_matches:
                    return {
                        "error": ("Registration identity does not match the current MCP session."),
                        "error_code": "identity_mismatch",
                        "session_id": ambient_session.id,
                        "session_ref": ambient_session.ref,
                        "canonical_external_id": ambient_session.external_id,
                        "observed_external_id": external_id,
                    }
        if native_terminal_id is not None:
            try:
                principal = await get_request_principal()
            except LookupError:
                return {
                    "error": "Terminal selection requires operator authentication.",
                    "error_code": "operator_auth_required",
                }
            if principal is not None:
                return {
                    "error": "Terminal selection requires operator authentication.",
                    "error_code": "operator_auth_required",
                }
            if (
                ambient_session is None
                or ambient_session.source != source
                or ambient_session.external_id != external_id
                or ambient_session.project_id != resolved_project_id
                or ambient_session.machine_id != resolved_machine_id
            ):
                return {
                    "error": "Terminal repair must target the current MCP session.",
                    "error_code": "identity_mismatch",
                }
        try:
            session = await asyncio.to_thread(
                session_manager.register,
                external_id=external_id,
                machine_id=resolved_machine_id,
                source=source,
                project_id=resolved_project_id,
                title=title,
                title_source=manual_title_source(title),
                git_branch=git_branch,
                parent_session_id=parent_session_id if parent_session_id is not None else UNSET,
                agent_depth=agent_depth,
                sandbox_enabled=sandbox_enabled,
            )

            result: dict[str, Any] = {
                "session_id": session.id,
                "session_ref": session.ref,
                "external_id": session.external_id,
                "status": session.status,
                "source": session.source,
                "project_id": session.project_id,
            }
            if source == "codex":
                result["terminal_binding"] = (
                    asdict(
                        await asyncio.to_thread(
                            repair_native_terminal_binding,
                            session_manager,
                            session,
                            selected_terminal_id=native_terminal_id,
                        )
                    )
                    if ambient_session is not None and ambient_session.id == session.id
                    else {"status": "unbound", "reason": "Repair requires the current MCP session."}
                )
            return result
        except MachineNotRegisteredError as e:
            return {"error": str(e), "error_code": "machine_not_registered"}
        except Exception as e:
            return {"error": f"Failed to register session: {e}"}
