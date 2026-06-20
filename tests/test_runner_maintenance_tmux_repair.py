"""Tests for the tmux window-name repair maintenance loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from gobby.runner_maintenance import (
    _select_tmux_repair_sessions,
    _tmux_repair_candidate_score,
    _tmux_repair_pane_key,
    tmux_window_name_repair_loop,
)

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


def test_tmux_repair_pane_key_uses_socket_identity() -> None:
    session = SimpleNamespace(terminal_context={"tmux_pane": "%1", "tmux_socket_name": "sock"})
    assert _tmux_repair_pane_key(session) == ("sock", "%1")


def test_tmux_repair_candidate_score_prefers_identity_and_activity() -> None:
    inactive = SimpleNamespace(external_id="", message_count=0, turn_count=0, tool_call_count=0)
    active = SimpleNamespace(external_id="external", message_count=1)

    assert _tmux_repair_candidate_score(inactive) == (0, 0)
    assert _tmux_repair_candidate_score(active) == (1, 1)


def test_select_tmux_repair_sessions_keeps_best_candidate_per_pane() -> None:
    stale = SimpleNamespace(
        external_id="",
        terminal_context={"tmux_pane": "%1", "tmux_socket_path": "/tmp/tmux"},
        message_count=0,
    )
    best = SimpleNamespace(
        external_id="external",
        terminal_context={"tmux_pane": "%1", "tmux_socket_path": "/tmp/tmux"},
        message_count=1,
    )
    other = SimpleNamespace(
        external_id="other",
        terminal_context={"tmux_pane": "%2", "tmux_socket_path": "/tmp/tmux"},
        message_count=0,
    )

    assert _select_tmux_repair_sessions([stale, best, other]) == [best, other]


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
async def test_repair_loop_repairs_one_best_session_per_tmux_pane() -> None:
    """Duplicate records for one pane cannot fight over the tmux window title."""
    stale = SimpleNamespace(
        external_id="",
        terminal_context={"tmux_pane": "%72", "tmux_socket_path": "/tmp/tmux"},
        transcript_path=None,
        message_count=0,
        turn_count=0,
        tool_call_count=0,
        ref="#7460",
    )
    grok = SimpleNamespace(
        external_id="grok-session-123",
        terminal_context={"tmux_pane": "%72", "tmux_socket_path": "/tmp/tmux"},
        transcript_path="/tmp/grok.jsonl",
        message_count=1,
        turn_count=0,
        tool_call_count=0,
        ref="#7514",
    )
    other = SimpleNamespace(
        external_id="other-session",
        terminal_context={"tmux_pane": "%73", "tmux_socket_path": "/tmp/tmux"},
        transcript_path=None,
        message_count=0,
        turn_count=0,
        tool_call_count=0,
        ref="#7515",
    )
    session_manager = _SessionManager([stale, grok, other])

    title_repair = AsyncMock(return_value=None)
    enforce = AsyncMock(return_value=True)
    with (
        patch("gobby.runner_maintenance.repair_missing_session_title", title_repair),
        patch("gobby.runner_maintenance.enforce_window_name_if_unmanaged", enforce),
    ):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert session_manager.calls == [(["active", "paused"], 200)]
    assert title_repair.await_args_list == [
        call(session_manager, grok),
        call(session_manager, other),
    ]
    assert enforce.await_args_list == [call(grok), call(other)]


@pytest.mark.asyncio
async def test_repair_loop_scopes_missing_socket_to_effective_default() -> None:
    """Root and agent default tmux sockets are distinct, but agent depths share one socket."""
    root = SimpleNamespace(
        agent_depth=0,
        external_id="root-session",
        terminal_context={"tmux_pane": "%72"},
        transcript_path="/tmp/root.jsonl",
        message_count=1,
        turn_count=0,
        tool_call_count=0,
        ref="#7600",
    )
    shallow_agent = SimpleNamespace(
        agent_depth=1,
        external_id="",
        terminal_context={"tmux_pane": "%72"},
        transcript_path=None,
        message_count=0,
        turn_count=0,
        tool_call_count=0,
        ref="#7601",
    )
    nested_agent = SimpleNamespace(
        agent_depth=3,
        external_id="nested-agent-session",
        terminal_context={"tmux_pane": "%72"},
        transcript_path="/tmp/nested.jsonl",
        message_count=1,
        turn_count=0,
        tool_call_count=0,
        ref="#7602",
    )
    session_manager = _SessionManager([root, shallow_agent, nested_agent])

    title_repair = AsyncMock(return_value=None)
    enforce = AsyncMock(return_value=True)
    with (
        patch("gobby.runner_maintenance.repair_missing_session_title", title_repair),
        patch("gobby.runner_maintenance.enforce_window_name_if_unmanaged", enforce),
    ):
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    assert title_repair.await_args_list == [
        call(session_manager, root),
        call(session_manager, nested_agent),
    ]
    assert enforce.await_args_list == [call(root), call(nested_agent)]


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
