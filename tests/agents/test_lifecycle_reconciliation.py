"""Restart reconciliation and stale-pending reaping (plan 2.4.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.lifecycle_reconciliation import LifecycleReconciliation
from gobby.storage.terminals import TerminalManager
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit


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
        runtime_registry=MagicMock(),
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
