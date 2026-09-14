"""Shared helpers for agent-run completion and completion-registry wakeups."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, TypedDict

from gobby.agents.lifecycle_checkout import resolve_session_checkout_root
from gobby.agents.terminal_delivery import (
    configure_terminal_delivery_offload as configure_terminal_delivery_offload,
)
from gobby.agents.terminal_delivery import (
    deliver_and_cleanup_terminal_run,
    run_terminal_delivery_offload,
    shielded_terminal_delivery,
)
from gobby.agents.terminal_delivery import (
    reset_terminal_delivery_offload as reset_terminal_delivery_offload,
)
from gobby.storage.clones import LocalCloneManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import (
    task_edited_file_set,
    task_edited_file_set_for_checkout,
)
from gobby.workflows.task_dirty_state import task_dirty_paths_async

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.agents import AgentRunTerminalReason
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager


logger = logging.getLogger(__name__)


def _agent_run_checkout_root(
    db: HubDatabase,
    session_manager: SessionManager | None,
    run: Any,
) -> str | None:
    """Resolve the checkout containing one agent run's task edits."""
    worktree_id = getattr(run, "worktree_id", None)
    if isinstance(worktree_id, str) and worktree_id:
        try:
            worktree = LocalWorktreeManager(db).get(worktree_id)
            if worktree is not None and worktree.worktree_path:
                return str(worktree.worktree_path)
        except Exception:
            logger.debug("Failed to resolve worktree %s", worktree_id, exc_info=True)
        return None

    clone_id = getattr(run, "clone_id", None)
    if isinstance(clone_id, str) and clone_id:
        try:
            clone = LocalCloneManager(db).get(clone_id)
            if clone is not None and clone.clone_path:
                return str(clone.clone_path)
        except Exception:
            logger.debug("Failed to resolve clone %s", clone_id, exc_info=True)
        return None

    child_session_id = getattr(run, "child_session_id", None)
    if not isinstance(child_session_id, str) or not child_session_id or session_manager is None:
        return None
    try:
        session = session_manager.get(child_session_id)
        root = resolve_session_checkout_root(db, session) if session is not None else None
        return str(root) if root else None
    except Exception:
        logger.debug(
            "Failed to resolve checkout for agent session %s",
            child_session_id,
            exc_info=True,
        )
        return None


def _agent_run_task_dirty_scope(
    db: HubDatabase,
    session_manager: SessionManager | None,
    run: Any,
    *,
    variables: dict[str, Any] | None = None,
) -> tuple[set[str], str] | None:
    """Resolve attributed paths and checkout root without running Git."""
    task_id = getattr(run, "task_id", None)
    if not isinstance(task_id, str) or not task_id:
        return None

    child_session_id = getattr(run, "child_session_id", None)
    if variables is None:
        variables = {}
        if isinstance(child_session_id, str) and child_session_id:
            try:
                variables = SessionVariableManager(db).get_variables(child_session_id)
            except Exception:
                logger.debug(
                    "Failed to read task edit attribution for agent session %s",
                    child_session_id,
                    exc_info=True,
                )

    checkout_root = _agent_run_checkout_root(db, session_manager, run)
    if checkout_root is None:
        return None
    attributed = task_edited_file_set_for_checkout(variables, task_id, checkout_root)
    if not attributed:
        attributed = task_edited_file_set(variables, task_id)
    checkout_ids = (getattr(run, "worktree_id", None), getattr(run, "clone_id", None))
    isolated = any(
        isinstance(checkout_id, str) and bool(checkout_id) for checkout_id in checkout_ids
    )
    if not attributed and isolated:
        attributed = {"."}
    return attributed, checkout_root


async def agent_run_task_dirty_paths(
    db: HubDatabase,
    session_manager: SessionManager | None,
    run: Any,
    *,
    variables: dict[str, Any] | None = None,
) -> list[str] | None:
    """Return dirty task paths without running Git or DB work on the event loop."""
    scope = await asyncio.to_thread(
        _agent_run_task_dirty_scope,
        db,
        session_manager,
        run,
        variables=variables,
    )
    if scope is None:
        return []
    attributed, checkout_root = scope
    dirty = await task_dirty_paths_async(attributed, checkout_root)
    return None if dirty is None else sorted(dirty)


def build_agent_exit_notification(
    run_id: str,
    *,
    variables: dict[str, Any],
    dirty_paths: list[str] | None,
) -> tuple[AgentRunTerminalReason | None, dict[str, Any], str]:
    """Build the structured result shared by cooperative and recovered exits."""
    terminal_reason: AgentRunTerminalReason | None = (
        "task_blocker" if variables.get("blocker_handed_off") is True else None
    )
    early_exit_step = variables.get("_agent_early_exit_step")
    if terminal_reason is None and isinstance(early_exit_step, str) and early_exit_step:
        terminal_reason = "early_exit"
    notify_result: dict[str, Any] = {
        "status": {"task_blocker": "blocked", "early_exit": "incomplete"}.get(
            terminal_reason or "", "success"
        ),
        "run_id": run_id,
        "dirty_paths": dirty_paths,
    }
    if terminal_reason is not None:
        notify_result["terminal_reason"] = terminal_reason
    if terminal_reason == "early_exit":
        notify_result["incomplete_step"] = early_exit_step
    verdict = variables.get("adversary_verdict")
    if isinstance(verdict, str) and verdict:
        notify_result["signoff_message"] = verdict
    message = (
        f"Agent {run_id} completed; dirty_paths={json.dumps(dirty_paths, separators=(',', ':'))}"
    )
    return terminal_reason, notify_result, message


