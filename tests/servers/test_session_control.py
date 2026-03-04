"""Tests for WebSocket session control handlers (SessionControlMixin).

Focuses on the terminal kill path in continue_in_chat.
"""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.session_control import _kill_terminal_session

pytestmark = pytest.mark.unit


class TestKillTerminalSession:
    """Tests for the _kill_terminal_session helper."""

    @pytest.mark.asyncio
    async def test_kills_via_tmux_pane(self) -> None:
        """Should call tmux kill-pane and return True on success."""
        ctx = {"tmux_pane": "%49", "parent_pid": "12345"}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_exec.assert_called_once_with(
            "tmux",
            "kill-pane",
            "-t",
            "%49",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_pid_when_tmux_fails(self) -> None:
        """Should try PID kill when tmux kill-pane fails."""
        ctx = {"tmux_pane": "%49", "parent_pid": "12345"}

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"pane not found"))

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("os.kill") as mock_kill,
        ):
            result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_pid_kill_only_when_no_tmux(self) -> None:
        """Should use PID kill directly when no tmux_pane available."""
        ctx = {"parent_pid": "9999"}

        with patch("os.kill") as mock_kill:
            result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_called_once_with(9999, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_returns_false_when_no_context(self) -> None:
        """Should return False when neither tmux_pane nor parent_pid available."""
        ctx: dict[str, str] = {}

        result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_dead_pid_gracefully(self) -> None:
        """Should return False when PID is already dead and no tmux."""
        ctx = {"parent_pid": "12345"}

        with patch("os.kill", side_effect=ProcessLookupError):
            result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_tmux_not_installed(self) -> None:
        """Should fall back to PID when tmux is not installed."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        with (
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
            patch("os.kill") as mock_kill,
        ):
            result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_called_once_with(5678, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_handles_tmux_timeout(self) -> None:
        """Should fall back to PID when tmux command times out."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=TimeoutError,
            ),
            patch("os.kill") as mock_kill,
        ):
            result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_called_once_with(5678, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_both_methods_fail(self) -> None:
        """Should return False when both tmux and PID kill fail."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            result = await _kill_terminal_session(ctx, "test-session-id")

        assert result is False


class TestContinueInChatTerminalKill:
    """Tests for terminal kill integration in _handle_continue_in_chat."""

    def _make_host(self) -> MagicMock:
        """Create a minimal SessionControlMixin host."""
        host = MagicMock()
        host._chat_sessions = {}
        host._active_chat_tasks = {}
        host._pending_modes = {}
        host._pending_worktree_paths = {}
        host._pending_agents = {}
        return host

    @pytest.mark.asyncio
    async def test_kills_terminal_when_no_agent_registered(self) -> None:
        """When no agent is in the registry, should try terminal kill."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.external_id = "cli-session-123"
        source_session.project_id = "proj-1"
        source_session.terminal_context = {"tmux_pane": "%5", "parent_pid": "999"}

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.update_status = MagicMock()

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "new-db-id"

        # Build a host that looks enough like the mixin
        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None

        # Mock the agent registry to return nothing
        mock_registry = MagicMock()
        mock_registry.get_by_session.return_value = None

        async def fake_create_chat_session(conv_id, project_id=None, resume_session_id=None):
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session
        host._send_error = AsyncMock()

        with (
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            ),
            patch(
                "gobby.servers.websocket.session_control._kill_terminal_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_kill,
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv",
                },
            )

        # Verify terminal kill was attempted
        mock_kill.assert_called_once_with(
            {"tmux_pane": "%5", "parent_pid": "999"},
            "source-uuid",
        )
        # Verify session was expired
        session_manager.update_status.assert_called_once_with("source-uuid", "expired")

    @pytest.mark.asyncio
    async def test_skips_terminal_kill_when_agent_found(self) -> None:
        """When an agent is in the registry, should NOT try terminal kill."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.external_id = "cli-session-123"
        source_session.project_id = "proj-1"
        source_session.terminal_context = {"tmux_pane": "%5"}

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "new-db-id"

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None

        running_agent = MagicMock()
        running_agent.run_id = "agent-1"
        running_agent.mode = "terminal"
        mock_registry = MagicMock()
        mock_registry.get_by_session.return_value = running_agent
        mock_registry.kill = AsyncMock()

        async def fake_create_chat_session(conv_id, project_id=None, resume_session_id=None):
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session
        host._send_error = AsyncMock()

        with (
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            ),
            patch(
                "gobby.servers.websocket.session_control._kill_terminal_session",
                new_callable=AsyncMock,
            ) as mock_kill,
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv",
                },
            )

        # Agent kill should have been used instead
        mock_registry.kill.assert_called_once()
        mock_kill.assert_not_called()
