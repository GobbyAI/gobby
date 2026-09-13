"""Agent lifecycle MCP tool registration."""

from __future__ import annotations

from typing import Any, Literal, cast

from gobby.mcp_proxy.tools.agent_cancellation import (
    stop_agent_run,
    terminate_agent_run,
)
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_runtime import facade
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.sessions.handoff_records import build_handoff_payload, stage_agent_end_handoff


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

        try:
            resolved_parent = ctx.resolve_session_id(parent_session_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
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
                    kill_agent_process=agents._kill_agent_process,
                    cleanup_terminal_artifacts=agents._cleanup_terminal_artifacts,
                )
                if result.get("success"):
                    cancelled.append(run.id)
                else:
                    errors.append({"run_id": run.id, "error": str(result.get("error", "unknown"))})
            except Exception as e:  # Cancellation continues across independently owned runs.
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
            "Persist a structured handoff, signal that this agent run is complete, and "
            "release its resources. Always self-scoped to the caller. "
            "Load gobby:references/sessions/handoffs.md. All sections share 10,000 JSON-escaped "
            "characters including rendered formatting; aim below 5,000 for ordinary handoffs. "
            "Include current continuation state and fresh epoch friction observations, including "
            "resolved friction. Never copy earlier reflections; record only actual recurrence. "
            "For necessary live detail after pruning, create or update permitted Markdown working "
            "notes and include their project-relative path in references. Never move cumulative "
            "history into notes. Use readable sentences; leave unneeded optional fields empty."
        ),
    )
    async def end_agent_run(
        current_state: str,
        next_steps: list[str],
        what_was_accomplished: list[str] | None = None,
        key_decisions: list[str] | None = None,
        problems_encountered: list[str] | None = None,
        what_didnt_work: list[str] | None = None,
        blockers: list[str] | None = None,
        notes: list[str] | None = None,
        references: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            payload = build_handoff_payload(
                current_state=current_state,
                next_steps=next_steps,
                what_was_accomplished=what_was_accomplished or (),
                key_decisions=key_decisions or (),
                problems_encountered=problems_encountered or (),
                what_didnt_work=what_didnt_work or (),
                blockers=blockers or (),
                notes=notes or (),
                references=references or (),
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc), "error_code": "invalid_handoff"}

        trusted_run_id = ctx.get_current_agent_run_id()
        current_session_id = ctx.get_current_session_id()
        if not current_session_id and not trusted_run_id:
            return {"success": False, "error": "No active session context available"}

        run_id = trusted_run_id
        if run_id is None:
            assert current_session_id is not None
            db_agent = ctx.agent_run_manager.get_by_session(current_session_id)
            run_id = (
                db_agent.id if db_agent else ctx.runner.get_run_id_by_session(current_session_id)
            )
        if not run_id:
            return {"success": False, "error": f"No agent found for session {current_session_id}"}

        db_run = ctx.runner.get_run(run_id)
        if not db_run:
            return {"success": False, "error": f"Agent run {run_id} not found"}
        if (
            current_session_id
            and db_run.child_session_id
            and db_run.child_session_id != current_session_id
        ):
            return {
                "success": False,
                "error": "Trusted agent run does not match the active session context",
            }
        if not db_run.child_session_id or not db_run.parent_session_id:
            return {
                "success": False,
                "error": "Agent run is missing its child or parent session boundary",
                "error_code": "invalid_agent_boundary",
            }

        kill_db = ctx.db or ctx.agent_run_manager.db
        try:
            handoff = stage_agent_end_handoff(
                kill_db,
                agent_run_id=run_id,
                child_session_id=db_run.child_session_id,
                parent_session_id=db_run.parent_session_id,
                payload=payload,
            )
        except Exception as exc:
            facade().logger.exception("Failed to persist agent-end handoff for run %s", run_id)
            return {
                "success": False,
                "run_id": run_id,
                "error": str(exc),
                "error_code": "handoff_persistence_failed",
            }
        result = cast(
            dict[str, Any],
            await facade()._complete_self_terminated_run(
                runner=ctx.runner,
                run=db_run,
                kill_db=kill_db,
                completion_registry=ctx.completion_registry,
                session_manager=ctx.session_manager,
            ),
        )
        if not result.get("success"):
            return result
        result_status = result.get("status")
        if result_status != "success":
            return {
                "success": False,
                "run_id": run_id,
                "status": result_status or "unknown",
                "error": "Self-termination did not complete with success status",
            }
        return {
            "success": True,
            "run_id": run_id,
            "status": result_status,
            "handoff_id": handoff.id,
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

        allowed_statuses = {"success", "cancelled", "error"}
        if status is not None and status not in allowed_statuses:
            return {
                "success": False,
                "error": f"Invalid status '{status}'. Allowed: {', '.join(sorted(allowed_statuses))}",
            }

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
        resolved_run_id = run_id

        db_run = ctx.runner.get_run(resolved_run_id)
        if not db_run:
            return {"success": False, "error": f"Agent run {resolved_run_id} not found"}

        agent_session_id = db_run.child_session_id or resolved_session_id

        is_self_termination = False
        if agent_session_id:
            caller_session_id = ctx.get_current_session_id()
            if caller_session_id and caller_session_id == agent_session_id:
                is_self_termination = True
        if status == "success" and not is_self_termination:
            return {
                "success": False,
                "error": "status='success' is only allowed for self-termination",
            }
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
                    signal=signal,
                    debug=debug,
                ),
            )

        termination_status: Literal["cancelled", "error"] = (
            "error" if effective_status == "error" else "cancelled"
        )
        return await terminate_agent_run(
            run=db_run,
            runner=ctx.runner,
            agent_run_manager=ctx.agent_run_manager,
            db=kill_db,
            lifecycle_monitor=ctx.lifecycle_monitor,
            completion_registry=ctx.completion_registry,
            task_manager=ctx.task_manager,
            session_manager=ctx.session_manager,
            agent_session_id=agent_session_id,
            effective_status=termination_status,
            signal=signal,
            debug=debug,
            stop=stop,
            kill_agent_process=agents._kill_agent_process,
            cleanup_terminal_artifacts=agents._cleanup_terminal_artifacts,
        )
