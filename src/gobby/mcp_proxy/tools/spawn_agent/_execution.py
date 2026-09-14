"""Post-execute_spawn helpers extracted so spawn_agent modules stay under 1,000 lines."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import (
    cleanup_failed_spawn,
    start_run_or_cleanup,
)
from gobby.mcp_proxy.tools.spawn_agent._health import (
    _terminal_is_live,
    schedule_tmux_health_check,
)
from gobby.mcp_proxy.tools.spawn_agent._response import (
    _tmux_runtime_metadata,
    build_spawn_response,
)
from gobby.mcp_proxy.tools.spawn_agent._runtime import _persist_spawn_runtime
from gobby.mcp_proxy.tools.spawn_agent._step_state import apply_claimed_step_update
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_actionable

logger = logging.getLogger(__name__)


def _link_auto_claimed_session(task_manager: Any, session_id: str, task_id: str) -> None:
    """Record the ``claimed`` session-task link the ``claim_task`` tool writes.

    The spawn-time claim replaces the agent's own ``claim_task`` call, so without
    this row the implementer session is invisible to close-time evidence merging
    (#21102). Best-effort, like the tool's own linking.
    """
    from gobby.storage.session_tasks import SessionTaskManager

    try:
        SessionTaskManager(task_manager.db).link_task(session_id, task_id, "claimed")
    except Exception as exc:
        logger.debug("Best-effort auto-claim session linking failed: %s", exc)


def _clear_parent_claim_session_variables(
    task_manager: Any,
    parent_session_id: str,
    task_id: str,
) -> None:
    """Remove a transferred task from the parent's claim variables."""
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.task_claim_state import remove_claimed_task

    try:
        session_var_manager = SessionVariableManager(task_manager.db)
        session_vars = session_var_manager.get_variables(parent_session_id)
        merge_dict = remove_claimed_task(session_vars, task_id)
        session_var_manager.merge_existing_variables(parent_session_id, merge_dict)
    except Exception as exc:
        logger.debug("Best-effort parent claim variable cleanup failed: %s", exc)


def _transfer_parent_owned_task_claim(
    task_manager: Any,
    task_id: str,
    *,
    parent_session_id: str,
    child_session_id: str,
) -> Any:
    """Transfer a task claim while the spawn path holds its task mutex."""
    current_owner = get_claimed_session_id(task_manager.get_task(task_id))
    if current_owner != parent_session_id:
        raise RuntimeError(
            f"Task {task_id} claim owner changed from parent session "
            f"{parent_session_id} to {current_owner}"
        )
    task_manager.release_task_claim(task_id)
    return task_manager.claim_task(task_id, session_id=child_session_id)


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
    parent_session_id: str,
    effective_provider: str,
    resolved_task_id: str | None,
    task_seq_num: Any,
    db: Any,
    agent_body: Any,
    effective_initial_variables: Any,
    reasoning: Any,
) -> dict[str, Any]:
    """Persist runtime, verify liveness, start the run, auto-claim, and build the response."""
    failure_identity = {
        "run_id": run_id,
        "worktree_id": isolation_ctx.worktree_id,
        "branch_name": isolation_ctx.branch_name,
    }
    terminal_id = getattr(spawn_result, "terminal_id", None)
    manager = getattr(spawn_request, "terminal_manager", None)
    registry = getattr(spawn_request, "terminal_runtime_registry", None)
    terminal = None
    if isinstance(terminal_id, str) and manager is not None:
        candidate = await asyncio.to_thread(manager.get, terminal_id)
        if isinstance(getattr(candidate, "backend", None), str):
            terminal = candidate
    tmux_socket_name, tmux_socket_path = _tmux_runtime_metadata(terminal)
    await asyncio.to_thread(
        _persist_spawn_runtime,
        runner,
        run_id,
        spawn_result,
        worktree_id=isolation_ctx.worktree_id,
        clone_id=isolation_ctx.clone_id,
        terminal_id=terminal_id,
    )
    if spawn_result.success and terminal is not None and registry is not None:
        alive, pane_output = await _terminal_is_live(terminal, registry)
        if not alive:
            spawn_result.success = False
            spawn_result.status = "failed"
            spawn_result.error = (
                f"{terminal.backend} terminal '{terminal.id}' failed liveness verification"
            )
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
                terminal_id=terminal_id,
            )
            return {
                "success": False,
                "error": spawn_result.error,
                **failure_identity,
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
            pid=spawn_result.pid,
            terminal_id=terminal_id,
        )
        if start_error is not None:
            return {**start_error, **failure_identity}

        commit_environment = getattr(handler, "commit_environment", None)
        if callable(commit_environment):
            await asyncio.to_thread(commit_environment, spawn_config)

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
                    "tmux_socket_name": tmux_socket_name,
                    "tmux_socket_path": tmux_socket_path,
                    "terminal_id": terminal_id,
                    "backend": getattr(terminal, "backend", None),
                },
            )
        except Exception as e:
            logger.debug("Failed to fire agent_started event for %s: %s", run_id, e)

        if resolved_task_id and task_manager:
            try:
                task_obj = await asyncio.to_thread(task_manager.get_task, resolved_task_id)
                if not task_obj or not is_task_actionable(task_obj):
                    logger.info(
                        "Skipping auto-claim for task %s; task is not actionable",
                        f"#{task_seq_num}" if task_seq_num else resolved_task_id,
                    )
                elif (
                    (current_owner := get_claimed_session_id(task_obj))
                    and current_owner != spawn_result.child_session_id
                    and current_owner != parent_session_id
                ):
                    logger.info(
                        "Skipping auto-claim for task %s; already assigned to %s",
                        f"#{task_seq_num}" if task_seq_num else resolved_task_id,
                        current_owner,
                    )
                else:
                    transferred_parent_claim = (
                        current_owner == parent_session_id
                        and current_owner != spawn_result.child_session_id
                    )
                    if transferred_parent_claim:
                        claimed_task = await asyncio.to_thread(
                            _transfer_parent_owned_task_claim,
                            task_manager,
                            resolved_task_id,
                            parent_session_id=parent_session_id,
                            child_session_id=spawn_result.child_session_id,
                        )
                    else:
                        claimed_task = await asyncio.to_thread(
                            task_manager.claim_task,
                            resolved_task_id,
                            session_id=spawn_result.child_session_id,
                        )
                    task_owned_by_child = (
                        get_claimed_session_id(claimed_task) == spawn_result.child_session_id
                    )
                    if transferred_parent_claim:
                        logger.info(
                            "Transferred task %s claim from parent session %s to agent %s "
                            "(session %s)",
                            (f"#{task_seq_num}" if task_seq_num else resolved_task_id),
                            parent_session_id,
                            run_id,
                            spawn_result.child_session_id,
                        )
                    else:
                        logger.info(
                            "Auto-claimed task %s for agent %s (session %s)",
                            (f"#{task_seq_num}" if task_seq_num else resolved_task_id),
                            run_id,
                            spawn_result.child_session_id,
                        )
                    if task_owned_by_child:
                        await asyncio.to_thread(
                            _link_auto_claimed_session,
                            task_manager,
                            spawn_result.child_session_id,
                            resolved_task_id,
                        )
                        if transferred_parent_claim:
                            await asyncio.to_thread(
                                _clear_parent_claim_session_variables,
                                task_manager,
                                parent_session_id,
                                resolved_task_id,
                            )
                    if (
                        task_owned_by_child
                        and db is not None
                        and agent_body is not None
                        and agent_body.step_workflow is not None
                    ):
                        await asyncio.to_thread(
                            apply_claimed_step_update,
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
                    terminal_id=terminal_id,
                )
                return {
                    "success": False,
                    "error": error,
                    **failure_identity,
                }

        if terminal is not None:
            schedule_tmux_health_check(
                runner,
                run_id,
                terminal.id,
                completion_registry,
            )
    else:
        if spawn_result.retryable_infrastructure is True:
            await asyncio.to_thread(
                runner.run_storage.merge_resume_metadata,
                run_id,
                {"spawn_retryable_infrastructure": True},
            )
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
            terminal_id=terminal_id,
        )

    if not spawn_result.success:
        return {
            "success": False,
            "error": spawn_result.error or "Failed to spawn agent",
            **failure_identity,
            "reasoning": reasoning.to_dict(),
        }

    response = build_spawn_response(
        run_id=run_id,
        spawn_result=spawn_result,
        effective_isolation=effective_isolation,
        isolation_ctx=isolation_ctx,
        base_commit_sha=base_commit_sha,
        code_index_preflight_warning=(
            spawn_request.code_index_preflight_warning if spawn_request is not None else None
        ),
        reasoning=reasoning,
        terminal=terminal,
    )
    return response
