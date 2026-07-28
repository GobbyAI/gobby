"""Shared helpers for agent-run completion and completion-registry wakeups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
from gobby.plans.review_terminal import terminalize_plan_review_run

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.events.completion_registry import CompletionEventRegistry


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
        current_run = runner.get_run(run_id)
        review_outcome = (
            await run_terminal_delivery_offload(
                terminalize_plan_review_run,
                runner.run_storage,
                db=runner.run_storage.db,
                run_id=run_id,
                action="complete",
                tool_calls_count=getattr(current_run, "tool_calls_count", 0),
                turns_used=getattr(current_run, "turns_used", 0),
            )
            if current_run is not None
            else None
        )
        if review_outcome is not None and review_outcome.handled:
            completed = review_outcome.run is not None
        else:
            completed = await run_terminal_delivery_offload(
                runner.complete_run,
                run_id,
                result=completion_result,
            )

        def read_terminal_run() -> Any:
            with runner.run_storage.db.bounded_transaction():
                return runner.get_run(run_id)

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
