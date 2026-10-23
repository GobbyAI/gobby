"""Remote cancel: a run on another machine is stopped through its node, never by a local PID."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import gobby.agents.kill as kill
from gobby.hub import remote_cancel
from gobby.hub.caller_scope import bind_caller, reset_caller
from gobby.hub.node_client import NodeCommandError
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.terminals import TerminalManager, tmux_locator_key
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"
# Far above any live PID: a local-path probe finds nothing and signals nothing.
NO_SUCH_PID = 2147483646
TMUX_SOCKET = "/tmp/tmux-node/default"
TMUX_SERVER_PID = 1234
TMUX_SERVER_START = 1700000000
TMUX_PANE = "%3"
_STAMP = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def remote_machine(temp_db: HubDatabase) -> str:
    machine_id = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID)
    return machine_id


@pytest.fixture
def live_terminal(temp_db: HubDatabase, sample_project: dict[str, Any], remote_machine: str) -> str:
    manager = TerminalManager(temp_db)
    terminal_id = str(uuid4())
    manager.create_pending(
        terminal_id=terminal_id,
        project_id=sample_project["id"],
        backend="tmux",
        ownership="gobby",
        spawn_key=f"gobby-{terminal_id}",
        machine_id=remote_machine,
    )
    manager.promote_to_live(
        terminal_id,
        locator={
            "socket_path": TMUX_SOCKET,
            "server_pid": TMUX_SERVER_PID,
            "server_start_time": TMUX_SERVER_START,
            "pane_id": TMUX_PANE,
        },
        locator_key=tmux_locator_key(
            socket_path=TMUX_SOCKET,
            server_pid=TMUX_SERVER_PID,
            server_start_time=TMUX_SERVER_START,
            pane_id=TMUX_PANE,
        ),
    )
    return terminal_id


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    client = AsyncMock()
    monkeypatch.setattr(remote_cancel, "node_command_client", lambda: client)
    return client


def _run(machine_id: str, terminal_id: str | None, pid: int | None = NO_SUCH_PID) -> AgentRun:
    return AgentRun(
        id=str(uuid4()),
        parent_session_id=str(uuid4()),
        child_session_id=None,
        provider="claude",
        prompt="do it",
        status="running",
        created_at=_STAMP,
        updated_at=_STAMP,
        machine_id=machine_id,
        terminal_id=terminal_id,
        pid=pid,
    )


def _state(temp_db: HubDatabase, terminal_id: str) -> str | None:
    row = TerminalManager(temp_db).get(terminal_id)
    return None if row is None else row.state


async def test_remote_run_cancels_through_node(
    temp_db: HubDatabase,
    remote_machine: str,
    live_terminal: str,
    node: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signalled = MagicMock()
    monkeypatch.setattr(kill, "_signal_process_group", signalled)
    monkeypatch.setattr(kill, "pid_matches_agent_identity", AsyncMock(return_value=True))
    node.send.return_value = {"stopped": True}
    run = _run(remote_machine, live_terminal, pid=os.getpid())

    result = await kill.kill_agent(run, temp_db, signal_name="KILL", close_terminal=True)

    assert result["success"] is True
    node.send.assert_awaited_once_with(
        remote_machine,
        "agent.cancel",
        {"run_id": run.id, "terminal_id": live_terminal, "signal": "KILL", "close_terminal": True},
    )
    signalled.assert_not_called()
    assert result["method"] == "node_command"
    assert _state(temp_db, live_terminal) == "exited"


async def test_offline_node_is_typed(
    temp_db: HubDatabase,
    remote_machine: str,
    live_terminal: str,
    node: AsyncMock,
) -> None:
    node.send.side_effect = NodeCommandError("node_offline", "Target has no live channel", "none")
    run = _run(remote_machine, live_terminal)

    result = await kill.kill_agent(run, temp_db, close_terminal=True)

    assert result["success"] is False
    assert result["error_code"] == "node_offline"
    assert _state(temp_db, live_terminal) == "live"


async def test_close_terminal_settles_row(
    temp_db: HubDatabase,
    remote_machine: str,
    live_terminal: str,
    node: AsyncMock,
) -> None:
    node.send.return_value = {"stopped": True}
    run = _run(remote_machine, live_terminal)

    result = await kill.kill_agent(run, temp_db, close_terminal=True)

    assert result["success"] is True
    assert result["method"] == "node_command"
    assert _state(temp_db, live_terminal) == "exited"


async def test_malformed_reply_is_node_reply_lost(
    temp_db: HubDatabase,
    remote_machine: str,
    live_terminal: str,
    node: AsyncMock,
) -> None:
    node.send.return_value = {"ok": True}
    run = _run(remote_machine, live_terminal)

    result = await kill.kill_agent(run, temp_db, close_terminal=True)

    assert result["success"] is False
    assert result["error_code"] == "node_reply_lost"
    assert _state(temp_db, live_terminal) == "live"


async def test_no_terminal_is_refused_without_node_call(
    temp_db: HubDatabase,
    remote_machine: str,
    node: AsyncMock,
) -> None:
    run = _run(remote_machine, None)

    result = await kill.kill_agent(run, temp_db, close_terminal=True)

    assert result["success"] is False
    assert result["error_code"] == "no_terminal"
    node.send.assert_not_awaited()


async def test_not_found_counts_as_stopped(
    temp_db: HubDatabase,
    remote_machine: str,
    live_terminal: str,
    node: AsyncMock,
) -> None:
    node.send.side_effect = NodeCommandError("not_found", "Run not found on node", "none")
    run = _run(remote_machine, live_terminal)

    result = await kill.kill_agent(run, temp_db, close_terminal=True)

    assert result["success"] is True
    assert result["method"] == "node_command"
    assert _state(temp_db, live_terminal) == "exited"


async def test_not_stopped_is_failure(
    temp_db: HubDatabase,
    remote_machine: str,
    live_terminal: str,
    node: AsyncMock,
) -> None:
    node.send.return_value = {"stopped": False}
    run = _run(remote_machine, live_terminal)

    result = await kill.kill_agent(run, temp_db, close_terminal=True)

    assert result["success"] is False
    assert result["error_code"] == "not_stopped"
    assert _state(temp_db, live_terminal) == "live"


async def test_cancel_needs_owned_machine(
    temp_db: HubDatabase,
    remote_machine: str,
    live_terminal: str,
    node: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signalled = MagicMock()
    closed = AsyncMock(return_value={"success": True, "method": "runtime_terminate"})
    monkeypatch.setattr(kill, "_signal_process_group", signalled)
    monkeypatch.setattr(kill, "_close_managed_terminal", closed)
    node.send.return_value = {"stopped": True}
    LocalMachineManager(temp_db).upsert_seen(require_machine_id(), TEST_USER_ID)
    remote = _run(remote_machine, live_terminal)
    local = _run(require_machine_id(), live_terminal)

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        for run in (remote, local):
            refused = await kill.kill_agent(run, temp_db, signal_name="KILL", close_terminal=True)
            assert refused["success"] is False
            assert refused["error_code"] == "machine_not_owned"
    finally:
        reset_caller(token)
    node.send.assert_not_awaited()
    signalled.assert_not_called()
    closed.assert_not_awaited()
    assert _state(temp_db, live_terminal) == "live"

    token = bind_caller(TEST_USER_ID, temp_db)
    try:
        owned = await kill.kill_agent(remote, temp_db, close_terminal=True)
    finally:
        reset_caller(token)
    assert owned["success"] is True
    assert node.send.await_count == 1

    unbound = await kill.kill_agent(remote, temp_db, close_terminal=True)
    assert unbound["success"] is True
    assert node.send.await_count == 2
