"""Shared helpers for agent-run completion and completion-registry wakeups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.agents.agent_cleanup import (
    configure_terminal_delivery_offload as configure_terminal_delivery_offload,
)
from gobby.agents.agent_cleanup import (
    deliver_and_cleanup_terminal_run,
    run_terminal_delivery_offload,
    shielded_terminal_delivery,
)
from gobby.agents.agent_cleanup import (
    reset_terminal_delivery_offload as reset_terminal_delivery_offload,
)

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

        result = notify_result or {
            "status": current.status,
            "run_id": run_id,
            "error": getattr(current, "error", None),
        }
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
