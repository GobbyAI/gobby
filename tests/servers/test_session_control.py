"""Tests for WebSocket session control handlers (SessionControlMixin).

Focuses on the terminal kill path in continue_in_chat.
"""

from __future__ import annotations

import asyncio
import json
import signal
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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
    async def test_kills_via_tmux_pane_on_recorded_socket(self) -> None:
        """Should target the recorded tmux socket path when killing a pane."""
        ctx = {
            "tmux_pane": "%49",
            "tmux_socket_path": "/tmp/tmux-1000/gobby",
            "parent_pid": "12345",
        }

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is True
        mock_exec.assert_called_once_with(
            "tmux",
            "-S",
            "/tmp/tmux-1000/gobby",
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
        host._pending_projects = {}
        host._pending_providers = {}
        host._pending_inject_contexts = {}
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
            reasoning_effort=None,
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
            terminal_context={},
            project_id="proj-1",
            sandbox_enabled=True,
            sandbox_policy_hash=ANY,
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
            reasoning_effort=None,
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
            mock_chat_session.reasoning_effort = reasoning_effort
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
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
        source_session.terminal_context = {"parent_pid": 29084, "tmux_pane": "%155"}

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
            mock_chat_session.reasoning_effort = reasoning_effort
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
                return_value=None,
            ),
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
            terminal_context={},
            project_id="proj-1",
            sandbox_enabled=True,
            sandbox_policy_hash=ANY,
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
    async def test_continue_in_chat_applies_requested_chat_mode_and_reasoning(self) -> None:
        """Requested mode/reasoning should override stale defaults during resume."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.external_id = None
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.title = "Terminal Session"
        source_session.chat_mode = "plan"
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
        mock_chat_session.chat_mode = "plan"
        mock_chat_session.reasoning_effort = None

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
            mock_chat_session.reasoning_effort = reasoning_effort
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
                return_value=None,
            ),
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv-id",
                    "chat_mode": "accept_edits",
                    "reasoning_effort": "high",
                },
            )

        assert captured["reasoning_effort"] == "high"
        session_manager.update.assert_called_once_with(
            "source-uuid",
            source="codex",
            model="gpt-5.4",
            chat_mode="accept_edits",
            session_type="web_chat",
            status="active",
            title="Terminal Session",
            terminal_context={},
            project_id="proj-1",
            sandbox_enabled=True,
            sandbox_policy_hash=ANY,
        )
        assert mock_chat_session.chat_mode == "accept_edits"
        assert mock_chat_session.reasoning_effort == "high"

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["chat_mode"] == "accept_edits"
        assert response["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_continue_in_chat_drops_sdk_resume_when_web_chat_policy_mismatches(self) -> None:
        """Web-chat continuations should fork instead of SDK-resuming across policy changes."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "web_chat"
        source_session.external_id = "sdk-session-123"
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "claude"
        source_session.title = "Old Web Chat"
        source_session.chat_mode = "plan"
        source_session.model = "sonnet"
        source_session.terminal_context = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(side_effect=lambda session_id: source_session if session_id == "source-uuid" else None)
        session_manager.update = MagicMock()
        session_manager.update_parent_session_id = MagicMock()

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "new-db-id"
        mock_chat_session.seq_num = 88

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None
        host._send_error = AsyncMock()
        host.web_chat_runtime_manager = MagicMock()
        host.web_chat_runtime_manager.policy_mismatch_reason.return_value = (
            "This chat was created under a different sandbox policy. Continue it in a new chat."
        )
        host.web_chat_runtime_manager.sandbox_config.enabled = True
        host.web_chat_runtime_manager.sandbox_policy_hash = "policy-new"

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
            captured["resume_session_id"] = resume_session_id
            captured["provider"] = provider
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

        assert captured["conversation_id"] == "new-conv-id"
        assert captured["resume_session_id"] is None
        session_manager.update_parent_session_id.assert_called_once_with("new-db-id", "source-uuid")

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["resumed"] is False
        assert response["resume_notice"] == (
            "This chat was created under a different sandbox policy. Continue it in a new chat."
        )

    @pytest.mark.asyncio
    async def test_continue_in_chat_queues_summary_fallback_context(self) -> None:
        """Auto fallback should prefer summary markdown when native resume is unavailable."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.external_id = None
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.title = "Terminal Session"
        source_session.chat_mode = "accept_edits"
        source_session.model = "gpt-5.4"
        source_session.summary_markdown = "## Summary fallback"
        source_session.digest_markdown = "## Digest fallback"
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

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
            reasoning_effort=None,
        ):
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
                return_value=None,
            ),
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv-id",
                    "fallback_context": "auto",
                },
            )

        assert host._pending_inject_contexts["source-uuid"] == "## Summary fallback"

    @pytest.mark.asyncio
    async def test_continue_in_chat_queues_digest_fallback_when_summary_missing(self) -> None:
        """Auto fallback should use digest markdown when summary markdown is absent."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.external_id = None
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.title = "Terminal Session"
        source_session.chat_mode = "accept_edits"
        source_session.model = "gpt-5.4"
        source_session.summary_markdown = None
        source_session.digest_markdown = "## Digest fallback"
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

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
            reasoning_effort=None,
        ):
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
                return_value=None,
            ),
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv-id",
                    "fallback_context": "auto",
                },
            )

        assert host._pending_inject_contexts["source-uuid"] == "## Digest fallback"

    @pytest.mark.asyncio
    async def test_continue_in_chat_prefers_native_resume_over_fallback_context(self) -> None:
        """Native resume should win even when summary or digest markdown exists."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "web_chat"
        source_session.external_id = "sdk-session-123"
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "claude"
        source_session.title = "Old Web Chat"
        source_session.chat_mode = "plan"
        source_session.model = "sonnet"
        source_session.summary_markdown = "## Summary fallback"
        source_session.digest_markdown = "## Digest fallback"
        source_session.terminal_context = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.update = MagicMock()
        session_manager.update_parent_session_id = MagicMock()

        mock_chat_session = MagicMock()
        mock_chat_session.db_session_id = "new-db-id"
        mock_chat_session.seq_num = 88

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None
        host._send_error = AsyncMock()
        host.web_chat_runtime_manager = MagicMock()
        host.web_chat_runtime_manager.policy_mismatch_reason.return_value = None
        host.web_chat_runtime_manager.sandbox_config.enabled = True
        host.web_chat_runtime_manager.sandbox_policy_hash = "policy-new"

        captured: dict[str, object] = {}

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
            reasoning_effort=None,
        ):
            captured["resume_session_id"] = resume_session_id
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
                    "fallback_context": "auto",
                },
            )

        assert captured["resume_session_id"] == "sdk-session-123"
        assert host._pending_inject_contexts == {}

    @pytest.mark.asyncio
    async def test_continue_in_chat_has_no_hidden_context_when_no_summary_or_digest_exists(self) -> None:
        """No-context fallback should leave the continuation without queued hidden context."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.external_id = None
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.title = "Terminal Session"
        source_session.chat_mode = "accept_edits"
        source_session.model = "gpt-5.4"
        source_session.summary_markdown = None
        source_session.digest_markdown = None
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

        async def fake_create_chat_session(
            conv_id,
            model=None,
            project_id=None,
            resume_session_id=None,
            provider=None,
            reasoning_effort=None,
        ):
            return mock_chat_session

        host._create_chat_session = fake_create_chat_session

        with (
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
                return_value=None,
            ),
        ):
            await SessionControlMixin._handle_continue_in_chat(
                host,
                ws,
                {
                    "source_session_id": "source-uuid",
                    "conversation_id": "new-conv-id",
                    "fallback_context": "auto",
                },
            )

        assert host._pending_inject_contexts == {}

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
        assert response["can_proxy_attach"] is True
        assert response["workflow_name"] == "release-checks"
        assert response["agent_run_id"] == "run-auto-1"
        assert response["agent_name"] == "code-reviewer"

    @pytest.mark.asyncio
    async def test_attach_to_session_keeps_live_handoff_tmux_proxy_attachable(self) -> None:
        """Live tmux metadata should keep resume-only terminal rows attachable."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = set()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.external_id = "cli-session-456"
        source_session.seq_num = 43
        source_session.source = "gemini"
        source_session.title = "Handoff Session"
        source_session.status = "handoff_ready"
        source_session.model = "gemini-2.5-pro"
        source_session.chat_mode = "plan"
        source_session.git_branch = "main"
        source_session.context_window = 1_000_000
        source_session.session_type = "terminal"
        source_session.terminal_context = {"tmux_pane": "%9"}
        source_session.workflow_name = None
        source_session.agent_run_id = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.db = MagicMock()

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {}}
        host._send_error = AsyncMock()

        await SessionControlMixin._handle_attach_to_session(
            host,
            ws,
            {"session_id": "source-uuid"},
        )

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["type"] == "attach_to_session_result"
        assert response["status"] == "handoff_ready"
        assert response["can_proxy_attach"] is True

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

    @pytest.mark.asyncio
    async def test_send_to_cli_session_uses_recorded_tmux_socket(self) -> None:
        """CLI proxy send should target the tmux server recorded on the session."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.terminal_context = {
            "tmux_pane": "%7",
            "tmux_socket_path": "/tmp/tmux-1000/gobby",
        }
        source_session.metadata = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.db = MagicMock()

        inter_message = MagicMock()
        inter_message.id = "msg-1"
        inter_msg_manager = MagicMock()
        inter_msg_manager.create_message.return_value = inter_message

        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {"attached_session_id": "web-123"}}
        host._send_error = AsyncMock()

        with (
            patch(
                "gobby.storage.inter_session_messages.InterSessionMessageManager",
                return_value=inter_msg_manager,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.get_tmux_manager_for_context",
                return_value=tmux_manager,
            ) as mock_get_tmux_manager,
        ):
            await SessionControlMixin._handle_send_to_cli_session(
                host,
                ws,
                {"session_id": "source-uuid", "content": "hello"},
            )

        mock_get_tmux_manager.assert_called_once_with(source_session.terminal_context)
        tmux_manager.send_keys.assert_awaited_once_with("%7", "hello\n")
        inter_msg_manager.mark_delivered.assert_called_once_with("msg-1")
        host._send_error.assert_not_awaited()

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["type"] == "send_to_cli_session_result"
        assert response["session_id"] == "source-uuid"
        assert response["delivered"] is True
        assert response["delivery_method"] == "tmux"
        assert response["message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_rebroadcast_pending_interactions_skips_missing_interaction_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.servers.websocket.session_control import SessionControlMixin

    websocket = MagicMock()
    websocket.send = AsyncMock()

    manager = MagicMock()
    manager.rebroadcast = AsyncMock(
        return_value=[
            {"kind": "tool", "tool_name": "Write", "arguments": {}},
            {"interaction_id": "int-1", "kind": "tool", "tool_name": "Write", "arguments": {}},
        ]
    )

    host = MagicMock()
    host._pending_interaction_manager = manager
    host._chat_sessions = {"conv-1": MagicMock(db_session_id="db-sess-1")}

    with caplog.at_level("WARNING"):
        await SessionControlMixin._rebroadcast_pending_interactions(host, websocket, ["conv-1"])

    websocket.send.assert_awaited_once()
    payload = json.loads(websocket.send.await_args.args[0])
    assert payload["tool_call_id"] == "int-1"
    assert "Skipping pending interaction missing interaction_id" in caplog.text
