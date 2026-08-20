"""Agent terminalization and cleanup helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.agents.kill import KILL_ERROR_NO_TARGET_PID
from gobby.mcp_proxy.tools.agents_runtime import facade

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner


async def _cleanup_terminal_artifacts(
    *,
    run_id: str | None,
    db: Any | None,
    terminal_id: str | None,
    agent_session_id: str | None,
    debug: bool,
    session_manager: Any | None,
    result: dict[str, Any],
) -> None:
    """Clean up terminal/session state after an explicit agent termination."""
    agents = facade()
    if not debug:
        cleanup = agents.cleanup_agent_runtime_state(
            db,
            run_id=run_id,
            child_session_id=agent_session_id,
            terminal_reason=result.get("terminal_reason"),
        )
        result["dispatch_mutex_released"] = cleanup.dispatch_mutex_rows
        result["agent_step_instances_deleted"] = cleanup.workflow_instance_rows
        if cleanup.errors:
            result["runtime_cleanup_errors"] = list(cleanup.errors)

    if not debug and agent_session_id:
        if session_manager is not None:
            try:
                session_manager.update_status(agent_session_id, "expired")
                result["session_expired"] = True
            except Exception as e:
                result["session_expire_error"] = str(e)


async def _complete_self_terminated_run(
    *,
    runner: AgentRunner,
    run: Any,
    kill_db: Any,
    completion_registry: Any | None,
    session_manager: Any | None,
    signal: str = "TERM",
    debug: bool = False,
) -> dict[str, Any]:
    """Terminate the caller after acknowledged completion delivery and cleanup."""
    agents = facade()
    agent_session_id = run.child_session_id
    result: dict[str, Any] = {}

    notify_result: dict[str, Any] = {"status": "success", "run_id": run.id}
    if agent_session_id:
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            session_vars = SessionVariableManager(kill_db).get_variables(agent_session_id)
            verdict = session_vars.get("adversary_verdict")
            if isinstance(verdict, str) and verdict:
                notify_result["signoff_message"] = verdict
        except Exception as e:
            agents.logger.debug("Failed to read adversary_verdict for %s: %s", agent_session_id, e)

    async def complete_with_acknowledged_delivery() -> bool:
        return bool(
            await agents.complete_and_notify_agent_run(
                runner,
                run.id,
                completion_registry=completion_registry,
                notify_result=notify_result,
                message=f"Agent {run.id} completed",
            )
        )

    if run.terminal_id and not debug:
        from gobby.agents.capture import terminate_managed_runtime_async
        from gobby.agents.tmux.session_manager import TmuxSessionManager
        from gobby.storage.agents import LocalAgentRunManager, TerminalAction
        from gobby.storage.terminals import TerminalManager
        from gobby.terminals import TerminalRuntimeRegistry
        from gobby.terminals.tmux_runtime import TmuxTerminalRuntime

        manager = LocalAgentRunManager(kill_db)
        terminal_manager = TerminalManager(kill_db)
        terminal = terminal_manager.get(run.terminal_id)
        runtime = TmuxTerminalRuntime(TmuxSessionManager())
        if terminal is not None and terminal.backend != "tmux":
            registry = TerminalRuntimeRegistry()
            registry.register(runtime)
            runtime = registry.resolve(terminal.backend)

        async def terminalize(
            _action: TerminalAction,
            _reason: str | None,
        ) -> Any | None:
            await complete_with_acknowledged_delivery()
            return runner.get_run(run.id)

        if terminal is None:
            termination_ok = False
            termination_error = "agent run has no terminal"
            termination_code = "kill_failed"
        else:
            termination = await terminate_managed_runtime_async(
                storage=manager,
                run=run,
                terminal=terminal,
                runtime=runtime,
                action="complete",
                terminalize=terminalize,
            )
            termination_ok = termination.success
            termination_error = termination.error
            termination_code = termination.error_code
        if not termination_ok:
            return {
                "success": False,
                "run_id": run.id,
                "error": termination_error,
                "error_code": termination_code,
            }
        result["status"] = "success"
        result["terminal_killed"] = True
    else:
        kill_result = await agents._kill_agent_process(
            run,
            kill_db,
            signal_name=signal,
            close_terminal=False,
        )
        if kill_result.get("success") or kill_result.get("error_code") == KILL_ERROR_NO_TARGET_PID:
            result.update(kill_result)
        else:
            result["terminal_cleanup_error"] = kill_result.get("error") or "unknown cleanup"

        completed = await complete_with_acknowledged_delivery()
        if not completed:
            current = runner.get_run(run.id)
            result["status"] = current.status if current else "unknown"
            result["noop"] = True
        else:
            result["status"] = "success"

    await agents._cleanup_terminal_artifacts(
        run_id=run.id,
        db=kill_db,
        terminal_id=run.terminal_id,
        agent_session_id=agent_session_id,
        debug=debug,
        session_manager=session_manager,
        result=result,
    )
    result["run_id"] = run.id
    result["success"] = True
    return result
