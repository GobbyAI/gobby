"""Tests for WebSocket session control handlers (SessionControlMixin).

Focuses on the terminal kill path in continue_in_chat.
"""

from __future__ import annotations

import asyncio
import json
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.terminal_kill import kill_terminal_session

pytestmark = pytest.mark.unit


class TestKillTerminalSession:
    """Tests for the kill_terminal_session helper."""

    @pytest.mark.asyncio
    async def test_kills_via_tmux_pane(self) -> None:
        """Should call tmux kill-pane and return True on success."""
        ctx = {"tmux_pane": "%49", "parent_pid": "12345"}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await kill_terminal_session(ctx, "test-session-id")

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
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_pid_kill_only_when_no_tmux(self) -> None:
        """Should use PID kill directly when no tmux_pane available."""
        ctx = {"parent_pid": "9999"}

        with patch("os.kill") as mock_kill:
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_called_once_with(9999, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_returns_false_when_no_context(self) -> None:
        """Should return False when neither tmux_pane nor parent_pid available."""
        ctx: dict[str, str] = {}

        result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_dead_pid_gracefully(self) -> None:
        """Should return False when PID is already dead and no tmux."""
        ctx = {"parent_pid": "12345"}

        with patch("os.kill", side_effect=ProcessLookupError):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_tmux_not_installed(self) -> None:
        """Should fall back to PID when tmux is not installed."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        with (
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
            patch("os.kill") as mock_kill,
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

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
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_called_once_with(5678, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_treats_missing_tmux_pane_as_already_cleaned_up(self) -> None:
        """Missing panes should count as success during resume cleanup."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"can't find pane: %10"))

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("os.kill") as mock_kill,
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_kill.assert_not_called()

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
            result = await kill_terminal_session(ctx, "test-session-id")

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
        host._pending_providers = {}
        return host

    @pytest.mark.asyncio
    async def test_kills_terminal_when_no_agent_registered(self) -> None:
        """When no agent is in the registry, should try terminal kill."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.session_type = "terminal"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "claude"
        source_session.model = "sonnet"
        source_session.chat_mode = "accept_edits"
        source_session.title = "CLI Session"
        source_session.terminal_context = {"tmux_pane": "%5", "parent_pid": "999"}

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.update = MagicMock(return_value=source_session)
        session_manager.update_status = MagicMock()
        session_manager.update_parent_session_id = MagicMock()

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "source-uuid"
        mock_chat_session.seq_num = 42

        # Build a host that looks enough like the mixin
        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None

        # Mock the agent registry to return nothing
        mock_registry = MagicMock()
        mock_registry.get_by_session.return_value = None

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
        ):
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session
        host._send_error = AsyncMock()

        with (
            patch(
                "gobby.agents.registry.get_running_agent_registry",
                return_value=mock_registry,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.kill_terminal_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_kill,
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
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
        session_manager.update_status.assert_not_called()
        session_manager.update.assert_any_call(
            "source-uuid",
            source="claude",
            model="sonnet",
            chat_mode="accept_edits",
            session_type="web_chat",
            status="active",
            title="CLI Session",
            project_id="proj-1",
        )
        session_manager.update_parent_session_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_terminal_kill_when_agent_found(self) -> None:
        """When an agent run is in the DB, should use kill_agent instead of terminal kill."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.session_type = "terminal"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "claude"
        source_session.model = "sonnet"
        source_session.chat_mode = "accept_edits"
        source_session.title = "CLI Session"
        source_session.terminal_context = {"tmux_pane": "%5"}

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.update = MagicMock(return_value=source_session)
        session_manager.update_parent_session_id = MagicMock()

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "source-uuid"
        mock_chat_session.seq_num = 42

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None

        mock_run = MagicMock()
        mock_run.id = "agent-1"
        mock_run.mode = "interactive"

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
        ):
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session
        host._send_error = AsyncMock()

        with (
            patch(
                "gobby.storage.agents.LocalAgentRunManager",
            ) as mock_arm_cls,
            patch(
                "gobby.agents.kill.kill_agent",
                new_callable=AsyncMock,
            ) as mock_kill_agent,
            patch(
                "gobby.servers.websocket.handlers.session_observe.kill_terminal_session",
                new_callable=AsyncMock,
            ) as mock_kill_terminal,
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_arm_cls.return_value.get_by_session.return_value = mock_run
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv",
                },
            )

        # DB-driven kill_agent should have been used instead of terminal kill
        mock_kill_agent.assert_called_once()
        mock_kill_terminal.assert_not_called()
        session_manager.update_parent_session_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_continue_in_chat_defaults_to_source_provider_and_normalizes_target_row(self) -> None:
        """Continuation should preserve the source provider when the client omits it."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.external_id = None
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.terminal_context = None

        target_session = MagicMock()
        target_session.session_type = "web_chat"
        target_session.source = "claude"
        target_session.model = None

        session_manager = MagicMock()

        def get_session(session_id: str):
            if session_id == "source-uuid":
                return source_session
            if session_id == "new-conv":
                return target_session
            return None

        session_manager.get = MagicMock(side_effect=get_session)
        session_manager.update = MagicMock()
        session_manager.update_parent_session_id = MagicMock()

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "new-db-id"

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None
        host._send_error = AsyncMock()

        captured: dict[str, object] = {}

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
            reasoning_effort=None,
        ):
            captured["conversation_id"] = conv_id
            captured["provider"] = provider
            captured["model"] = model
            captured["project_id"] = project_id
            captured["resume_session_id"] = resume_session_id
            captured["reasoning_effort"] = reasoning_effort
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with patch(
            "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv",
                },
            )

        assert captured["provider"] == "codex"
        session_manager.update.assert_any_call(
            "new-conv",
            source="codex",
            model=None,
            title=None,
            chat_mode=None,
        )
        session_manager.update_parent_session_id.assert_called_once_with(
            "new-db-id",
            "source-uuid",
        )

    @pytest.mark.asyncio
    async def test_continue_in_chat_reuses_terminal_session_identity(self) -> None:
        """Terminal attach should convert the source session into the active web chat."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.title = "Terminal Session"
        source_session.chat_mode = "accept_edits"
        source_session.model = "gpt-5.4"
        source_session.terminal_context = None

        converted_session = MagicMock()
        converted_session.id = "source-uuid"
        converted_session.session_type = "web_chat"

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.update = MagicMock(return_value=converted_session)
        session_manager.update_parent_session_id = MagicMock()

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "source-uuid"
        mock_chat_session.seq_num = 88

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None
        host._send_error = AsyncMock()

        captured: dict[str, object] = {}

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
            reasoning_effort=None,
        ):
            captured["conversation_id"] = conv_id
            captured["provider"] = provider
            captured["model"] = model
            captured["project_id"] = project_id
            captured["resume_session_id"] = resume_session_id
            captured["reasoning_effort"] = reasoning_effort
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with patch(
            "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv-id",
                },
            )

        assert captured["conversation_id"] == "source-uuid"
        assert captured["provider"] == "codex"
        assert captured["model"] == "gpt-5.4"
        session_manager.update.assert_called_once_with(
            "source-uuid",
            source="codex",
            model="gpt-5.4",
            chat_mode="accept_edits",
            session_type="web_chat",
            status="active",
            title="Terminal Session",
            project_id="proj-1",
        )
        session_manager.update_parent_session_id.assert_not_called()

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["type"] == "session_continued"
        assert response["conversation_id"] == "source-uuid"
        assert response["db_session_id"] == "source-uuid"
        assert response["session_type"] == "web_chat"
        assert response["model"] == "gpt-5.4"

    @pytest.mark.asyncio
    async def test_attach_to_session_returns_extended_metadata(self) -> None:
        """Observed sessions should include session/agent metadata for the UI."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = set()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.external_id = "cli-session-123"
        source_session.seq_num = 42
        source_session.source = "claude"
        source_session.title = "Observed Session"
        source_session.status = "active"
        source_session.model = "sonnet"
        source_session.chat_mode = "accept_edits"
        source_session.git_branch = "main"
        source_session.context_window = 200000
        source_session.session_type = "terminal"
        source_session.workflow_name = "release-checks"
        source_session.agent_run_id = "run-auto-1"

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.db = MagicMock()

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {}}
        host._send_error = AsyncMock()

        mock_run = MagicMock()
        mock_run.agent_name = "code-reviewer"

        with patch("gobby.storage.agents.LocalAgentRunManager.get", return_value=mock_run):
            await SessionControlMixin._handle_attach_to_session(
                host,
                ws,
                {"session_id": "source-uuid"},
            )

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["type"] == "attach_to_session_result"
        assert response["session_type"] == "terminal"
        assert response["workflow_name"] == "release-checks"
        assert response["agent_run_id"] == "run-auto-1"
        assert response["agent_name"] == "code-reviewer"

    @pytest.mark.asyncio
    async def test_attach_to_session_rejects_web_chat_sessions(self) -> None:
        """Interactive attach should be limited to terminal sessions."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = set()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "web_chat"

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {}}
        host._send_error = AsyncMock()

        await SessionControlMixin._handle_attach_to_session(
            host,
            ws,
            {"session_id": "source-uuid"},
        )

        host._send_error.assert_awaited_once_with(
            ws,
            "attach_to_session only supports terminal sessions",
            code="UNSUPPORTED_SESSION_TYPE",
        )
        ws.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_to_cli_session_rejects_web_chat_sessions(self) -> None:
        """CLI proxy send should reject non-terminal targets."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "web_chat"

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {}}
        host._send_error = AsyncMock()

        await SessionControlMixin._handle_send_to_cli_session(
            host,
            ws,
            {"session_id": "source-uuid", "content": "hello"},
        )

        host._send_error.assert_awaited_once_with(
            ws,
            "send_to_cli_session only supports terminal sessions",
            code="UNSUPPORTED_SESSION_TYPE",
        )
        ws.send.assert_not_awaited()
