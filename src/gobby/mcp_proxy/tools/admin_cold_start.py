"""gobby-agents cold_start_admin: the /gobby admin and /gobby assistant pane cold start.

The counterpart's role is the duplicate guard. A live counterpart session, or a live
pane with the counterpart role in the caller's tab, is reused and never opened twice.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import Any, Literal

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.apply_agent_definition import apply_agent_definition_impl
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.workspaces.registry import current_actor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import LIVE_SESSION_STATUSES, SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspaces import WorkspaceManager, WorkspaceTarget
from gobby.terminals.session_pane import session_pane
from gobby.terminals.workspace_contract import WorkspaceOpError
from gobby.utils.session_context import get_session_context

# Each operator pane's counterpart role and the router command that cold starts it.
_COUNTERPARTS = {"orchestrator": ("assistant", "assistant"), "assistant": ("orchestrator", "admin")}
# The argv that opens each provider's interactive UI on an initial prompt. A bare AGY
# prompt is not accepted; -i keeps it interactive.
_PANE_LAUNCH = {
    "agy": ("agy", "-i"),
    "claude": ("claude",),
    "codex": ("codex",),
    "droid": ("droid",),
    "grok": ("grok",),
}


@dataclass(frozen=True)
class _Caller:
    session: Session
    ref: str
    pane: WorkspaceTarget | None


def _refusal(code: str, error: str) -> dict[str, Any]:
    return {"success": False, "error_code": code, "error": error}


def _caller(db: HubDatabase, session_id: str) -> _Caller | None:
    session = SessionManager(db).get(session_id)
    if session is None:
        return None
    return _Caller(session, session.ref, session_pane(WorkspaceManager(db), session))


def _live_holder(db: HubDatabase, session: Session, role: str) -> tuple[str, str | None] | None:
    """The newest other live ``role`` session here with a live terminal: (ref, pane id)."""
    rows = db.fetchall(
        """
SELECT s.id FROM sessions s JOIN session_variables v ON v.session_id = s.id
WHERE s.project_id = %s AND s.machine_id = %s AND s.status = ANY(%s) AND s.id <> %s
AND v.variables ->> '_agent_type' = %s
ORDER BY s.created_at DESC
""",
        (session.project_id, session.machine_id, list(LIVE_SESSION_STATUSES), session.id, role),
    )
    sessions, terminals = SessionManager(db), TerminalManager(db)
    for row in rows:
        holder = sessions.get(str(row["id"]))
        terminal = terminals.resolve_live_for_session(holder) if holder else None
        if holder is not None and terminal is not None:
            pane = WorkspaceManager(db).get_pane_for_terminal(terminal.id)
            return holder.ref, pane.id if pane else None
    return None


def _session_ref(db: HubDatabase, terminal_id: str | None) -> str | None:
    terminal = TerminalManager(db).get(terminal_id) if terminal_id else None
    session = (
        SessionManager(db).get(terminal.session_id) if terminal and terminal.session_id else None
    )
    return session.ref if session else None


async def cold_start_admin_impl(ctx: AgentsRegistryContext, role: str) -> dict[str, Any]:
    """Apply ``role`` to the calling pane, then reuse or open its counterpart pane."""
    if role not in _COUNTERPARTS:
        return _refusal("invalid_role", f"Role must be orchestrator or assistant, not {role!r}")
    ops = ctx.workspace_ops_resolver() if ctx.workspace_ops_resolver is not None else None
    db = ctx.db
    context = get_session_context()
    if db is None or ops is None:
        return _refusal("workspaces_unavailable", "Workspace panes are not configured")
    caller = await asyncio.to_thread(_caller, db, context.session_id) if context else None
    if caller is None:
        return _refusal("session_unavailable", "No session context")
    applied = await apply_agent_definition_impl(
        agent=role, db=db, session_id=caller.session.id, task_manager=ctx.task_manager
    )
    if not applied.get("success"):
        return applied
    counterpart, command = _COUNTERPARTS[role]
    report: dict[str, Any] = {
        "success": True,
        "role": role,
        "definition": applied["status"],
        "session": caller.ref,
    }
    holder = await asyncio.to_thread(_live_holder, db, caller.session, counterpart)
    if holder is not None:
        pane_id = holder[1]
        return {
            **report,
            "created": False,
            "counterpart": {"role": counterpart, "pane": pane_id, "session": holder[0]},
        }
    if caller.pane is None or caller.pane.pane is None or caller.pane.tab is None:
        return _refusal("pane_unavailable", "Run the cold start from a workspace pane")
    source = caller.session.source
    if source not in _PANE_LAUNCH:
        return _refusal("unsupported_provider", f"No pane command for provider {source!r}")
    try:
        actor = await current_actor()
        snapshot = await ops.workspace_snapshot(actor, caller.pane.workspace.id)
        existing = next(
            (
                pane
                for pane in snapshot.panes
                if pane.tab_id == caller.pane.tab.id and pane.role == counterpart
            ),
            None,
        )
        if existing is not None:
            session = await asyncio.to_thread(_session_ref, db, existing.terminal_id)
            return {
                **report,
                "created": False,
                "counterpart": {"role": counterpart, "pane": existing.id, "session": session},
            }
        added = (
            await ops.pane_split(actor, caller.pane.pane.id, "horizontal", role=counterpart)
        ).panes[0]
        prefix = "$gobby" if source == "codex" else "/gobby"
        line = shlex.join([*_PANE_LAUNCH[source], f"{prefix} {command}"])
        write = await ops.pane_send_text(actor, added.id, line, submit=True)
    except WorkspaceOpError as exc:
        return _refusal(exc.code, str(exc))
    launch = {"command": line, "indeterminate": write.indeterminate, "detail": write.detail}
    return {
        **report,
        "created": True,
        "counterpart": {"role": counterpart, "pane": added.id, "session": None},
        "launch": launch,
    }


def register_admin_cold_start_tools(
    registry: InternalToolRegistry, ctx: AgentsRegistryContext
) -> None:
    @registry.tool(
        name="cold_start_admin",
        description=(
            "Cold start this operator pane as the Orchestrator or Assistant: apply that "
            "definition to the calling session, then reuse the live counterpart (a live "
            "session with the other role, or a pane with that role in this tab) or split "
            "this pane once and launch the same provider CLI with the counterpart's "
            "/gobby command. Re-runnable; returns both seat refs and created."
        ),
    )
    async def cold_start_admin(role: Literal["orchestrator", "assistant"]) -> dict[str, Any]:
        return await cold_start_admin_impl(ctx, role)
