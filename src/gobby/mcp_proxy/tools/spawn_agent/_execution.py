"""Post-execute_spawn helpers extracted so spawn_agent modules stay under 1,000 lines."""

from __future__ import annotations

import logging
from typing import Any

from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import (
    cleanup_failed_spawn,
    start_run_or_cleanup,
)
from gobby.mcp_proxy.tools.spawn_agent._health import (
    _check_tmux_session_alive,
    schedule_tmux_health_check,
)
from gobby.mcp_proxy.tools.spawn_agent._runtime import (
    _build_spawn_success_response,
    _persist_spawn_runtime,
    _tmux_runtime_metadata,
)
from gobby.mcp_proxy.tools.spawn_agent._step_state import apply_claimed_step_update
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable

logger = logging.getLogger(__name__)


async def finalize_executed_spawn(
    *,
    runner: Any,
    run_id: str,
    spawn_result: Any,
    spawn_request: Any,
    isolation_ctx: Any,
    effective_isolation: str,
    base_commit_sha: Any,
    handler: Any,
    spawn_config: Any,
    completion_registry: Any,
    cleanup_isolation_on_failure: bool,
    task_manager: Any,
    task_spawn_lease: Any,
    parent_session_id: str,
    effective_provider: str,
    resolved_task_id: str | None,
    task_seq_num: Any,
    db: Any,
    agent_body: Any,
    effective_initial_variables: Any,
    reasoning: Any,
    speed_payload: Any,
) -> dict[str, Any]:
    """Persist runtime, verify liveness, start the run, auto-claim, and build the response."""
    tmux_session_name, tmux_socket_name, tmux_socket_path = _tmux_runtime_metadata(spawn_result)
    _persist_spawn_runtime(
        runner,
        run_id,
        spawn_result,
        tmux_session_name=tmux_session_name,
        worktree_id=isolation_ctx.worktree_id,
        clone_id=isolation_ctx.clone_id,
        terminal_id=getattr(spawn_result, "terminal_id", None),
    )
    if spawn_result.success:
        attach_error = task_spawn_lease.attach(run_id)
        if attach_error is not None:
            task_spawn_lease.release_unattached()
            error = f"task spawn mutex attach failed: {attach_error}"
            await cleanup_failed_spawn(
                runner,
                run_id,
                error,
                handler,
                spawn_config,
                completion_registry=completion_registry,
                cleanup_isolation=cleanup_isolation_on_failure,
                task_manager=task_manager,
                child_session_id=spawn_result.child_session_id,
                pid=spawn_result.pid,
                tmux_session_name=tmux_session_name,
                tmux_socket_name=tmux_socket_name,
                tmux_socket_path=tmux_socket_path,
            )
            return {
                "success": False,
                "error": error,
                "run_id": run_id,
                "speed": speed_payload,
            }

    tmux_spawn = bool(
        spawn_result.success and spawn_result.terminal_type == "tmux" and tmux_session_name
    )
    if tmux_spawn and tmux_session_name:
        alive, pane_output = await _check_tmux_session_alive(
            tmux_session_name,
            socket_name=tmux_socket_name,
            socket_path=tmux_socket_path,
        )
        if not alive:
            spawn_result.success = False
            spawn_result.status = "failed"
            spawn_result.error = f"tmux session '{tmux_session_name}' failed live-pane verification"
            if pane_output:
                spawn_result.error = f"{spawn_result.error}\nPane output:\n{pane_output}"
            await cleanup_failed_spawn(
                runner,
                run_id,
                spawn_result.error,
                handler,
                spawn_config,
                completion_registry=completion_registry,
                cleanup_isolation=cleanup_isolation_on_failure,
                task_manager=task_manager,
                child_session_id=spawn_result.child_session_id,
                pid=spawn_result.pid,
                tmux_session_name=tmux_session_name,
                tmux_socket_name=tmux_socket_name,
                tmux_socket_path=tmux_socket_path,
            )
            return {
                "success": False,
                "error": spawn_result.error,
                "run_id": run_id,
                "speed": speed_payload,
            }

    if spawn_result.success and spawn_result.child_session_id is not None:
        start_error = await start_run_or_cleanup(
            runner,
            run_id,
            handler,
            spawn_config,
            completion_registry=completion_registry,
            cleanup_isolation=cleanup_isolation_on_failure,
            task_manager=task_manager,
            child_session_id=spawn_result.child_session_id,
        )
        if start_error is not None:
            return start_error

        try:
            from gobby.runner_broadcasting import fire_agent_event

            fire_agent_event(
                "agent_started",
                run_id,
                {
                    "session_id": spawn_result.child_session_id,
                    "parent_session_id": parent_session_id,
                    "provider": effective_provider,
                    "pid": spawn_result.pid,
                    "tmux_session_name": tmux_session_name,
                    "tmux_socket_name": tmux_socket_name,
                    "tmux_socket_path": tmux_socket_path,
                    "terminal_id": getattr(spawn_result, "terminal_id", None),
                },
            )
        except Exception as e:
            logger.debug("Failed to fire agent_started event for %s: %s", run_id, e)

        if resolved_task_id and task_manager:
            try:
                task_obj = task_manager.get_task(resolved_task_id)
                if not task_obj or not is_task_actionable(task_obj):
                    logger.info(
                        "Skipping auto-claim for task %s; task is not actionable",
                        f"#{task_seq_num}" if task_seq_num else resolved_task_id,
                    )
                elif (
                    current_owner := get_claimed_session_id(task_obj)
                ) and current_owner != spawn_result.child_session_id:
                    logger.info(
                        "Skipping auto-claim for task %s; already assigned to %s",
                        f"#{task_seq_num}" if task_seq_num else resolved_task_id,
                        current_owner,
                    )
                else:
                    claimed_task = task_manager.claim_task(
                        resolved_task_id,
                        session_id=spawn_result.child_session_id,
                    )
                    task_owned_by_child = (
                        get_claimed_session_id(claimed_task) == spawn_result.child_session_id
                    )
                    logger.info(
                        "Auto-claimed task %s for agent %s (session %s)",
                        (f"#{task_seq_num}" if task_seq_num else resolved_task_id),
                        run_id,
                        spawn_result.child_session_id,
                    )
                    if (
                        task_owned_by_child
                        and db is not None
                        and agent_body is not None
                        and agent_body.step_workflow is not None
                    ):
                        apply_claimed_step_update(
                            db,
                            agent_body,
                            session_id=spawn_result.child_session_id,
                            initial_variables=effective_initial_variables,
                        )
            except Exception as e:
                error = f"Failed to auto-claim task {resolved_task_id}: {e}"
                logger.warning(error)
                await cleanup_failed_spawn(
                    runner,
                    run_id,
                    error,
                    handler,
                    spawn_config,
                    completion_registry=completion_registry,
                    cleanup_isolation=cleanup_isolation_on_failure,
                    task_manager=task_manager,
                    child_session_id=spawn_result.child_session_id,
                    pid=spawn_result.pid,
                    tmux_session_name=tmux_session_name,
                    tmux_socket_name=tmux_socket_name,
                    tmux_socket_path=tmux_socket_path,
                )
                return {
                    "success": False,
                    "error": error,
                    "run_id": run_id,
                    "speed": speed_payload,
                }

        if spawn_result.terminal_type == "tmux" and tmux_session_name:
            schedule_tmux_health_check(
                runner,
                run_id,
                tmux_session_name,
                tmux_socket_name,
                tmux_socket_path,
                completion_registry,
            )
    else:
        task_spawn_lease.release_unattached()
        await cleanup_failed_spawn(
            runner,
            run_id,
            spawn_result.error or "Spawn failed",
            handler,
            spawn_config,
            completion_registry=completion_registry,
            cleanup_isolation=cleanup_isolation_on_failure,
            task_manager=task_manager,
            child_session_id=spawn_result.child_session_id,
            pid=spawn_result.pid,
            tmux_session_name=tmux_session_name,
            tmux_socket_name=tmux_socket_name,
            tmux_socket_path=tmux_socket_path,
        )

    if not spawn_result.success:
        return {
            "success": False,
            "error": spawn_result.error or "Failed to spawn agent",
            "reasoning": reasoning.to_dict(),
            "speed": speed_payload,
        }

    response = _build_spawn_success_response(
        run_id=run_id,
        spawn_result=spawn_result,
        effective_isolation=effective_isolation,
        isolation_ctx=isolation_ctx,
        base_commit_sha=base_commit_sha,
        tmux_session_name=tmux_session_name,
        tmux_socket_name=tmux_socket_name,
        tmux_socket_path=tmux_socket_path,
        code_index_preflight_warning=(
            spawn_request.code_index_preflight_warning if spawn_request is not None else None
        ),
        reasoning=reasoning,
    )
    response["speed"] = speed_payload
    return response
