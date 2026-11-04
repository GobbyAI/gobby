"""Definition-configured interactive cold starts: apply a definition to the calling pane.

Other seats are spawned by their parent seat; a cold start opens no companion pane.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.apply_agent_definition import apply_agent_definition_impl
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import get_session_context
from gobby.workflows.agent_resolver import AgentResolutionError, resolve_agent_with_row


@dataclass(frozen=True)
class _Caller:
    session: Session
    ref: str


def _refusal(code: str, error: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": error}


def _caller(db: HubDatabase, session_id: str) -> _Caller | None:
    session = SessionManager(db).get(session_id)
    if session is None:
        return None
    return _Caller(session, session.ref)


async def cold_start_admin_impl(ctx: AgentsRegistryContext, role: str) -> dict[str, Any]:
    """Apply ``role`` to the calling session's pane."""
    db = ctx.db
    context = get_session_context()
    if db is None:
        return _refusal("session_unavailable", "No session store is configured")
    caller = await asyncio.to_thread(_caller, db, context.session_id) if context else None
    if caller is None:
        return _refusal("session_unavailable", "No session context")
    try:
        resolved = await asyncio.to_thread(
            resolve_agent_with_row, role, db, project_id=caller.session.project_id
        )
    except AgentResolutionError as exc:
        return _refusal("invalid_role", str(exc))
    if resolved is None:
        return _refusal("invalid_role", f"Agent definition {role!r} is unavailable")
    body, row = resolved
    if not row.enabled or not body.supports_cold_start():
        return _refusal("invalid_role", "The definition cannot cold start interactively")
    applied = await apply_agent_definition_impl(
        agent=role, db=db, session_id=caller.session.id, task_manager=ctx.task_manager
    )
    if not applied.get("success"):
        return applied
    return {
        "success": True,
        "role": role,
        "definition": applied["status"],
        "session": caller.ref,
    }


def register_admin_cold_start_tools(
    registry: InternalToolRegistry, ctx: AgentsRegistryContext
) -> None:
    @registry.tool(
        name="cold_start_admin",
        description=(
            "Cold start this pane using an enabled definition's declared cold-start commands. "
            "Applies the definition to the calling session. Re-runnable."
        ),
    )
    async def cold_start_admin(role: str) -> dict[str, Any]:
        return await cold_start_admin_impl(ctx, role)
