"""Agent query and status MCP tool registration."""

from __future__ import annotations

from typing import Any, cast

from gobby.mcp_proxy.tools.agent_live_activity import (
    overlay_live_activity,
    overlay_runs_live_activity,
)
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_payloads import _agent_result_payload
from gobby.mcp_proxy.tools.agents_runtime import facade
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import AgentRunStatus


def register_agent_query_tools(
    registry: InternalToolRegistry,
    ctx: AgentsRegistryContext,
) -> None:
    @registry.tool(
        name="get_agent_result",
        description="Get the result of a completed agent run.",
    )
    async def get_agent_result(run_id: str) -> dict[str, Any]:
        run = ctx.runner.get_run(run_id)
        if not run:
            return {"success": False, "error": f"Agent run {run_id} not found"}
        run = await overlay_live_activity(run, ctx.transcript_reader)
        return {"success": True, **_agent_result_payload(run)}

    @registry.tool(
        name="wait_for_agent",
        description=(
            "Block until an agent run reaches a terminal status or the timeout expires. "
            "Use this instead of shell sleeps, tmux polling, or provider Monitor waits."
        ),
    )
    async def wait_for_agent(
        run_id: str,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 2.0,
    ) -> dict[str, Any]:
        agents = facade()
        requested_timeout = max(
            0.0, min(float(timeout_seconds), agents._WAIT_FOR_AGENT_MAX_TIMEOUT_SECONDS)
        )
        timeout = requested_timeout
        interval = max(0.1, min(float(poll_interval_seconds), 30.0))
        deadline = agents.time.monotonic() + timeout

        while True:
            run = ctx.runner.get_run(run_id)
            if not run:
                return {"success": False, "error": f"Agent run {run_id} not found"}

            payload = _agent_result_payload(
                await overlay_live_activity(run, ctx.transcript_reader), include_prompt=False
            )
            if run.status in agents._TERMINAL_AGENT_STATUSES:
                return {"success": True, "completed": True, **payload}

            remaining = deadline - agents.time.monotonic()
            if remaining <= 0:
                return {
                    "success": True,
                    "completed": False,
                    "timeout_seconds": timeout,
                    "requested_timeout_seconds": requested_timeout,
                    **payload,
                }

            await agents.asyncio.sleep(min(interval, remaining))

    @registry.tool(
        name="list_agent_runs",
        description=(
            "List agent runs for a session. Defaults to current session. "
            "Accepts #N, N, UUID, or prefix for session_id."
        ),
    )
    async def list_agent_runs(
        parent_session_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        effective_parent_ref = parent_session_id or ctx.get_current_session_id()
        if not effective_parent_ref:
            return {
                "success": False,
                "error": "No parent_session_id provided and no context available",
            }

        try:
            resolved_parent_id = ctx.resolve_session_id(effective_parent_ref)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        runs = ctx.runner.list_runs(resolved_parent_id, status=status, limit=limit)

        return {
            "success": True,
            "runs": [
                {
                    "id": run.id,
                    "status": run.status,
                    "provider": run.provider,
                    "model": run.model,
                    "workflow_name": run.workflow_name,
                    "prompt": run.prompt[:100] + "..." if len(run.prompt) > 100 else run.prompt,
                    "started_at": run.started_at,
                    "completed_at": run.completed_at,
                }
                for run in runs
            ],
            "count": len(runs),
        }

    @registry.tool(
        name="can_spawn_agent",
        description=(
            "Check if an agent can be spawned. Defaults to checking for the current session. "
            "Accepts #N, N, UUID, or prefix for session_id."
        ),
    )
    async def can_spawn_agent(parent_session_id: str | None = None) -> dict[str, Any]:
        effective_parent_ref = parent_session_id or ctx.get_current_session_id()
        if not effective_parent_ref:
            return {
                "success": False,
                "can_spawn": False,
                "reason": "No parent_session_id provided and no context available",
            }

        try:
            resolved_parent_id = ctx.resolve_session_id(effective_parent_ref)
        except ValueError as e:
            return {"success": False, "can_spawn": False, "reason": str(e)}

        can_spawn, reason, _parent_depth = ctx.runner.can_spawn(resolved_parent_id)
        return {
            "success": True,
            "can_spawn": can_spawn,
            "reason": reason,
        }

    @registry.tool(
        name="list_running_agents",
        description=(
            "List active agent runs. Defaults to build-wide scope. Pass "
            "scope='parent' or parent_session_id to filter by parent session; "
            "pass status='running' to match `gobby agents runs list --status running`."
        ),
    )
    async def list_running_agents(
        parent_session_id: str | None = None,
        scope: str = "all",
        status: str = "active",
        limit: int = 100,
    ) -> dict[str, Any]:
        scope_key = scope.strip().lower().replace("_", "-")
        if scope_key in {"build", "build-wide"}:
            scope_key = "all"
        if parent_session_id is not None or scope_key == "current":
            scope_key = "parent"
        if scope_key not in {"all", "parent"}:
            return {
                "success": False,
                "error": "scope must be one of: all, build, build-wide, parent, current",
            }

        status_key = status.strip().lower()
        if status_key not in {"active", "pending", "running"}:
            return {"success": False, "error": "status must be one of: active, pending, running"}

        resolved_parent_id = None
        if scope_key == "parent":
            effective_parent_ref = parent_session_id or ctx.get_current_session_id()
            if not effective_parent_ref:
                return {
                    "success": False,
                    "error": "No parent_session_id or session context available",
                }
            try:
                resolved_parent_id = ctx.resolve_session_id(effective_parent_ref)
            except ValueError as e:
                return {"success": False, "error": str(e)}
            parent_status = None if status_key == "active" else cast(AgentRunStatus, status_key)
            runs = ctx.agent_run_manager.list_by_parent(
                resolved_parent_id,
                limit=limit,
                status=parent_status,
            )
        elif status_key == "active":
            runs = ctx.agent_run_manager.list_active(limit=limit)
        elif status_key == "running":
            runs = ctx.agent_run_manager.list_running(limit=limit)
        else:
            runs = ctx.agent_run_manager.list_by_status(status="pending", limit=limit)
        runs = await overlay_runs_live_activity(runs, ctx.transcript_reader)

        return {
            "success": True,
            "agents": [run.to_brief() for run in runs],
            "count": len(runs),
            "scope": scope_key,
            "status": status_key,
            "parent_session_id": resolved_parent_id,
        }

    @registry.tool(
        name="get_running_agent",
        description="Get process state for a running agent.",
    )
    async def get_running_agent(run_id: str) -> dict[str, Any]:
        run = ctx.agent_run_manager.get(run_id)
        if not run or run.status not in ("running", "pending"):
            return {"success": False, "error": f"No running agent found with ID {run_id}"}
        run = await overlay_live_activity(run, ctx.transcript_reader)

        return {"success": True, "agent": run.to_dict()}

    @registry.tool(
        name="unregister_agent",
        description="Mark an agent run as cancelled (internal use).",
    )
    async def unregister_agent(run_id: str) -> dict[str, Any]:
        run = ctx.agent_run_manager.get(run_id)
        if run and run.status in ("running", "pending"):
            ctx.agent_run_manager.fail(run_id, error="Unregistered")
            return {"success": True, "message": f"Unregistered agent {run_id}"}
        if run:
            return {"success": True, "message": f"Agent {run_id} already in status {run.status}"}
        return {"success": False, "error": f"No agent found with ID {run_id}"}

    @registry.tool(
        name="running_agent_stats",
        description="Get statistics about running agents.",
    )
    async def running_agent_stats() -> dict[str, Any]:
        all_runs = ctx.agent_run_manager.list_active()
        by_parent: dict[str, int] = {}

        for run in all_runs:
            by_parent[run.parent_session_id] = by_parent.get(run.parent_session_id, 0) + 1

        return {
            "success": True,
            "total": len(all_runs),
            "by_parent_count": len(by_parent),
        }
