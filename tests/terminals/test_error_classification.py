"""Vanished-target classification across the exception chain."""

from __future__ import annotations

import pytest

from gobby.agents.tmux.text_injection import TmuxTargetUnavailableError
from gobby.terminals.error_classification import (
    _MAX_CHAIN_DEPTH,
    is_vanished_terminal_target,
    iter_exception_chain,
)
from gobby.terminals.host_client import HostUnavailableError
from gobby.terminals.runtime import TerminalWriteError, UnregisteredBackendError

pytestmark = pytest.mark.unit


def _tmux_target_gone(detail: str) -> TmuxTargetUnavailableError:
    """Build the error exactly as classify_tmux_text_injection_error builds it."""
    return TmuxTargetUnavailableError(
        f"tmux target is unavailable: {detail}",
        command=("tmux", "send-keys"),
        stderr=detail,
        returncode=1,
    )


def test_chained_write_error_is_recognized() -> None:
    """The exact production shape: the marker lives only in __cause__."""
    cause = _tmux_target_gone("can't find session: 2f0c6f1e-1d2b-4a1e-9d33-0f5b7c9a1e42")
    try:
        raise TerminalWriteError(stage="none") from cause
    except TerminalWriteError as error:
        # The wrapper's own message says nothing about why the write failed.
        assert "can't find session" not in str(error)
        assert is_vanished_terminal_target(error) is True


def test_native_host_unavailable_is_recognized() -> None:
    cause = HostUnavailableError("gterm host unavailable")
    try:
        raise TerminalWriteError(stage="none") from cause
    except TerminalWriteError as error:
        assert is_vanished_terminal_target(error) is True


def test_unregistered_backend_still_logs_loudly() -> None:
    """A misrouted write is a real defect, not a race, and must keep its traceback."""
    try:
        raise TerminalWriteError(stage="none") from UnregisteredBackendError("native")
    except TerminalWriteError as error:
        assert is_vanished_terminal_target(error) is False


def test_missing_tmux_executable_is_not_a_vanished_target() -> None:
    missing_executable = FileNotFoundError(2, "No such file or directory", "tmux")
    missing_socket = FileNotFoundError(
        2,
        "No such file or directory",
        "/tmp/tmux-501/gobby.sock",
    )

    assert is_vanished_terminal_target(missing_executable) is False
    assert is_vanished_terminal_target(missing_socket) is True


def test_missing_executable_does_not_mask_a_later_vanished_link() -> None:
    """The socket rule rejects one link; it must not abandon the rest of the chain."""
    cause = _tmux_target_gone("can't find pane: %7")
    error = FileNotFoundError(2, "No such file or directory", "tmux")
    error.__cause__ = cause

    assert is_vanished_terminal_target(error) is True


def test_timeout_anywhere_in_the_chain_is_a_vanished_target() -> None:
    error = TerminalWriteError(stage="none")
    error.__cause__ = TimeoutError("tmux invocation timed out")

    assert is_vanished_terminal_target(error) is True


def test_cycle_is_bounded() -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert list(iter_exception_chain(first)) == [first, second]
    assert is_vanished_terminal_target(first) is False


def test_depth_is_bounded() -> None:
    """A marker deeper than _MAX_CHAIN_DEPTH is out of reach by design."""
    marker_depth = 30
    links = [RuntimeError(f"link {index}") for index in range(40)]
    links[marker_depth] = RuntimeError("can't find session: deep")
    for shallower, deeper in zip(links, links[1:], strict=False):
        shallower.__cause__ = deeper

    assert marker_depth > _MAX_CHAIN_DEPTH
    assert len(list(iter_exception_chain(links[0]))) == _MAX_CHAIN_DEPTH
    assert is_vanished_terminal_target(links[0]) is False
    assert is_vanished_terminal_target(links[marker_depth]) is True


def test_context_is_followed_when_there_is_no_cause() -> None:
    """Bare `raise` inside an except block sets __context__, never __cause__."""
    error = TerminalWriteError(stage="none")
    error.__context__ = HostUnavailableError("gterm host unavailable")

    assert error.__cause__ is None
    assert is_vanished_terminal_target(error) is True
