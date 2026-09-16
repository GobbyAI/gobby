"""Bounded durable coordination waits, exposed with agent coordination tools."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.headless_waits import headless_wait_refusal
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.coordination_waits import CoordinationWaitManager, coordination_wait_payload


def register_coordination_tools(registry: InternalToolRegistry, ctx: AgentsRegistryContext) -> None:
    @registry.tool(
        name="wait_for_coordination",
        description=(
            "Register a durable wait on another owner session, then yield the turn. Provide "
            "exactly one condition: a unique coordination_key released by that owner's durable "
            "coordination_release message to you with metadata.coordination_key, or a nonempty "
            "set of canonical session statuses. Returns wait_id and waiting/terminal outcome. "
            "Default timeout 900 seconds, maximum 3600. Repeating the same condition returns "
            "the original wait without extending expiry. Completion uses protected wake delivery."
        ),
    )
    async def wait_for_coordination(
        owner_session: str,
        coordination_key: str | None = None,
        statuses: list[str] | None = None,
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
            owner = ctx.resolve_session_id(owner_session)
            row = await asyncio.to_thread(
                CoordinationWaitManager(ctx.db).register,
                waiter,
                owner,
                coordination_key=coordination_key,
                statuses=statuses,
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
