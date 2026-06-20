"""Agent lifecycle MCP tool registration."""

from __future__ import annotations

from typing import Any, cast

from gobby.mcp_proxy.tools.agent_cancellation import (
    stop_agent_run,
    terminalize_killed_agent_run,
)
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_runtime import facade
from gobby.mcp_proxy.tools.internal import InternalToolRegistry


def register_agent_lifecycle_tools(
    registry: InternalToolRegistry,
    ctx: AgentsRegistryContext,
) -> None:
    @registry.tool(
        name="stop_agent",
        description="Stop a running agent and mark the run cancelled.",
    )
    async def stop_agent(run_id: str) -> dict[str, Any]:
        agents = facade()
        return await stop_agent_run(
            run_id=run_id,
            runner=ctx.runner,
            agent_run_manager=ctx.agent_run_manager,
            db=ctx.db,
            lifecycle_monitor=ctx.lifecycle_monitor,
            completion_registry=ctx.completion_registry,
            task_manager=ctx.task_manager,
            session_manager=ctx.session_manager,
            hook_manager_resolver=ctx.hook_manager_resolver,
            kill_agent_process=agents._kill_agent_process,
            cleanup_terminal_artifacts=agents._cleanup_terminal_artifacts,
        )

    @registry.tool(
        name="cancel_stale_helpers",
        description=(
            "Cancel all still-running runs of an agent spawned by a parent session. "
            "Used by freshness rules before parent turn delivery."
        ),
    )
    async def cancel_stale_helpers(
        parent_session_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        if not parent_session_id:
            return {"success": False, "error": "parent_session_id is required"}
        if not agent_name:
            return {"success": False, "error": "agent_name is required"}

        resolved_parent = ctx.resolve_session_id(parent_session_id)
        stale = [
            run
            for run in ctx.agent_run_manager.list_by_parent(resolved_parent)
            if run.agent_name == agent_name and run.status in ("pending", "running")
        ]

        cancelled: list[str] = []
        errors: list[dict[str, str]] = []
        agents = facade()
        for run in stale:
            try:
                result = await stop_agent_run(
                    run_id=run.id,
                    runner=ctx.runner,
                    agent_run_manager=ctx.agent_run_manager,
                    db=ctx.db,
                    lifecycle_monitor=ctx.lifecycle_monitor,
                    completion_registry=ctx.completion_registry,
                    task_manager=ctx.task_manager,
                    session_manager=ctx.session_manager,
                    hook_manager_resolver=ctx.hook_manager_resolver,
                    kill_agent_process=agents._kill_agent_process,
                    cleanup_terminal_artifacts=agents._cleanup_terminal_artifacts,
                )
                if result.get("success"):
                    cancelled.append(run.id)
                else:
                    errors.append({"run_id": run.id, "error": str(result.get("error", "unknown"))})
            except Exception as e:  # noqa: BLE001 - best-effort cancellation
                errors.append({"run_id": run.id, "error": str(e)})
                agents.logger.warning("cancel_stale_helpers: failed to stop %s: %s", run.id, e)

        return {
            "success": True,
            "cancelled": cancelled,
            "errors": errors,
            "count": len(cancelled),
        }

    @registry.tool(
        name="end_agent_run",
        description=(
            "Signal that this agent run is complete and release its resources. "
            "Always self-scoped to the caller."
        ),
    )
    async def end_agent_run() -> dict[str, Any]:
        current_session_id = ctx.get_current_session_id()
        if not current_session_id:
            return {"success": False, "error": "No active session context available"}

        db_agent = ctx.agent_run_manager.get_by_session(current_session_id)
        run_id = db_agent.id if db_agent else ctx.runner.get_run_id_by_session(current_session_id)
        if not run_id:
            return {"success": False, "error": f"No agent found for session {current_session_id}"}

        db_run = ctx.runner.get_run(run_id)
        if not db_run:
            return {"success": False, "error": f"Agent run {run_id} not found"}

        result = cast(
            dict[str, Any],
            await facade()._complete_self_terminated_run(
                runner=ctx.runner,
                run=db_run,
                kill_db=ctx.db or ctx.agent_run_manager.db,
                completion_registry=ctx.completion_registry,
                session_manager=ctx.session_manager,
                hook_manager_resolver=ctx.hook_manager_resolver,
            ),
        )
        if not result.get("success"):
            return result
        return {
            "success": True,
            "run_id": run_id,
            "status": result.get("status", "success"),
        }

    @registry.tool(
        name="kill_agent",
        description=(
            "Kill a running agent process and close its terminal. "
            "Use run_id (parent kills child) or session_id (self-termination). "
            "Defaults to self-termination if no run_id or session_id provided."
        ),
    )
    async def kill_agent(
        run_id: str | None = None,
        session_id: str | None = None,
        signal: str = "TERM",
        force: bool = False,
        stop: bool = True,
        debug: bool = False,
        status: str | None = None,
    ) -> dict[str, Any]:
        if force:
            signal = "KILL"

        signal = signal.upper()
        allowed_signals = {"TERM", "KILL", "INT", "HUP", "QUIT"}
        if signal not in allowed_signals:
            return {
                "success": False,
                "error": f"Invalid signal '{signal}'. Allowed: {', '.join(sorted(allowed_signals))}",
            }

        effective_session_ref = session_id
        if run_id is None and not effective_session_ref:
            effective_session_ref = ctx.get_current_session_id()

        resolved_session_id: str | None = None
        if run_id is None and effective_session_ref:
            try:
                resolved_session_id = ctx.resolve_session_id(effective_session_ref)
            except ValueError as e:
                return {"success": False, "error": str(e)}

            db_agent = ctx.agent_run_manager.get_by_session(resolved_session_id)
            if db_agent:
                run_id = db_agent.id
            else:
                run_id = ctx.runner.get_run_id_by_session(resolved_session_id)

            if not run_id:
                return {
                    "success": False,
                    "error": f"No agent found for session {effective_session_ref}",
                }

        if run_id is None:
            return {
                "success": False,
                "error": "Either run_id or session_id required (or active context)",
            }

        db_run = ctx.runner.get_run(run_id)
        if not db_run:
            return {"success": False, "error": f"Agent run {run_id} not found"}

        agent_session_id = db_run.child_session_id or resolved_session_id
        tmux_session_name = db_run.tmux_session_name

        is_self_termination = resolved_session_id is not None
        if not is_self_termination and agent_session_id:
            caller_session_id = ctx.get_current_session_id()
            if caller_session_id and caller_session_id == agent_session_id:
                is_self_termination = True
        effective_status = status or ("success" if is_self_termination else "cancelled")

        agents = facade()
        kill_db = ctx.db or ctx.agent_run_manager.db
        if effective_status == "success":
            return cast(
                dict[str, Any],
                await agents._complete_self_terminated_run(
                    runner=ctx.runner,
                    run=db_run,
                    kill_db=kill_db,
                    completion_registry=ctx.completion_registry,
                    session_manager=ctx.session_manager,
                    hook_manager_resolver=ctx.hook_manager_resolver,
                    signal=signal,
                    debug=debug,
                ),
            )

        result = cast(
            dict[str, Any],
            await agents._kill_agent_process(
                db_run,
                kill_db,
                signal_name=signal,
                close_terminal=not debug,
            ),
        )
        if not result.get("success"):
            return result

        if not stop:
            result["workflow_stopped"] = False
            return result

        result.update(
            await terminalize_killed_agent_run(
                runner=ctx.runner,
                run_id=run_id,
                effective_status=effective_status,
                lifecycle_monitor=ctx.lifecycle_monitor,
                completion_registry=ctx.completion_registry,
                task_manager=ctx.task_manager,
            )
        )

        await agents._cleanup_terminal_artifacts(
            run_id=run_id,
            db=kill_db,
            tmux_session_name=tmux_session_name,
            agent_session_id=agent_session_id,
            debug=debug,
            session_manager=ctx.session_manager,
            hook_manager_resolver=ctx.hook_manager_resolver,
            result=result,
        )

        return result