class TaskCompletionState(TypedDict):
    """Terminal task ownership fields delivered with every agent completion."""

    task_ref: str | None
    claimed_by: str | None
    is_escalated: bool
    escalation_reason: str | None
    linked_commits: list[str]


def closed_task_completion_result(task: Task, result: str | None = None) -> str | None:
    """Append authoritative close metadata to a task-bound agent result."""
    if task.closed_at is None:
        return None
    task_ref = f"#{task.seq_num}" if task.seq_num is not None else task.id[:8]
    suffix = (
        "Task completion: "
        f"task={task_ref}; closed_at={task.closed_at.isoformat()}; "
        f"commit_sha={task.closed_commit_sha or '<none>'}"
    )
    base_result = result.rstrip() if result else ""
    if base_result.endswith(suffix):
        return base_result
    return f"{base_result}\n\n{suffix}" if base_result else suffix


def closed_task_run_completion_result(
    db: HubDatabase,
    run: Any,
    result: str | None = None,
) -> str | None:
    """Add authoritative close metadata when a successful run's task is closed."""
    task_id = getattr(run, "task_id", None)
    if not isinstance(task_id, str) or not task_id:
        return result

    try:
        task = LocalTaskManager(db).get_task(task_id)
    except Exception:
        logger.warning(
            "Failed to read task %s while completing agent run %s",
            task_id,
            getattr(run, "id", "unknown"),
            exc_info=True,
        )
        return result
    if task.closed_at is None:
        return result

    base_result = result if result is not None else getattr(run, "result", None)
    return closed_task_completion_result(
        task,
        base_result if isinstance(base_result, str) else None,
    )


def _task_completion_state(db: HubDatabase, task_id: str | None) -> TaskCompletionState:
    state: TaskCompletionState = {
        "task_ref": task_id,
        "claimed_by": None,
        "is_escalated": False,
        "escalation_reason": None,
        "linked_commits": [],
    }
    if task_id is None:
        return state

    try:
        task = LocalTaskManager(db).get_task(task_id)
    except Exception:
        logger.warning("Failed to read terminal task state for %s", task_id, exc_info=True)
        return state

    state["task_ref"] = f"#{task.seq_num}" if task.seq_num else task.id
    state["claimed_by"] = task.claimed_by_session_id
    state["is_escalated"] = task.is_escalated
    state["escalation_reason"] = task.escalation_reason
    state["linked_commits"] = list(task.commits or [])
    return state


def build_workflow_completion_notification(
    db: HubDatabase,
    run_id: str,
    workflow: str,
    task_id: str | None,
) -> tuple[dict[str, Any], str]:
    """Build one completion payload and message format for taskful and taskless runs."""
    task_state = _task_completion_state(db, task_id)
    notify_result: dict[str, Any] = {
        "status": "success",
        "run_id": run_id,
        "via": "workflow_terminate",
        "workflow": workflow,
        **task_state,
    }
    task_ref = task_state["task_ref"] or "none"
    claimed_by = task_state["claimed_by"] or "none"
    escalation_reason = (
        json.dumps(task_state["escalation_reason"], ensure_ascii=False)
        if task_state["escalation_reason"] is not None
        else "none"
    )
    linked_commits = json.dumps(task_state["linked_commits"], separators=(",", ":"))
    message = (
        f"Agent {run_id} completed via workflow terminate; "
        f"task_ref={task_ref}; claimed_by={claimed_by}; "
        f"is_escalated={str(task_state['is_escalated']).lower()}; "
        f"escalation_reason={escalation_reason}; linked_commits={linked_commits}"
    )
    return notify_result, message


async def complete_and_notify_agent_run(
    runner: AgentRunner,
    run_id: str,
    *,
    completion_registry: CompletionEventRegistry | None = None,
    notify_result: dict[str, Any] | None = None,
    completion_result: str | None = None,
    terminal_reason: AgentRunTerminalReason | None = None,
    message: str = "",
) -> bool:
    """Mark an agent run complete, then wake any waiters registered on it."""

    async def complete_and_deliver() -> bool:
        def read_terminal_run() -> Any:
            with runner.run_storage.db.bounded_transaction():
                return runner.get_run(run_id)

        if terminal_reason is None:
            completed = await run_terminal_delivery_offload(
                runner.complete_run,
                run_id,
                result=completion_result,
            )
        else:
            completed = await run_terminal_delivery_offload(
                runner.complete_run,
                run_id,
                result=completion_result,
                terminal_reason=terminal_reason,
            )

        current = await run_terminal_delivery_offload(read_terminal_run)
        if current is None or current.status not in {"success", "error", "timeout", "cancelled"}:
            return completed

        result = dict(notify_result) if notify_result is not None else {"status": current.status}
        result["run_id"] = run_id
        if notify_result is None:
            error = getattr(current, "error", None)
            if error is not None:
                result["error"] = error
        elif result.get("error") is None:
            result.pop("error", None)
        await deliver_and_cleanup_terminal_run(
            db=runner.run_storage.db,
            completion_registry=completion_registry,
            run_id=run_id,
            result=result,
            message=message,
            run_db=run_terminal_delivery_offload,
        )
        return completed

    settled = await shielded_terminal_delivery(run_id, complete_and_deliver)
    return bool(settled)
