"""Read-only build observability MCP tools for gobby-tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.build.observability import (
    explain_dispatch,
    get_build_status,
    list_build_history,
)
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext


def create_build_observability_registry(ctx: RegistryContext) -> InternalToolRegistry:
    registry = InternalToolRegistry(
        name="gobby-tasks-build-observability",
        description="Read-only build observability tools",
    )

    def get_build_status_tool(
        input_ref: str,
        history_limit: int = 5,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return get_build_status(
            input_ref,
            db=ctx.task_manager.db,
            project_id=_project_id(ctx, project_id),
            history_limit=history_limit,
        )

    def explain_dispatch_tool(
        task_id: str,
        max_active_agents: int | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return explain_dispatch(
            task_id,
            db=ctx.task_manager.db,
            project_id=_project_id(ctx, project_id),
            max_active_agents=max_active_agents,
        )

    def list_build_history_tool(
        input_ref: str,
        limit: int = 20,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return list_build_history(
            input_ref,
            db=ctx.task_manager.db,
            project_id=_project_id(ctx, project_id),
            limit=limit,
        )

    registry.register(
        name="get_build_status",
        description="Return compact build state for a task tree or build input.",
        input_schema={
            "type": "object",
            "properties": {
                "input_ref": {"type": "string"},
                "history_limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5},
                "project_id": {"type": "string"},
            },
            "required": ["input_ref"],
        },
        func=get_build_status_tool,
    )
    registry.register(
        name="explain_dispatch",
        description="Explain dispatcher eligibility and proposed action without mutation.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "max_active_agents": {"type": "integer", "minimum": 1},
                "project_id": {"type": "string"},
            },
            "required": ["task_id"],
        },
        func=explain_dispatch_tool,
    )
    registry.register(
        name="list_build_history",
        description="List recent build run and event history for a task tree or build input.",
        input_schema={
            "type": "object",
            "properties": {
                "input_ref": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "project_id": {"type": "string"},
            },
            "required": ["input_ref"],
        },
        func=list_build_history_tool,
    )

    return registry


def _project_id(ctx: RegistryContext, explicit: str | None) -> str:
    resolved = explicit or ctx.get_current_project_id()
    if resolved is None:
        raise ValueError("Could not determine project_id for build observability")
    return resolved


__all__ = ["create_build_observability_registry"]
