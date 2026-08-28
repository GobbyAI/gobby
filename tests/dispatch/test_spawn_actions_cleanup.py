"""Dispatch spawn cleanup closes terminals through the shared terminal services."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import kill as agent_kill
from gobby.agents.terminal_delivery import run_terminal_delivery_offload
from gobby.dispatch import spawn_actions
from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.mutex import RuntimeDispatchMutex
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


async def test_cleanup_unattached_spawned_run_closes_terminal_with_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id="run-1", status="running")
    storage = MagicMock()
    storage.get.return_value = run
    storage.fail.return_value = run
    monkeypatch.setattr(spawn_actions, "LocalAgentRunManager", lambda _db: storage)
    delivered = AsyncMock()
    monkeypatch.setattr(spawn_actions, "deliver_existing_terminal_run_in_scope", delivered)
    kill_agent = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(agent_kill, "kill_agent", kill_agent)
    terminal_services = object()
    db = cast(HubDatabase, object())

    terminated = await spawn_actions.cleanup_unattached_spawned_run(
        "run-1", db=db, error="boom", terminal_services=terminal_services
    )

    assert terminated is True
    storage.get.assert_called_once_with("run-1")
    assert kill_agent.await_args is not None
    assert kill_agent.await_args.args == (run, db)
    assert kill_agent.await_args.kwargs == {
        "close_terminal": True,
        "terminal_services": terminal_services,
    }
    assert storage.fail.call_args.kwargs == {"error": "dispatch spawn cleanup: boom"}
    delivered.assert_awaited_once_with(
        db=db,
        agent_run_manager=storage,
        completion_registry=None,
        run_id="run-1",
        run_db=run_terminal_delivery_offload,
    )


async def test_cleanup_or_quarantine_forwards_terminal_services() -> None:
    cleanup = AsyncMock(return_value=True)
    quarantine = AsyncMock()
    terminal_services = object()
    db = cast(HubDatabase, object())

    terminated = await spawn_actions._cleanup_or_quarantine_spawned_run(
        cast(SpawnAgentAction, object()),
        run_id="run-1",
        mutex=cast(RuntimeDispatchMutex, object()),
        db=db,
        error="boom",
        completion_registry=None,
        terminal_services=terminal_services,
        cleanup_unattached_spawned_run=cleanup,
        quarantine_unterminated_spawned_run=quarantine,
    )

    assert terminated is True
    cleanup.assert_awaited_once_with(
        "run-1",
        db=db,
        error="boom",
        completion_registry=None,
        terminal_services=terminal_services,
    )
    quarantine.assert_not_awaited()
