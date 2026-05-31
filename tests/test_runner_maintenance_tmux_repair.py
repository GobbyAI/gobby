"""Tests for the tmux window-name repair maintenance loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner_maintenance import tmux_window_name_repair_loop

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_repair_loop_enforces_only_paned_sessions() -> None:
    """The sweep lists active/paused sessions and only enforces those with a pane."""
    paned = MagicMock()
    paned.terminal_context = {"tmux_pane": "%1"}
    paned.ref = "#1"
    no_pane = MagicMock()
    no_pane.terminal_context = {"cwd": "/x"}
    no_pane.ref = "#2"
    none_ctx = MagicMock()
    none_ctx.terminal_context = None
    none_ctx.ref = "#3"

    session_manager = MagicMock()
    session_manager.list.return_value = [paned, no_pane, none_ctx]

    enforce = AsyncMock(return_value=True)
    with patch("gobby.workflows.summary_actions.enforce_window_name_if_unmanaged", enforce):
        # is_shutdown_requested True -> startup repair runs once, then the loop exits.
        await tmux_window_name_repair_loop(session_manager, lambda: True)

    session_manager.list.assert_called_once_with(statuses=["active", "paused"], limit=200)
    enforce.assert_awaited_once_with(paned)


@pytest.mark.asyncio
async def test_repair_loop_handles_no_session_manager() -> None:
    """A missing session manager is a no-op, not a crash."""
    await tmux_window_name_repair_loop(None, lambda: True)


@pytest.mark.asyncio
async def test_repair_loop_survives_list_failure() -> None:
    """A failing session list is logged and does not raise."""
    session_manager = MagicMock()
    session_manager.list.side_effect = RuntimeError("db down")

    await tmux_window_name_repair_loop(session_manager, lambda: True)
