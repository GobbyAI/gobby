"""Regression tests for compact_self continuation readiness gating."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


class _TestRegistry(InternalToolRegistry):
    def get_tool(self, name: str) -> Callable[..., Any] | None:
        tool = self._tools.get(name)
        return tool.func if tool else None


_LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000021"


@pytest.mark.asyncio
@patch("gobby.sessions.machine_scope.get_machine_id", return_value=_LOCAL_MACHINE_ID)
async def test_terminal_compact_self_schedules_codex_marker_watcher(
    _get_machine_id: MagicMock,
) -> None:
    registry = _TestRegistry(name="test", description="test")
    session = MagicMock()
    session.id = "s1"
    session.session_type = "terminal"
    session.source = "codex"
    session.machine_id = _LOCAL_MACHINE_ID
    session.transcript_path = None
    session.summary_markdown = """## Current State

The compact handoff summary is ready, and the terminal session can proceed with compaction.

## Next Steps

Send the compact command and watch for the provider's completion marker.
"""
    session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}

    session_manager = MagicMock()
    session_manager.get.return_value = session
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref

    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None
    tmux = MagicMock()
    tmux.dispatch_keys = AsyncMock(return_value=True)
    tmux.snapshot_lines = AsyncMock(return_value="")
    db = MagicMock()

    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=agent_run_manager,
    ):
        register_terminal_tools(registry, session_manager, db)

    compact_self = registry.get_tool("compact_self")
    assert compact_self is not None
    cursor = MagicMock()
    cursor.saw_fresh_turn_aborted.return_value = True

    with (
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal.CodexRolloutCursor.at_eof",
            return_value=cursor,
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal.manager_for_terminal_context",
            return_value=tmux,
        ),
        patch("gobby.mcp_proxy.tools.sessions._terminal._CODEX_INTERRUPT_SETTLE_SECONDS", 0),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal.mark_compact_self_continuation_pending",
            return_value=True,
        ),
        patch(
            "gobby.mcp_proxy.tools.sessions._terminal."
            "schedule_codex_compact_self_continuation_readiness",
            return_value=True,
        ) as mock_schedule_readiness,
        session_context_for_test("s1"),
    ):
        result = await compact_self()

    assert result["compacted"] is True
    assert result["continuation_pending"] is True
    mock_schedule_readiness.assert_called_once_with(
        db,
        pending_session_id="s1",
        target_session=session,
        before_command="",
        attempt_id=ANY,
    )
