"""Acceptance 2.5.4 / 2.5.6 / 2.5.22 / 2.5.31–32: external terminal discovery."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import ProjectOwnershipConflictError, TerminalManager
from gobby.terminals.discovery import seed_external_terminal
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _create_pending, _manager

pytestmark = pytest.mark.unit

_SOCKET = "/private/tmp/tmux-501/default"


@pytest.fixture(autouse=True)
def _machine() -> Any:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _context(
    *,
    pane: str = "%12",
    pid: int = 1658,
    start: int = 1784592177,
    name: str = "ext",
) -> dict[str, object]:
    return {
        "tmux_socket_path": _SOCKET,
        "tmux_pane": pane,
        "tmux_session": name,
        "tmux_window": "@1",
        "tmux_server_pid": pid,
        "tmux_server_start_time": start,
    }


def test_session_start_seeds_external_terminal(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    row = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context=_context(),
        generation={
            "socket_path": _SOCKET,
            "server_pid": 1658,
            "server_start_time": 1784592177,
            "pane_id": "%12",
        },
    )
    assert row is not None
    assert row.ownership == "external"
    assert row.state == "live"
    assert row.spawn_key is None


def test_concurrent_and_replayed_discovery_is_idempotent(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    generation = {
        "socket_path": _SOCKET,
        "server_pid": 1658,
        "server_start_time": 1784592177,
        "pane_id": "%12",
    }
    first = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context=_context(),
        generation=generation,
    )
    replay = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context=_context(name="renamed"),
        generation=generation,
    )
    assert first is not None and replay is not None
    assert replay.id == first.id
    assert replay.session_name == "renamed"
    recycled_pid = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context=_context(pid=9999),
        generation={**generation, "server_pid": 9999},
    )
    assert recycled_pid is not None and recycled_pid.id != first.id
    recycled_start = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context=_context(start=1),
        generation={**generation, "server_start_time": 1},
    )
    assert recycled_start is not None and recycled_start.id != first.id


def test_cross_project_discovery_is_conflict_until_exit(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.storage.projects import LocalProjectManager

    manager = _manager(temp_db)
    other = LocalProjectManager(temp_db).create("other-project", "/tmp/other").id
    generation = {
        "socket_path": _SOCKET,
        "server_pid": 1,
        "server_start_time": 2,
        "pane_id": "%3",
    }
    first = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context=_context(pane="%3", pid=1, start=2),
        generation=generation,
    )
    assert first is not None
    with pytest.raises(ProjectOwnershipConflictError):
        seed_external_terminal(
            manager,
            project_id=other,
            session_id=None,
            terminal_context=_context(pane="%3", pid=1, start=2),
            generation=generation,
        )
    manager.mark_exited(first.id)
    inserted = seed_external_terminal(
        manager,
        project_id=other,
        session_id=None,
        terminal_context=_context(pane="%3", pid=1, start=2),
        generation=generation,
    )
    assert inserted is not None
    assert inserted.id != first.id


def test_discovery_producers_share_composition_root_manager() -> None:
    from gobby.hooks.event_handlers import EventHandlers
    from gobby.sessions.liveness_monitor import SessionLivenessMonitor

    manager = MagicMock(spec=TerminalManager)
    handlers = EventHandlers(terminal_manager=manager)
    monitor = SessionLivenessMonitor(
        session_storage=MagicMock(),
        terminal_manager=manager,
    )
    assert handlers.terminal_manager is manager
    assert monitor.terminal_manager is manager
    assert handlers.terminal_manager is monitor.terminal_manager


def test_discovery_does_not_steal_unpromoted_managed_pane(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = _manager(temp_db)
    pending = _create_pending(manager, sample_project["id"], spawn_key="gobby-managed")
    generation = {
        "socket_path": _SOCKET,
        "server_pid": 4,
        "server_start_time": 5,
        "pane_id": "%6",
    }
    result = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context=_context(pane="%6", pid=4, start=5, name="gobby-managed"),
        generation=generation,
    )
    assert result is not None
    assert result.id == pending.id
    assert result.ownership == "gobby"
    assert manager.get(pending.id) is not None
