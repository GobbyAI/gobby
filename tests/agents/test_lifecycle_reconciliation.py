"""Restart reconciliation and stale-pending reaping (plan 2.4.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

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
