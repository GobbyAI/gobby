"""Tests for the tmux window-name repair maintenance loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.runner_maintenance import tmux_window_name_repair_loop

pytestmark = pytest.mark.unit


class _SessionManager:
    def __init__(self, sessions: list[SimpleNamespace]) -> None:
        self.sessions = sessions
        self.calls: list[tuple[list[str], int]] = []

    def list(self, *, statuses: list[str], limit: int) -> list[SimpleNamespace]:
        self.calls.append((statuses, limit))
        return self.sessions


class _BrokenSessionManager:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []

    def list(self, *, statuses: list[str], limit: int) -> list[SimpleNamespace]:
        self.calls.append((statuses, limit))
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_repair_loop_enforces_only_paned_sessions() -> None:
    """The sweep lists active/paused sessions and only enforces those with a pane."""
    paned = SimpleNamespace(terminal_context={"tmux_pane": "%1"}, ref="#1")
    no_pane = SimpleNamespace(terminal_context={"cwd": "/x"}, ref="#2")
    none_ctx = SimpleNamespace(terminal_context=None, ref="#3")
    session_manager = _SessionManager([paned, no_pane, none_ctx])

    enforce = AsyncMock(return_value=True)
    with patch("gobby.runner_maintenance.enforce_window_name_if_unmanaged", enforce):
        # is_shutdown_requested True -> startup repair runs once, then the loop exits.
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert session_manager.calls == [(["active", "paused"], 200)]
    enforce.assert_awaited_once_with(paned)


@pytest.mark.asyncio
async def test_repair_loop_uses_configured_session_list_limit() -> None:
    """The repair sweep honors the configured session list limit."""
    session_manager = _SessionManager([])

    await tmux_window_name_repair_loop(
        session_manager,
        lambda: True,
        session_list_limit=50,
    )

    assert session_manager.calls == [(["active", "paused"], 50)]


@pytest.mark.asyncio
async def test_repair_loop_normalizes_nonpositive_session_list_limit() -> None:
    """Nonpositive limits clamp to the smallest safe list bound."""
    session_manager = _SessionManager([])

    await tmux_window_name_repair_loop(
        session_manager,
        lambda: True,
        session_list_limit=0,
    )

    assert session_manager.calls == [(["active", "paused"], 1)]


@pytest.mark.asyncio
async def test_repair_loop_normalizes_nonpositive_interval_seconds() -> None:
    """Nonpositive intervals clamp so the repair loop cannot hot-loop."""
    session_manager = _SessionManager([])
    sleep_calls: list[float] = []
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    with patch("gobby.runner_maintenance.asyncio.sleep", sleep):
        await tmux_window_name_repair_loop(
            session_manager,
            is_shutdown_requested,
            interval_seconds=0,
        )

    assert sleep_calls == [1]


@pytest.mark.asyncio
async def test_repair_loop_handles_no_session_manager() -> None:
    """A missing session manager is a no-op, not a crash."""
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return True

    await tmux_window_name_repair_loop(None, is_shutdown_requested)

    assert shutdown_checks == 1


@pytest.mark.asyncio
async def test_repair_loop_survives_list_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A failing session list is logged and does not raise."""
    session_manager = _BrokenSessionManager()

    with caplog.at_level("WARNING", logger="gobby.runner_maintenance"):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert session_manager.calls == [(["active", "paused"], 200)]
    assert "tmux window repair: failed to list sessions: db down" in caplog.text
