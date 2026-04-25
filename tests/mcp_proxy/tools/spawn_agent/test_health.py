"""Spawn-agent health check tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionInfo
from gobby.mcp_proxy.tools.spawn_agent._health import _check_tmux_session_alive

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_check_tmux_session_alive_uses_configured_manager() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(return_value=TmuxSessionInfo(name="sess", pane_pid=123))

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
        return_value=manager,
    ) as manager_cls:
        result = await _check_tmux_session_alive(
            "sess",
            socket_name="custom",
            socket_path="/tmp/tmux-1000/custom",
        )

    assert result is True
    config = manager_cls.call_args.args[0]
    assert config.socket_name == "custom"
    assert config.socket_path == "/tmp/tmux-1000/custom"
    manager.get_session.assert_awaited_once_with("sess")


@pytest.mark.asyncio
async def test_check_tmux_session_alive_rejects_dead_pane() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(
        return_value=TmuxSessionInfo(name="sess", pane_pid=123, pane_dead=True)
    )

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
        return_value=manager,
    ):
        result = await _check_tmux_session_alive("sess", socket_name="gobby")

    assert result is False


@pytest.mark.asyncio
async def test_check_tmux_session_alive_rejects_missing_pane_pid() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(return_value=TmuxSessionInfo(name="sess", pane_pid=None))

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
        return_value=manager,
    ):
        result = await _check_tmux_session_alive("sess", socket_name="gobby")

    assert result is False
