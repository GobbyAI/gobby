"""Spawn-agent health check tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionInfo
from gobby.config.tmux import TmuxConfig
from gobby.mcp_proxy.tools.spawn_agent._health import (
    _check_tmux_session_alive,
    _deferred_tmux_health_check,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_check_tmux_session_alive_uses_configured_manager() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(return_value=TmuxSessionInfo(name="sess", pane_pid=123))

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ) as manager_cls,
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
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

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ) as manager_cls,
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
        result = await _check_tmux_session_alive("sess", socket_name="gobby")

    assert result is False
    config = manager_cls.call_args.args[0]
    assert config.socket_name == "gobby"
    assert config.socket_path is None
    manager.is_available.assert_called_once_with()
    manager.get_session.assert_awaited_once_with("sess")


@pytest.mark.asyncio
async def test_check_tmux_session_alive_rejects_missing_pane_pid() -> None:
    manager = MagicMock()
    manager.is_available.return_value = True
    manager.get_session = AsyncMock(return_value=TmuxSessionInfo(name="sess", pane_pid=None))

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health.TmuxSessionManager",
            return_value=manager,
        ) as manager_cls,
        patch(
            "gobby.agents.tmux.get_configured_tmux_config",
            return_value=TmuxConfig(),
        ),
    ):
        result = await _check_tmux_session_alive("sess", socket_name="gobby")

    assert result is False
    config = manager_cls.call_args.args[0]
    assert config.socket_name == "gobby"
    assert config.socket_path is None
    manager.is_available.assert_called_once_with()
    manager.get_session.assert_awaited_once_with("sess")


@pytest.mark.asyncio
async def test_deferred_health_check_does_not_fail_terminal_run() -> None:
    runner = MagicMock()
    terminal_run = SimpleNamespace(status="success")
    runner.run_storage.get.return_value = terminal_run
    runner.run_storage.fail.side_effect = lambda *args, **kwargs: setattr(
        terminal_run, "status", "error"
    )

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await _deferred_tmux_health_check(
            runner,
            run_id="run-123",
            tmux_session_name="tmux-run",
            socket_name=None,
            socket_path=None,
            delay=0,
        )

    assert terminal_run.status == "success"
    runner.run_storage.fail.assert_not_called()
