"""Operator hold on a parked seat's task claims.

A root terminal operator parking a seat on purpose (a directed CLI update, say)
places the hold so the claim sweep does not read the seat's exit as a death.
Nothing places it implicitly; see ``gobby.sessions.operator_claim_hold``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from gobby.sessions.operator_claim_hold import operator_claim_hold_horizon
from gobby.storage.sessions._operator_claim_hold import (
    clear_operator_claim_hold,
    record_operator_claim_hold,
)

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager


def _authorize(
    session_manager: SessionManager, session_ref: str
) -> tuple[Session, Session] | dict[str, Any]:
    """Return (caller, target), or the refusal for a non-root caller or foreign target."""
    from gobby.utils.session_context import get_current_session_id

    caller_ref = get_current_session_id()
    caller = None
    if caller_ref:
        try:
            caller = session_manager.get(session_manager.resolve_session_reference(caller_ref))
        except ValueError:
            caller = None
    if (
        caller is None
        or caller.session_type != "terminal"
        or caller.agent_run_id
        or caller.agent_depth
    ):
        return {
            "success": False,
            "error": "Claim holds require a root interactive terminal caller.",
            "error_code": "claim_hold_caller_forbidden",
        }
    try:
        target = session_manager.get(
            session_manager.resolve_session_reference(session_ref, caller.project_id)
        )
    except ValueError:
        target = None
    if target is None:
        return {
            "success": False,
            "error": f"Session {session_ref} not found",
            "error_code": "claim_hold_target_not_found",
        }
    if target.machine_id != caller.machine_id:
        return {
            "success": False,
            "error": "Claim holds are limited to sessions on the caller's machine.",
            "error_code": "claim_hold_target_forbidden",
            "target_session_id": target.id,
        }
    return caller, target


def register_claim_hold_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
    db: HubDatabase,
) -> None:
    """Register the hold and release tools on the sessions registry."""

    @registry.tool(
        name="hold_session_claims",
        description=(
            "Root terminal operators only: keep a parked seat's task claims through its "
            "absence (e.g. a directed CLI update) until release, proven resume, or the "
            "revival horizon. Returns the fixed expiry."
        ),
    )
    async def hold_session_claims(session_id: str, reason: str) -> dict[str, Any]:
        """Place or renew the hold on `session_id` (#N, project#N, or UUID)."""
        if not reason.strip():
            return {
                "success": False,
                "error": "A hold needs a reason.",
                "error_code": "claim_hold_reason_required",
            }
        authorized = _authorize(session_manager, session_id)
        if isinstance(authorized, dict):
            return authorized
        caller, target = authorized
        placed_at = await asyncio.to_thread(
            record_operator_claim_hold,
            db,
            target.id,
            actor_session_id=caller.id,
            reason=reason.strip(),
        )
        return {
            "success": True,
            "session_id": target.id,
            "actor_session_id": caller.id,
            "held_at": placed_at.isoformat(),
            "expires_at": (placed_at + operator_claim_hold_horizon()).isoformat(),
        }

    @registry.tool(
        name="release_session_claims_hold",
        description="Root terminal operators only: release a seat's claim hold.",
    )
    async def release_session_claims_hold(session_id: str) -> dict[str, Any]:
        """Clear the hold on `session_id`; its claims return to the ordinary schedule."""
        authorized = _authorize(session_manager, session_id)
        if isinstance(authorized, dict):
            return authorized
        _caller, target = authorized
        released = await asyncio.to_thread(clear_operator_claim_hold, db, target.id)
        return {"success": True, "session_id": target.id, "released": released}


__all__ = ["register_claim_hold_tools"]
