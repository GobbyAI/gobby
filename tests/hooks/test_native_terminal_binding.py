"""Native terminal binding verifies the CLI's actual terminal."""

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import psutil
import pytest

from gobby.hooks.event_handlers._session_start.terminal_runtime import bind_discovered_terminal
from gobby.sessions.terminal_binding import cli_shares_shell_tty
from gobby.storage.terminals import Terminal
from gobby.terminals import foreground
from gobby.terminals.foreground import process_tty


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
    terminal_id = "20000000-0000-4000-8000-000000000088"
    manager.get.return_value = SimpleNamespace(
        id=terminal_id, process={"pgid": 101}, session_id=None
    )
    sessions = MagicMock()
    context = {"gobby_terminal_id": terminal_id, "parent_pid": cli_pid}
    with patch(
        "gobby.hooks.event_handlers._session_start.terminal_runtime.cli_tty_refusal",
        return_value=None,
    ) as shell_proof:
        result = bind_discovered_terminal(
            manager,
            MagicMock(),
            session_id="session-1",
            project_id="project-1",
            terminal_context=context,
            session_manager=sessions,
        )
    assert result is None
    shell_proof.assert_not_called()
    manager.bind_session.assert_not_called()
    sessions.clear_gobby_terminal_id.assert_called_once_with("session-1")
    assert context == {"parent_pid": cli_pid}


def test_native_binding_clears_route_when_terminal_id_is_malformed() -> None:
    manager = MagicMock()
    manager.get.side_effect = ValueError("badly formed hexadecimal UUID string")
    sessions = MagicMock()
    context = {"gobby_terminal_id": "%proof-id", "parent_pid": 202}

    result = bind_discovered_terminal(
        manager,
        MagicMock(),
        session_id="session-1",
        project_id="project-1",
        terminal_context=context,
        session_manager=sessions,
    )

    assert result is None
    assert "gobby_terminal_id" not in context
    manager.bind_session.assert_not_called()
    sessions.clear_gobby_terminal_id.assert_called_once_with("session-1")


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


# The child makes the pty its controlling terminal, reports ready, then waits on stdin.
_CONTROLLING_TTY_CHILD = (
    "import fcntl, os, sys, termios\n"
    "fd = os.open(sys.argv[1], os.O_RDWR)\n"
    "fcntl.ioctl(fd, termios.TIOCSCTTY, 0)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def child_on_fresh_pty() -> Iterator[tuple[int, str]]:
    """A live process whose controlling terminal is a pty opened for this test."""
    master, slave = os.openpty()
    tty_name = os.ttyname(slave)
    child = subprocess.Popen(
        [sys.executable, "-c", _CONTROLLING_TTY_CHILD, tty_name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        yield child.pid, tty_name
    finally:
        if child.stdin is not None:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        os.close(slave)
        os.close(master)
        psutil._psposix.get_terminal_map.cache_clear()


@pytest.mark.skipif(os.name != "posix", reason="needs a POSIX pty and controlling terminal")
def test_cli_tty_proof_sees_a_pty_missing_from_the_cached_device_map(
    child_on_fresh_pty: tuple[int, str],
) -> None:
    child_pid, tty_name = child_on_fresh_pty
    psutil._psposix.get_terminal_map.cache_clear()
    # A fresh map resolves the child's controlling terminal to the pty it opened.
    assert psutil.Process(child_pid).terminal() == tty_name
    # The memoized map is one shared dict; dropping the child's device makes the miss exact.
    psutil._psposix.get_terminal_map().pop(os.stat(tty_name).st_rdev)
    assert psutil.Process(child_pid).terminal() is None
    terminal = cast(Terminal, SimpleNamespace(process={"pgid": child_pid}))

    assert cli_shares_shell_tty(child_pid, terminal) is True


def _refusal_messages(caplog: pytest.LogCaptureFixture, ttys: dict[int, str | None]) -> list[str]:
    terminal_id = "20000000-0000-4000-8000-000000000088"
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(
        id=terminal_id, process={"pgid": 101}, session_id=None
    )
    logger = logging.getLogger("tests.native_terminal_binding")
    processes = {pid: SimpleNamespace(terminal=lambda tty=tty: tty) for pid, tty in ttys.items()}
    caplog.set_level(logging.INFO, logger=logger.name)
    with patch("gobby.sessions.terminal_binding.psutil.Process", side_effect=processes.__getitem__):
        result = bind_discovered_terminal(
            manager,
            logger,
            session_id="session-1",
            project_id="project-1",
            terminal_context={"gobby_terminal_id": terminal_id, "parent_pid": 202},
            session_manager=MagicMock(),
        )
    assert result is None
    manager.bind_session.assert_not_called()
    return [record.getMessage() for record in caplog.records if record.name == logger.name]


def test_native_binding_refusal_names_an_empty_tty(caplog: pytest.LogCaptureFixture) -> None:
    messages = _refusal_messages(caplog, {101: None, 202: None})
    assert len(messages) == 1
    assert "empty tty" in messages[0]


def test_native_binding_refusal_names_a_tty_mismatch_with_both_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = _refusal_messages(caplog, {101: "/dev/ttys001", 202: "/dev/ttys002"})
    assert len(messages) == 1
    assert "tty mismatch" in messages[0]
    assert "/dev/ttys001" in messages[0] and "/dev/ttys002" in messages[0]


def test_a_missing_device_map_cache_is_logged_once_rather_than_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A psutil without the private map cache cannot reread new ptys; that must be visible."""
    monkeypatch.setattr(psutil, "_psposix", SimpleNamespace(), raising=False)
    foreground._warn_terminal_map_cache_missing.cache_clear()
    reads: list[None] = []

    class _NoTty:
        def terminal(self) -> str | None:
            reads.append(None)
            return None

    try:
        with caplog.at_level(logging.WARNING, logger=foreground.logger.name):
            assert process_tty(_NoTty()) is None
            assert process_tty(_NoTty()) is None
    finally:
        foreground._warn_terminal_map_cache_missing.cache_clear()

    # Each read still retries once, and the warning names the missing API once.
    assert len(reads) == 4
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "get_terminal_map.cache_clear" in warnings[0].getMessage()
