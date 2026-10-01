"""Persist, roll back, park and announce a daemon-stop resume successor."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from gobby.agents import resume_finalization
from gobby.storage import daemon_resume_keys
from gobby.storage.agents import AgentRun
from gobby.storage.terminals import Terminal

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.agents import LocalAgentRunManager

logger = logging.getLogger(__name__)


class _ResumeRunner(Protocol):
    @property
    def child_session_manager(self) -> Any: ...

    @property
    def run_storage(self) -> LocalAgentRunManager: ...


def _persist_resume_runtime(
    runner: _ResumeRunner,
    run_id: str,
    *,
    pid: int | None,
    terminal_id: str | None,
    worktree_id: str | None,
    clone_id: str | None,
) -> AgentRun | None:
    runner.run_storage.update_runtime(
        run_id,
        pid=pid,
        terminal_id=terminal_id,
        worktree_id=worktree_id,
        clone_id=clone_id,
    )
    transitioned = runner.run_storage.transition_resume_phase(
        run_id,
        expected_phase="launch_requested",
        new_phase="runtime_persisted",
    )
    if transitioned is None:
        current = runner.run_storage.get(run_id)
        if current is not None and (
            current.status in {"running", "success", "error", "timeout", "cancelled"}
            or (current.resume_metadata_json or {}).get("daemon_stop_resume_phase") == "finalized"
        ):
            return current
        return None
    started = runner.run_storage.start(run_id)
    if started is not None:
        return started
    current = runner.run_storage.get(run_id)
    return current if current is not None and current.status == "running" else None


async def _rollback_prepared_resume(
    runner: _ResumeRunner,
    *,
    original_run_id: str,
    successor_run_id: str,
    child_session_id: str,
) -> bool:
    from gobby.storage.agent_resume import rollback_prepared_daemon_resume

    return await asyncio.to_thread(
        rollback_prepared_daemon_resume,
        runner.run_storage.db,
        original_run_id=original_run_id,
        successor_run_id=successor_run_id,
        child_session_id=child_session_id,
    )


async def _park_unlaunched_successor(
    runner: _ResumeRunner,
    *,
    original_run: AgentRun,
    successor_run_id: str,
    child_session_id: str,
    completion_registry: CompletionEventRegistry | None,
) -> None:
    """Park a spawned-but-failed successor, containing every step.

    Runs inside the dispatcher's failure path: a raise here would leak the
    dispatch mutex and leave the successor provisional, so each step logs
    and continues instead of propagating.
    """
    from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state

    try:
        await resume_finalization.finalize_resume_handoff_async(
            runner.run_storage.db,
            original_run_id=original_run.id,
            successor_run_id=successor_run_id,
            child_session_id=child_session_id,
            completion_registry=completion_registry,
        )
    except Exception:
        logger.warning(
            "Failed to finalize handoff while parking unlaunched successor %s",
            successor_run_id,
            exc_info=True,
        )
    try:
        runner.run_storage.cancel(successor_run_id, terminal_reason="daemon_stop")
    except Exception:
        logger.warning(
            "Failed to park unlaunched successor %s",
            successor_run_id,
            exc_info=True,
        )
    try:
        await asyncio.to_thread(
            cleanup_agent_runtime_state,
            runner.run_storage.db,
            run_id=successor_run_id,
            child_session_id=child_session_id,
            terminal_reason="daemon_stop",
        )
    except Exception:
        logger.warning(
            "Failed runtime cleanup while parking unlaunched successor %s",
            successor_run_id,
            exc_info=True,
        )


def _fire_resume_started(
    original_run: AgentRun,
    run_id: str,
    provider: str,
    terminal_result: Any,
    terminal: Terminal | None,
    parent_session_id: str,
) -> None:
    try:
        from gobby.runner_broadcasting import fire_agent_event

        fire_agent_event(
            "agent_started",
            run_id,
            {
                daemon_resume_keys.RESUMED_FROM_RUN_ID_KEY: original_run.id,
                "parent_session_id": parent_session_id,
                "provider": provider,
                "pid": terminal_result.pid,
                "terminal_id": terminal.id if terminal is not None else None,
                "backend": terminal.backend if terminal is not None else None,
            },
        )
    except Exception as exc:
        logger.warning("Failed to fire resumed agent_started event for %s: %s", run_id, exc)
