from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.storage.agents import AgentRun, AgentRunStatus, AgentRunTerminalReason

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class RecordingDb:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> SimpleNamespace:
        self.executed.append((sql, params))
        return SimpleNamespace(rowcount=1)

    def bounded_transaction(self) -> nullcontext[None]:
        return nullcontext()


class RecordingCompletionRegistry:
    def __init__(self) -> None:
        self.cleaned: list[str] = []

    def cleanup(self, completion_id: str) -> None:
        self.cleaned.append(completion_id)


class AcknowledgingCompletionRegistry(RecordingCompletionRegistry):
    def __init__(self, delivery: dict[str, bool] | None, events: list[str] | None = None) -> None:
        super().__init__()
        self.delivery = delivery
        self.events = events
        self.notifications: list[tuple[str, dict[str, object], str]] = []

    async def notify(
        self,
        completion_id: str,
        result: dict[str, object],
        message: str = "",
    ) -> dict[str, bool] | None:
        if self.events is not None:
            self.events.append("notify")
        self.notifications.append((completion_id, result, message))
        return self.delivery

    def cleanup(self, completion_id: str) -> None:
        if self.events is not None:
            self.events.append("cleanup")
        super().cleanup(completion_id)


def _run(
    task_id: str | None = "task-1",
    *,
    child_session_id: str | None = "child-1",
    status: AgentRunStatus = "success",
    terminal_reason: AgentRunTerminalReason | None = None,
    tool_calls_count: int = 0,
    turns_used: int = 0,
    reused_worktree: bool = False,
) -> AgentRun:
    return AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        child_session_id=child_session_id,
        provider="codex",
        prompt="test",
        status=status,
        terminal_reason=terminal_reason,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        updated_at=datetime(2026, 5, 20, tzinfo=UTC),
        task_id=task_id,
        worktree_id="wt-1",
        tool_calls_count=tool_calls_count,
        turns_used=turns_used,
        resume_metadata_json={"initial_variables": {"reused_worktree": True}}
        if reused_worktree
        else None,
    )


def _handler(
    db: object,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    *,
    agent_run_manager: Any | None = None,
    completion_registry: Any | None = None,
    session_manager: Any | None = None,
    session_coordinator: Any | None = None,
    task_recovery: Any | None = None,
    clone_storage: Any | None = None,
    terminal_services: Any | None = None,
) -> AgentCleanupHandler:
    async def default_run_db(
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return func(*args, **kwargs)

    clearable = MagicMock()
    return AgentCleanupHandler(
        agent_run_manager=agent_run_manager or MagicMock(),
        db=cast("HubDatabase", db),
        get_session_manager=lambda: session_manager,
        get_session_coordinator=lambda: session_coordinator,
        clone_storage=clone_storage,
        completion_registry=completion_registry,
        task_recovery=task_recovery or AsyncMock(),
        prompt_detector=clearable,
        terminal_prompt_monitor=clearable,
        stall_classifier=clearable,
        loop_tracker=clearable,
        master_fds={},
        run_db=run_db or default_run_db,
        terminal_services=terminal_services,
    )


class _RecordingTaskRecovery:
    """Task recovery fake that records terminal-agent recovery requests."""

    def __init__(self) -> None:
        self.recovered: list[tuple[AgentRun, str]] = []

    async def recover_task_from_terminal_agent(self, run: AgentRun, *, outcome: str) -> None:
        self.recovered.append((run, outcome))


class _FailingNotifyRegistry(RecordingCompletionRegistry):
    """Completion registry whose subscriber notification always raises."""

    async def notify(
        self,
        completion_id: str,
        result: dict[str, object],
        message: str = "",
    ) -> dict[str, bool] | None:
        raise RuntimeError("subscriber notification failed")


def _stub_runtime_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )
