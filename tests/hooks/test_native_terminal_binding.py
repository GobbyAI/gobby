"""Native terminal binding verifies the CLI's actual terminal."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import psutil
import pytest

from gobby.hooks.event_handlers._session_start.terminal_runtime import bind_discovered_terminal
from gobby.sessions.terminal_binding import cli_shares_shell_tty
from gobby.storage.terminals import Terminal


def test_native_binding_refuses_a_different_cli_tty() -> None:
    terminal_id = "20000000-0000-4000-8000-000000000088"
    terminal = SimpleNamespace(id=terminal_id, process={"pgid": 101}, session_id=None)
    manager = MagicMock()
    manager.get.return_value = terminal
    logger = MagicMock()
    sessions = MagicMock()
    context = {"gobby_terminal_id": terminal_id, "parent_pid": 202}
    processes = {
        101: SimpleNamespace(terminal=lambda: "/dev/ttys001"),
        202: SimpleNamespace(terminal=lambda: "/dev/ttys002"),
    }

    with patch("gobby.sessions.terminal_binding.psutil.Process", side_effect=processes.__getitem__):
        pending = bind_discovered_terminal(
            manager,
            logger,
            session_id="session-1",
            project_id="project-1",
            terminal_context=context,
            session_manager=sessions,
        )

    manager.bind_session.assert_not_called()
    assert pending is None
    assert context == {"parent_pid": 202}
    logger.info.assert_called_once()
    sessions.clear_gobby_terminal_id.assert_called_once_with("session-1")


def test_native_binding_accepts_matching_cli_tty() -> None:
    terminal_id = "20000000-0000-4000-8000-000000000088"
    terminal = SimpleNamespace(id=terminal_id, process={"pgid": 101}, session_id=None)
    manager = MagicMock()
    manager.get.return_value = terminal
    manager.bind_session.return_value = terminal
    logger = MagicMock()
    sessions = MagicMock()
    context = {"gobby_terminal_id": terminal_id, "parent_pid": 202}
    with patch(
        "gobby.sessions.terminal_binding.psutil.Process",
        return_value=SimpleNamespace(terminal=lambda: "/dev/ttys001"),
    ):
        pending = bind_discovered_terminal(
            manager,
            logger,
            session_id="session-1",
            project_id="project-1",
            terminal_context=context,
            session_manager=sessions,
        )
    manager.bind_session.assert_called_once_with(terminal_id, "session-1", "project-1")
    sessions.clear_gobby_terminal_id.assert_not_called()
    assert pending is None
    assert context["gobby_terminal_id"] == terminal_id


@pytest.mark.parametrize("cli_pid", [None, True, "202", 0, -1])
def test_native_binding_clears_route_when_cli_pid_is_invalid(cli_pid: object) -> None:
    manager = MagicMock()
    sessions = MagicMock()
    context = {"gobby_terminal_id": "native-pane", "parent_pid": cli_pid}
    assert (
        bind_discovered_terminal(
            manager,
            MagicMock(),
            session_id="session-1",
            project_id="project-1",
            terminal_context=context,
            session_manager=sessions,
        )
        is None
    )
    manager.bind_session.assert_not_called()
    sessions.clear_gobby_terminal_id.assert_called_once_with("session-1")
    assert context == {"parent_pid": cli_pid}


def test_native_binding_clears_route_when_row_is_missing() -> None:
    manager = MagicMock()
    manager.get.return_value = None
    sessions = MagicMock()
    assert (
        bind_discovered_terminal(
            manager,
            MagicMock(),
            session_id="session-1",
            project_id="project-1",
            terminal_context={"gobby_terminal_id": "missing", "parent_pid": 202},
            session_manager=sessions,
        )
        is None
    )
    manager.bind_session.assert_not_called()
    sessions.clear_gobby_terminal_id.assert_called_once_with("session-1")


@pytest.mark.parametrize("shell_pid", [None, True, "101", 0, -1])
def test_cli_tty_proof_requires_a_valid_shell_pid(shell_pid: object) -> None:
    terminal = cast(Terminal, SimpleNamespace(process={"pgid": shell_pid}))
    assert cli_shares_shell_tty(202, terminal) is False


@pytest.mark.parametrize("cli_tty,shell_tty", [(None, "/dev/ttys001"), ("/dev/ttys001", None)])
def test_cli_tty_proof_requires_both_ttys(cli_tty: str | None, shell_tty: str | None) -> None:
    terminal = cast(Terminal, SimpleNamespace(process={"pgid": 101}))
    processes = {
        202: SimpleNamespace(terminal=lambda: cli_tty),
        101: SimpleNamespace(terminal=lambda: shell_tty),
    }
    with patch("gobby.sessions.terminal_binding.psutil.Process", side_effect=processes.__getitem__):
        assert cli_shares_shell_tty(202, terminal) is False


@pytest.mark.parametrize("error", [psutil.AccessDenied(202), psutil.NoSuchProcess(202), OSError()])
def test_cli_tty_proof_refuses_failed_inspection(error: Exception) -> None:
    terminal = cast(Terminal, SimpleNamespace(process={"pgid": 101}))
    with patch("gobby.sessions.terminal_binding.psutil.Process", side_effect=error):
        assert cli_shares_shell_tty(202, terminal) is False
