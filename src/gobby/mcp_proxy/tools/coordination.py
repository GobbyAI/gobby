"""Bounded durable coordination waits, exposed with agent coordination tools."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.headless_waits import headless_wait_refusal
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.coordination_waits import CoordinationWaitManager, coordination_wait_payload


def _resolve_owner(
    ctx: AgentsRegistryContext,
    waiter: str,
    owner_session: str | None,
    *,
    reply: bool,
) -> str:
    """Resolve the owner to wait on, defaulting a reply wait to the spawning parent."""
    if owner_session is not None:
        return ctx.resolve_session_id(owner_session)
    if not reply:
        raise ValueError("owner_session is required")
    session = ctx.session_manager.get(waiter) if ctx.session_manager is not None else None
    if session is None or session.parent_session_id is None:
        raise ValueError("This session has no parent to reply; name owner_session instead")
    return session.parent_session_id


def register_coordination_tools(registry: InternalToolRegistry, ctx: AgentsRegistryContext) -> None:
    @registry.tool(
        name="wait_for_coordination",
        description=(
            "Register a durable wait on another owner session, then yield the turn. Provide "
            "exactly one condition: a unique coordination_key released by that owner's durable "
            "coordination_release message to you with metadata.coordination_key, a nonempty "
            "set of canonical session statuses, or reply=true for the next ordinary message "
            "that owner sends you after this call. Omit owner_session with reply=true to wait "
            "on the session that spawned you. Returns wait_id and waiting/terminal outcome. "
            "Default timeout 900 seconds, maximum 3600. Repeating the same condition returns "
            "the original wait without extending expiry. Completion uses protected wake delivery."
        ),
    )
    async def wait_for_coordination(
        owner_session: str | None = None,
        coordination_key: str | None = None,
        statuses: list[str] | None = None,
        reply: bool = False,
        timeout: float = 900,
    ) -> dict[str, Any]:
        waiter = ctx.get_current_session_id()
        if waiter is None or ctx.db is None or ctx.completion_registry is None:
            return {"success": False, "error": "Coordination waits require active session services"}
        refusal = headless_wait_refusal(
            agent_run_manager=ctx.agent_run_manager,
            session_id=waiter,
            tool_name="wait_for_coordination",
        )
        if refusal is not None:
            return refusal
        try:
            owner = _resolve_owner(ctx, waiter, owner_session, reply=reply)
            row = await asyncio.to_thread(
                CoordinationWaitManager(ctx.db).register,
                waiter,
                owner,
                coordination_key=coordination_key,
                statuses=statuses,
                reply=reply,
                timeout=timeout,
            )
            return coordination_wait_payload(row)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    @registry.tool(
        name="cancel_coordination_wait",
        description="Cancel your own durable coordination wait by wait_id; terminal outcomes persist.",
    )
    async def cancel_coordination_wait(wait_id: str) -> dict[str, Any]:
        waiter = ctx.get_current_session_id()
        if waiter is None or ctx.db is None:
            return {"success": False, "error": "Cancellation requires an active session"}
        try:
            row = await asyncio.to_thread(CoordinationWaitManager(ctx.db).cancel, wait_id, waiter)
            return coordination_wait_payload(row)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
