"""Restart reconciliation and stale-pending reaping (plan 2.4.4)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.lifecycle_reconciliation import LifecycleReconciliation
from gobby.storage.terminals import TerminalManager
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "live", "orphaned"])
async def test_non_native_termination_is_fenced_without_runtime_probe(state: str) -> None:
    row = SimpleNamespace(id="terminal-1", backend="tmux", state=state)
    run = SimpleNamespace(id="run-1", terminal_id=row.id, resume_metadata_json={})
    storage = MagicMock()
    storage.list_termination_candidates.return_value = [run]
    manager = MagicMock()
    manager.get.return_value = row
    registry = MagicMock()

    async def run_db(fn: Callable[..., object], *args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)

    reconciler = LifecycleReconciliation(
        agent_run_manager=storage,
        db=MagicMock(),
        cleanup_handler=MagicMock(),
        run_db=run_db,
        terminal_manager=manager,
        runtime_registry=registry,
    )
    assert await reconciler.reconcile_pending_terminations(machine_id="machine-1") == 0
    reason = f"unsupported_terminal_backend:tmux:{state}"
    storage.merge_resume_metadata.assert_called_once_with(
        run.id,
        {"reconciliation_pending": True, "reconciliation_blocked_reason": reason},
    )
    registry.resolve.assert_not_called()

    run.resume_metadata_json = {"reconciliation_blocked_reason": reason}
    assert await reconciler.reconcile_pending_terminations(machine_id="machine-1") == 0
    storage.merge_resume_metadata.assert_called_once()
    registry.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_pending_terminal_reaped_after_failed_spawn() -> None:
    now = datetime.now(UTC)
    pending = make_memory_terminal()
    pending.state = "pending"
    pending.attempt_started_at = now - timedelta(seconds=200)
    store = MemoryTerminalStore(pending)
    runtime = FakeRuntime()
    cleanup = MagicMock()
    reconciler = LifecycleReconciliation(
        agent_run_manager=MagicMock(),
        db=MagicMock(),
        terminal_manager=cast(TerminalManager, store),
        runtime_registry=runtime_registry(runtime),
        cleanup_handler=cleanup,
        run_db=lambda fn, *args, **kwargs: fn(*args, **kwargs),
        spawn_in_doubt_seconds=150.0,
    )
    reaped = await reconciler.reap_stale_pending()
    assert reaped == 1
    row = store.get(pending.id)
    assert row is not None
    assert row.state == "exited"
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_reconciliation_resolves_activity_from_every_source() -> None:
    terminal = make_memory_terminal(
        terminal_id="terminal-reconcile",
        session_name="gobby-reconcile",
        backend="native",
    )
    run = SimpleNamespace(
        id="run-reconcile",
        terminal_id=terminal.id,
        child_session_id="session-reconcile",
        parent_session_id="parent-reconcile",
        pending_terminal_action=None,
        pending_terminal_reason=None,
        tool_calls_count=5,
        turns_used=3,
    )
    manager = MagicMock()
    manager.list_termination_candidates.return_value = [run]
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(tool_call_count=2, turn_count=6)
    transcript_reader = MagicMock()
    transcript_reader.get_activity_counts = AsyncMock(
        return_value={"message_count": 12, "tool_call_count": 7, "turn_count": 4}
    )

    async def run_db(fn: object, *args: object, **kwargs: object) -> object:
        return cast(MagicMock, fn)(*args, **kwargs)

    reconciler = LifecycleReconciliation(
        agent_run_manager=manager,
        db=MagicMock(),
        terminal_manager=cast(TerminalManager, MemoryTerminalStore(terminal)),
        runtime_registry=MagicMock(),
        cleanup_handler=MagicMock(),
        run_db=run_db,
        session_manager=session_manager,
        transcript_reader=transcript_reader,
    )
    termination = SimpleNamespace(success=True, error_code=None, error=None)

    with patch(
        "gobby.agents.lifecycle_reconciliation.terminate_managed_runtime_async",
        new=AsyncMock(return_value=termination),
    ) as terminate:
        reconciled = await reconciler.reconcile_pending_terminations(machine_id="machine-1")

    assert reconciled == 1
    transcript_reader.get_activity_counts.assert_awaited_once_with("session-reconcile")
    assert run.tool_calls_count == 7
    assert run.turns_used == 6
    termination_call = terminate.await_args
    assert termination_call is not None
    assert termination_call.kwargs["action"] == "complete"
