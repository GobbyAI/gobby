"""Best-effort dispatcher ticks after MCP task handoff transitions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.tasks._context import RegistryContext

logger = logging.getLogger(__name__)


def schedule_dispatcher_tick(
    ctx: RegistryContext,
    *,
    project_id: str | None,
    reason: str,
) -> None:
    """Schedule an immediate dispatcher tick when a tool creates dispatchable work."""
    if not project_id:
        return
    from gobby.app_context import get_app_context

    services = get_app_context()
    if services is None or getattr(services, "agent_runner", None) is None:
        return
    if getattr(services, "task_manager", None) is not ctx.task_manager:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    loop.create_task(
        _run_dispatcher_tick(ctx, project_id=project_id, reason=reason, services=services),
        name=f"gobby-dispatcher-tick-{reason}",
    )


async def _run_dispatcher_tick(
    ctx: RegistryContext,
    *,
    project_id: str,
    reason: str,
    services: Any,
) -> None:
    try:
        from gobby.build.dispatch_tick import kick_dispatcher_tick

        await kick_dispatcher_tick(
            db=ctx.task_manager.db,
            project_id=project_id,
            services=services,
        )
    except Exception:
        logger.warning(
            "dispatcher_tick_after_task_handoff_failed",
            extra={"project_id": project_id, "reason": reason},
            exc_info=True,
        )
