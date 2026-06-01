"""
Internal MCP tools for Gobby Agent System.

Exposes functionality for:
- Spawning agents (via spawn_agent unified tool)
- Getting agent results (retrieve completed run output)
- Listing agents (view runs for a session)
- Cancelling agents (stop running agents)

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.kill import kill_agent as _kill_agent_process
from gobby.agents.run_completion import complete_and_notify_agent_run
from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.mcp_proxy.tools.agent_cancellation import (
    stop_agent_run,
    terminalize_killed_agent_run,
)
from gobby.mcp_proxy.tools.agent_live_activity import (
    overlay_live_activity,
    overlay_runs_live_activity,
)
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import AgentRunStatus, LocalAgentRunManager

if TYPE_CHECKING:
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner

logger = logging.getLogger(__name__)

_TERMINAL_AGENT_STATUSES = {"success", "error", "timeout", "cancelled"}
_WAIT_FOR_AGENT_MAX_TIMEOUT_SECONDS = 1800.0


def _agent_result_payload(run: Any, *, include_prompt: bool = True) -> dict[str, Any]:
    payload = {
        "run_id": run.id,
        "status": run.status,
        "result": run.result,
        "error": run.error,
        "provider": run.provider,
        "model": run.model,
        "tool_calls_count": run.tool_calls_count,
        "turns_used": run.turns_used,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "child_session_id": run.child_session_id,
        "terminal_reason": run.terminal_reason,
    }
    if include_prompt:
        payload["prompt"] = run.prompt
    return payload


def _fire_synthetic_stop(
    hook_manager_resolver: Any | None,
    session_id: str,
) -> None:
    """Fire a synthetic STOP event so stop-triggered rules evaluate for killed agents.

    When kill_agent sends SIGTERM, the CLI never fires its stop hook.
    This ensures rules like digest-on-response still run.
    """
    if not hook_manager_resolver:
        return

    try:
        hook_mgr = hook_manager_resolver()
        if hook_mgr is None:
            return

        from datetime import datetime

        from gobby.hooks.events import HookEvent, HookEventType, SessionSource

        stop_event = HookEvent(
            event_type=HookEventType.STOP,
            session_id=session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            metadata={"_platform_session_id": session_id},
        )
        # Evaluate workflow rules only (skip full handle() which does
        # daemon health checks, adapter routing, session resolution, etc.)
        hook_mgr._evaluate_workflow_rules(stop_event)
        logger.debug(f"Fired synthetic stop rules for killed agent session {session_id}")
    except Exception as e:
        logger.warning(f"Failed to fire synthetic stop rules for session {session_id}: {e}")


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
    if not debug:
        cleanup = cleanup_agent_runtime_state(
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
            import subprocess

            from gobby.config.tmux import TmuxConfig

            tmux_cfg = TmuxConfig()
            kill_cmd = [tmux_cfg.command]
            if tmux_cfg.socket_name:
                kill_cmd.extend(["-L", tmux_cfg.socket_name])
            kill_cmd.extend(["kill-session", "-t", tmux_session_name])
            subprocess.run(kill_cmd, capture_output=True, timeout=5)
            result["tmux_session_killed"] = True
        except Exception as e:
            logger.debug(f"tmux session cleanup failed for {tmux_session_name}: {e}")

    if not debug and agent_session_id:
        if session_manager is not None:
            try:
                session_manager.update_status(agent_session_id, "expired")
                result["session_expired"] = True
            except Exception as e:
                result["session_expire_error"] = str(e)

        _fire_synthetic_stop(hook_manager_resolver, agent_session_id)


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
            logger.debug("Failed to read adversary_verdict for %s: %s", agent_session_id, e)

    completed = await complete_and_notify_agent_run(
        runner,
        run.id,
        completion_registry=completion_registry,
        notify_result=notify_result,
        message=f"Agent {run.id} completed",
    )
    if not completed:
        current = runner.get_run(run.id)
        logger.debug(
            "Self-success terminalization no-op for run %s; current status=%s",
            run.id,
            current.status if current else "missing",
        )
        result["status"] = current.status if current else "unknown"
        result["noop"] = True
    else:
        result["status"] = "success"

    kill_result = await _kill_agent_process(
        run,
        kill_db,
        signal_name=signal,
        close_terminal=not debug,
    )
    if kill_result.get("success") or kill_result.get("error") == "No target PID found":
        result.update(kill_result)
    else:
        result["terminal_cleanup_error"] = kill_result.get("error") or "unknown terminal cleanup"

    await _cleanup_terminal_artifacts(
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


def create_agents_registry(
    runner: AgentRunner,
    session_manager: Any | None = None,
    # spawn_agent dependencies
    task_manager: Any | None = None,
    worktree_storage: Any | None = None,
    git_manager: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    # For mode=self (workflow activation on caller session)
    db: Any | None = None,
    # For firing synthetic stop events on agent kill
    hook_manager_resolver: Any | None = None,
    completion_registry: Any | None = None,
    lifecycle_monitor: AgentLifecycleMonitor | None = None,
    # Legacy parameter — ignored, kept for caller compatibility during migration
    running_registry: Any | None = None,
    daemon_config: Any | None = None,
    code_index: Any | None = None,
    transcript_reader: Any | None = None,
) -> InternalToolRegistry:
    """
    Create an agent tool registry with all agent-related tools.

    Args:
        runner: AgentRunner instance for executing agents.
        session_manager: Optional SessionManager for resolving session references.
        task_manager: Task manager for spawn_agent task resolution.
        worktree_storage: Worktree storage for spawn_agent isolation.
        git_manager: Git manager for spawn_agent isolation.
        clone_storage: Clone storage for spawn_agent isolation.
        clone_manager: Clone git manager for spawn_agent isolation.
        db: Database instance for agent definition lookups.
        completion_registry: CompletionEventRegistry for auto-subscribing parent sessions.

    Returns:
        InternalToolRegistry with all agent tools registered.
    """
    from gobby.utils.project_context import get_project_context
    from gobby.utils.session_context import get_current_session_id, resolve_session_ref

    agent_run_manager = LocalAgentRunManager(db) if db else runner.run_storage

    def _resolve_session_id(ref: str) -> str:
        return resolve_session_ref(session_manager, ref)

    registry = InternalToolRegistry(
        name="gobby-agents",
        description="Agent spawning - start, monitor, and manage subagents",
    )

    @registry.tool(
        name="get_agent_result",
        description="Get the result of a completed agent run.",
    )
    async def get_agent_result(run_id: str) -> dict[str, Any]:
        """
        Get the result of an agent run.

        Args:
            run_id: The agent run ID.

        Returns:
            Dict with run details including status, result, error.
        """
        run = runner.get_run(run_id)
        if not run:
            return {"success": False, "error": f"Agent run {run_id} not found"}
        run = await overlay_live_activity(run, transcript_reader)
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
        """
        Wait for an agent run to finish without forcing callers to spin in the terminal.

        Args:
            run_id: Agent run ID to wait for.
            timeout_seconds: Maximum time to wait, capped at 30 minutes.
            poll_interval_seconds: Delay between status checks, capped to a sane range.

        Returns:
            Dict with completed=true and the terminal run payload, or completed=false
            with the latest run payload when the timeout expires.
        """
        requested_timeout = max(
            0.0, min(float(timeout_seconds), _WAIT_FOR_AGENT_MAX_TIMEOUT_SECONDS)
        )
        timeout = requested_timeout
        interval = max(0.1, min(float(poll_interval_seconds), 30.0))
        deadline = time.monotonic() + timeout

        while True:
            run = runner.get_run(run_id)
            if not run:
                return {"success": False, "error": f"Agent run {run_id} not found"}

            payload = _agent_result_payload(
                await overlay_live_activity(run, transcript_reader), include_prompt=False
            )
            if run.status in _TERMINAL_AGENT_STATUSES:
                return {"success": True, "completed": True, **payload}

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "success": True,
                    "completed": False,
                    "timeout_seconds": timeout,
                    "requested_timeout_seconds": requested_timeout,
                    **payload,
                }

            await asyncio.sleep(min(interval, remaining))

    @registry.tool(
        name="list_agent_runs",
        description="List agent runs for a session. Defaults to current session. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def list_agent_runs(
        parent_session_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        List agent runs for a session.

        Args:
            parent_session_id: Optional session reference (#N, N, UUID, or prefix).
                               Falls back to SessionContext if not provided.
            status: Optional status filter (pending, running, success, error, timeout, cancelled).
            limit: Maximum results (default: 20).

        Returns:
            Dict with list of agent runs.
        """

        # Resolve session_id from context if not provided
        effective_parent_ref = parent_session_id or get_current_session_id()
        if not effective_parent_ref:
            return {
                "success": False,
                "error": "No parent_session_id provided and no context available",
            }

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_parent_id = _resolve_session_id(effective_parent_ref)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        runs = runner.list_runs(resolved_parent_id, status=status, limit=limit)

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
        name="stop_agent",
        description="Stop a running agent and mark the run cancelled.",
    )
    async def stop_agent(run_id: str) -> dict[str, Any]:
        """
        Stop a running agent and mark the run as cancelled.

        Args:
            run_id: The agent run ID to stop.

        Returns:
            Dict with success status.
        """
        return await stop_agent_run(
            run_id=run_id,
            runner=runner,
            agent_run_manager=agent_run_manager,
            db=db,
            lifecycle_monitor=lifecycle_monitor,
            completion_registry=completion_registry,
            task_manager=task_manager,
            session_manager=session_manager,
            hook_manager_resolver=hook_manager_resolver,
            kill_agent_process=_kill_agent_process,
            cleanup_terminal_artifacts=_cleanup_terminal_artifacts,
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
        """Cancel active helper runs for a parent session, continuing after per-run errors."""
        if not parent_session_id:
            return {"success": False, "error": "parent_session_id is required"}
        if not agent_name:
            return {"success": False, "error": "agent_name is required"}

        resolved_parent = _resolve_session_id(parent_session_id)
        stale = [
            run
            for run in agent_run_manager.list_by_parent(resolved_parent)
            if run.agent_name == agent_name and run.status in ("pending", "running")
        ]

        cancelled: list[str] = []
        errors: list[dict[str, str]] = []
        for run in stale:
            try:
                result = await stop_agent_run(
                    run_id=run.id,
                    runner=runner,
                    agent_run_manager=agent_run_manager,
                    db=db,
                    lifecycle_monitor=lifecycle_monitor,
                    completion_registry=completion_registry,
                    task_manager=task_manager,
                    session_manager=session_manager,
                    hook_manager_resolver=hook_manager_resolver,
                    kill_agent_process=_kill_agent_process,
                    cleanup_terminal_artifacts=_cleanup_terminal_artifacts,
                )
                if result.get("success"):
                    cancelled.append(run.id)
                else:
                    errors.append({"run_id": run.id, "error": str(result.get("error", "unknown"))})
            except Exception as e:  # noqa: BLE001 - best-effort cancellation
                errors.append({"run_id": run.id, "error": str(e)})
                logger.warning("cancel_stale_helpers: failed to stop %s: %s", run.id, e)

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
        """Complete the caller's agent run without requiring explicit identifiers."""

        current_session_id = get_current_session_id()
        if not current_session_id:
            return {"success": False, "error": "No active session context available"}

        db_agent = agent_run_manager.get_by_session(current_session_id)
        run_id = db_agent.id if db_agent else runner.get_run_id_by_session(current_session_id)
        if not run_id:
            return {"success": False, "error": f"No agent found for session {current_session_id}"}

        db_run = runner.get_run(run_id)
        if not db_run:
            return {"success": False, "error": f"Agent run {run_id} not found"}

        result = await _complete_self_terminated_run(
            runner=runner,
            run=db_run,
            kill_db=db or agent_run_manager.db,
            completion_registry=completion_registry,
            session_manager=session_manager,
            hook_manager_resolver=hook_manager_resolver,
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
        """
        Kill a running agent process.

        This terminates the process, closes the terminal, and cleans up workflow state.
        Can be called by parent (using run_id) or by the agent itself (using session_id).

        Args:
            run_id: Agent run ID (for parent killing child)
            session_id: Optional session reference (#N, N, UUID, or prefix) for self-termination.
                       Falls back to SessionContext if not provided and run_id is None.
            signal: Signal to send (TERM, KILL, INT, HUP, QUIT). Default: TERM
            force: Use SIGKILL immediately (equivalent to signal="KILL")
            stop: Also terminalize workflow state. Defaults true for direct MCP compatibility.
            debug: If True, kill agent process but preserve workflow state and leave
                terminal open for inspection. Default: False (full cleanup).
            status: Completion status for the agent run. Self-termination defaults
                to "success", parent-initiated kill defaults to "cancelled".
                Agents can pass "error" to indicate failure.

        Returns:
            Dict with success status and kill details.
        """

        if force:
            signal = "KILL"

        # Validate signal against allowlist to prevent injection
        signal = signal.upper()
        allowed_signals = {"TERM", "KILL", "INT", "HUP", "QUIT"}
        if signal not in allowed_signals:
            return {
                "success": False,
                "error": f"Invalid signal '{signal}'. Allowed: {', '.join(sorted(allowed_signals))}",
            }

        # Resolve run_id from session_id if needed (self-termination case)
        effective_session_ref = session_id
        if run_id is None and not effective_session_ref:
            effective_session_ref = get_current_session_id()

        resolved_session_id: str | None = None
        if run_id is None and effective_session_ref:
            # Resolve session_id (accepts #N, N, UUID, prefix)
            try:
                resolved_session_id = _resolve_session_id(effective_session_ref)
            except ValueError as e:
                return {"success": False, "error": str(e)}

            # Query DB for agent run with this child_session_id
            db_agent = agent_run_manager.get_by_session(resolved_session_id)
            if db_agent:
                run_id = db_agent.id
            else:
                run_id = runner.get_run_id_by_session(resolved_session_id)

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

        # Get agent info from DB
        db_run = runner.get_run(run_id)
        if not db_run:
            return {"success": False, "error": f"Agent run {run_id} not found"}

        agent_session_id = db_run.child_session_id or resolved_session_id
        tmux_session_name = db_run.tmux_session_name

        is_self_termination = resolved_session_id is not None
        if not is_self_termination and agent_session_id:
            caller_session_id = get_current_session_id()
            if caller_session_id and caller_session_id == agent_session_id:
                is_self_termination = True
        effective_status = status or ("success" if is_self_termination else "cancelled")

        kill_db = db or agent_run_manager.db
        if effective_status == "success":
            return await _complete_self_terminated_run(
                runner=runner,
                run=db_run,
                kill_db=kill_db,
                completion_registry=completion_registry,
                session_manager=session_manager,
                hook_manager_resolver=hook_manager_resolver,
                signal=signal,
                debug=debug,
            )

        result = await _kill_agent_process(
            db_run,
            kill_db,
            signal_name=signal,
            close_terminal=not debug,
        )
        if not result.get("success"):
            return result

        if not stop:
            result["workflow_stopped"] = False
            return result

        result.update(
            await terminalize_killed_agent_run(
                runner=runner,
                run_id=run_id,
                effective_status=effective_status,
                lifecycle_monitor=lifecycle_monitor,
                completion_registry=completion_registry,
                task_manager=task_manager,
            )
        )

        await _cleanup_terminal_artifacts(
            run_id=run_id,
            db=kill_db,
            tmux_session_name=tmux_session_name,
            agent_session_id=agent_session_id,
            debug=debug,
            session_manager=session_manager,
            hook_manager_resolver=hook_manager_resolver,
            result=result,
        )

        return result

    @registry.tool(
        name="can_spawn_agent",
        description="Check if an agent can be spawned. Defaults to checking for the current session. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def can_spawn_agent(parent_session_id: str | None = None) -> dict[str, Any]:
        """
        Check if an agent can be spawned from the given session.

        This checks the agent depth limit to prevent infinite nesting.

        Args:
            parent_session_id: Optional session reference (#N, N, UUID, or prefix).
                               Falls back to SessionContext if not provided.

        Returns:
            Dict with can_spawn boolean and reason.
        """

        # Resolve session_id from context if not provided
        effective_parent_ref = parent_session_id or get_current_session_id()
        if not effective_parent_ref:
            return {
                "success": False,
                "can_spawn": False,
                "reason": "No parent_session_id provided and no context available",
            }

        # Resolve session_id to UUID (accepts #N, N, UUID, or prefix)
        try:
            resolved_parent_id = _resolve_session_id(effective_parent_ref)
        except ValueError as e:
            return {"success": False, "can_spawn": False, "reason": str(e)}

        can_spawn, reason, _parent_depth = runner.can_spawn(resolved_parent_id)
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
        """List active agent runs across the build or under one parent session."""

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
            effective_parent_ref = parent_session_id or get_current_session_id()
            if not effective_parent_ref:
                return {
                    "success": False,
                    "error": "No parent_session_id or session context available",
                }
            try:
                resolved_parent_id = _resolve_session_id(effective_parent_ref)
            except ValueError as e:
                return {"success": False, "error": str(e)}
            parent_status = None if status_key == "active" else cast(AgentRunStatus, status_key)
            runs = agent_run_manager.list_by_parent(
                resolved_parent_id,
                limit=limit,
                status=parent_status,
            )
        elif status_key == "active":
            runs = agent_run_manager.list_active(limit=limit)
        elif status_key == "running":
            runs = agent_run_manager.list_running(limit=limit)
        else:
            runs = agent_run_manager.list_by_status(status="pending", limit=limit)
        runs = await overlay_runs_live_activity(runs, transcript_reader)

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
        """
        Get the state for a running agent.

        Args:
            run_id: The agent run ID.

        Returns:
            Dict with running agent details.
        """
        run = agent_run_manager.get(run_id)
        if not run or run.status not in ("running", "pending"):
            return {"success": False, "error": f"No running agent found with ID {run_id}"}
        run = await overlay_live_activity(run, transcript_reader)

        return {"success": True, "agent": run.to_dict()}

    @registry.tool(
        name="unregister_agent",
        description="Mark an agent run as cancelled (internal use).",
    )
    async def unregister_agent(run_id: str) -> dict[str, Any]:
        """
        Mark an agent run as cancelled.

        This is typically called automatically when a session ends,
        but can be called manually for cleanup.

        Args:
            run_id: The agent run ID to unregister.

        Returns:
            Dict with success status.
        """
        run = agent_run_manager.get(run_id)
        if run and run.status in ("running", "pending"):
            agent_run_manager.fail(run_id, error="Unregistered")
            return {"success": True, "message": f"Unregistered agent {run_id}"}
        elif run:
            return {"success": True, "message": f"Agent {run_id} already in status {run.status}"}
        else:
            return {"success": False, "error": f"No agent found with ID {run_id}"}

    @registry.tool(
        name="running_agent_stats",
        description="Get statistics about running agents.",
    )
    async def running_agent_stats() -> dict[str, Any]:
        """
        Get statistics about running agents.

        Returns:
            Dict with counts by mode and parent.
        """
        all_runs = agent_run_manager.list_active()
        by_parent: dict[str, int] = {}

        for run in all_runs:
            by_parent[run.parent_session_id] = by_parent.get(run.parent_session_id, 0) + 1

        return {
            "success": True,
            "total": len(all_runs),
            "by_parent_count": len(by_parent),
        }

    @registry.tool(
        name="evaluate_spawn",
        description="Dry-run evaluation of spawn_agent. Defaults parent_session_id to current session.",
    )
    async def evaluate_spawn_tool(
        agent: str = "default",
        workflow: str | None = None,
        task_id: str | None = None,
        isolation: str | None = None,
        provider: str | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        parent_session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Dry-run evaluation of spawn_agent.

        Simulates the spawn process and reports what would happen,
        including any misconfigurations, without actually spawning.

        Args:
            agent: Agent name (default: "default").
            workflow: Optional workflow name override.
            task_id: Optional task ID for branch naming.
            isolation: Optional isolation mode (none, worktree, clone).
            provider: Optional provider override.
            branch_name: Optional explicit branch name.
            base_branch: Optional base branch for isolation.
            parent_session_id: Optional parent session reference (#N, N, UUID, or prefix).
                               Falls back to SessionContext if not provided.
            project_path: Optional project path.

        Returns:
            Dict with evaluation results including can_spawn, items, and workflow_evaluation.
        """
        from gobby.agents.dry_run import evaluate_spawn

        # Resolve parent_session_id from context if not provided
        effective_parent_ref = parent_session_id or get_current_session_id()

        # Resolve parent session if provided
        resolved_parent = None
        if effective_parent_ref:
            try:
                resolved_parent = _resolve_session_id(effective_parent_ref)
            except ValueError:
                resolved_parent = effective_parent_ref

        # Get project path from context if not provided
        if not project_path:
            project_ctx = get_project_context()
            if project_ctx:
                project_path = project_ctx.get("project_path")

        # Get MCP manager from runner if available
        mcp_mgr = getattr(runner, "_mcp_manager", None)

        eval_result = await evaluate_spawn(
            agent=agent,
            workflow=workflow,
            task_id=task_id,
            isolation=isolation,
            provider=provider,
            branch_name=branch_name,
            base_branch=base_branch,
            parent_session_id=resolved_parent,
            project_path=project_path,
            db=db,
            runner=runner,
            session_manager=session_manager,
            git_manager=git_manager,
            worktree_storage=worktree_storage,
            clone_storage=clone_storage,
            clone_manager=clone_manager,
            task_manager=task_manager,
            mcp_manager=mcp_mgr,
        )
        return eval_result.to_dict()

    # Register spawn_agent tool from spawn_agent module
    from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

    spawn_registry = create_spawn_agent_registry(
        runner=runner,
        task_manager=task_manager,
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        clone_storage=clone_storage,
        clone_manager=clone_manager,
        session_manager=session_manager,
        db=db,
        completion_registry=completion_registry,
        daemon_config=daemon_config,
        code_index=code_index,
    )

    # Merge spawn_agent tools into agents registry
    for tool_name, tool in spawn_registry._tools.items():
        registry._tools[tool_name] = tool

    # --- apply_persona tool ---

    @registry.tool(
        name="apply_persona",
        description=(
            "Apply a persona-capable agent definition to the current session. "
            "Updates prompt-facing persona state and skill selection without "
            "spawning a child agent or changing provider/model/isolation."
        ),
    )
    async def apply_persona(
        agent: str,
        variables: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply an agent definition's persona to the current session.

        Args:
            agent: Agent definition name to apply (e.g. "developer", "qa-reviewer").
            variables: Additional variables to merge after persona changes.
            task_id: Optional task reference to bind to the session.

        Returns:
            Dict with success status and activation details.
        """
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        return await apply_persona_impl(
            agent=agent,
            db=db,
            variables=variables,
            task_id=task_id,
            task_manager=task_manager,
        )

    return registry
