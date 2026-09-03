"""Shared helpers for agent-run completion and completion-registry wakeups."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, TypedDict

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
from gobby.storage.tasks import LocalTaskManager

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.hub.protocol import HubDatabase


logger = logging.getLogger(__name__)


class TaskCompletionState(TypedDict):
    """Terminal task ownership fields delivered with every agent completion."""

    task_ref: str | None
    claimed_by: str | None
    is_escalated: bool
    escalation_reason: str | None
    linked_commits: list[str]


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
    message: str = "",
) -> bool:
    """Mark an agent run complete, then wake any waiters registered on it."""

    async def complete_and_deliver() -> bool:
        def read_terminal_run() -> Any:
            with runner.run_storage.db.bounded_transaction():
                return runner.get_run(run_id)

        completed = await run_terminal_delivery_offload(
            runner.complete_run,
            run_id,
            result=completion_result,
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
