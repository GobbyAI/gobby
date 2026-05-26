"""Best-effort dispatcher ticks after MCP task handoff transitions."""

from __future__ import annotations

from gobby.mcp_proxy.tools.tasks._context import RegistryContext


def schedule_dispatcher_tick(
    ctx: RegistryContext,
    *,
    project_id: str | None,
    reason: str,
) -> None:
    """Schedule an immediate dispatcher tick when a tool creates dispatchable work."""
    from gobby.build.dispatch_tick import schedule_dispatcher_tick_for_project

    schedule_dispatcher_tick_for_project(
        ctx.task_manager.db,
        project_id=project_id,
        reason=reason,
    )
