"""Agent terminalization and cleanup helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.agents_runtime import facade

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner


def _fire_synthetic_stop(
    hook_manager_resolver: Any | None,
    session_id: str,
    session_manager: Any | None = None,
) -> None:
    """Fire a synthetic STOP event so stop-triggered rules evaluate for killed agents."""
    if not hook_manager_resolver:
        return

    agents = facade()
    try:
        hook_mgr = hook_manager_resolver()
        if hook_mgr is None:
            return

        from gobby.hooks.events import HookEvent, HookEventType, SessionSource

        source = SessionSource.CLAUDE
        if session_manager is not None:
            try:
                session = session_manager.get(session_id)
                session_source = getattr(session, "source", None) if session else None
                if isinstance(session_source, str) and session_source:
                    source = SessionSource(session_source)
            except (AttributeError, ValueError) as exc:
                agents.logger.debug(
                    "Failed to resolve source for synthetic stop session %s: %s",
                    session_id,
                    exc,
                )

        stop_event = HookEvent(
            event_type=HookEventType.STOP,
            session_id=session_id,
            source=source,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": session_id},
        )
        hook_mgr.evaluate_workflow_rules(stop_event)
        agents.logger.debug(f"Fired synthetic stop rules for killed agent session {session_id}")
    except Exception as e:
        agents.logger.warning(f"Failed to fire synthetic stop rules for session {session_id}: {e}")


async def _cleanup_terminal_artifacts(
    *,
    run_id: str | None,
    db: Any | None,
    tmux_session_name: str | None,
    agent_session_id: str | None,
    debug: bool,
    session_manager: Any | None,
    hook_manager_resolver: Any | None,
    result: dict[str, Any],
) -> None:
    """Clean up terminal/session state after an explicit agent termination."""
    agents = facade()
    if not debug:
        cleanup = agents.cleanup_agent_runtime_state(
            db,
            run_id=run_id,
            child_session_id=agent_session_id,
        )
        result["dispatch_mutex_released"] = cleanup.dispatch_mutex_rows
        result["workflow_instances_deleted"] = cleanup.workflow_instance_rows
        if cleanup.errors:
            result["runtime_cleanup_errors"] = list(cleanup.errors)

    if not debug and tmux_session_name:
        try:
            from gobby.agents.tmux import get_tmux_session_manager

            await get_tmux_session_manager().kill_session(tmux_session_name, missing_ok=True)
            result["tmux_session_killed"] = True
        except Exception as e:
            agents.logger.debug(f"tmux session cleanup failed for {tmux_session_name}: {e}")

    if not debug and agent_session_id:
        if session_manager is not None:
            try:
                session_manager.update_status(agent_session_id, "expired")
                result["session_expired"] = True
            except Exception as e:
                result["session_expire_error"] = str(e)

        agents._fire_synthetic_stop(
            hook_manager_resolver,
            agent_session_id,
            session_manager=session_manager,
        )


async def _complete_self_terminated_run(
    *,
    runner: AgentRunner,
    run: Any,
    kill_db: Any,
    completion_registry: Any | None,
    session_manager: Any | None,
    hook_manager_resolver: Any | None,
    signal: str = "TERM",
    debug: bool = False,
) -> dict[str, Any]:
    """Terminate the caller's process and finalize the run as success."""
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

    completed = await agents.complete_and_notify_agent_run(
        runner,
        run.id,
        completion_registry=completion_registry,
        notify_result=notify_result,
        message=f"Agent {run.id} completed",
    )
    if not completed:
        current = runner.get_run(run.id)
        agents.logger.debug(
            "Self-success terminalization no-op for run %s; current status=%s",
            run.id,
            current.status if current else "missing",
        )
        result["status"] = current.status if current else "unknown"
        result["noop"] = True
    else:
        result["status"] = "success"

    kill_result = await agents._kill_agent_process(
        run,
        kill_db,
        signal_name=signal,
        close_terminal=not debug,
    )
    if kill_result.get("success") or kill_result.get("error") == "No target PID found":
        result.update(kill_result)
    else:
        result["terminal_cleanup_error"] = kill_result.get("error") or "unknown terminal cleanup"

    await agents._cleanup_terminal_artifacts(
        run_id=run.id,
        db=kill_db,
        tmux_session_name=run.tmux_session_name,
        agent_session_id=agent_session_id,
        debug=debug,
        session_manager=session_manager,
        hook_manager_resolver=hook_manager_resolver,
        result=result,
    )
    result["run_id"] = run.id
    result["success"] = True
    return result
