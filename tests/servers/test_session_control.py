"""Tests for WebSocket session control handlers (SessionControlMixin).

Focuses on the terminal kill path in continue_in_chat.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import psutil
import pytest

from gobby.llm.context_windows import resolve_context_window
from gobby.servers.websocket.handlers.session_observe import _resolve_fallback_inject_context
from gobby.sessions.terminal_kill import kill_terminal_session

pytestmark = pytest.mark.unit


def _context_window(model: str, *, provider: str | None = None) -> int:
    value = resolve_context_window(model, provider=provider)
    assert value is not None
    return value


def test_handoff_and_auto_fallback_use_handoff_when_summary_missing() -> None:
    source_session = MagicMock()
    source_session.summary_markdown = "   "
    source_session.handoff_markdown = "## Handoff fallback"

    assert _resolve_fallback_inject_context(source_session, "summary") is None
    assert _resolve_fallback_inject_context(source_session, "handoff") == "## Handoff fallback"
    assert _resolve_fallback_inject_context(source_session, "auto") == "## Handoff fallback"


def test_summary_fallback_context_ignores_whitespace_summary_and_handoff() -> None:
    source_session = MagicMock()
    source_session.summary_markdown = "   "
    source_session.handoff_markdown = "\n\t "

    assert _resolve_fallback_inject_context(source_session, "summary") is None


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
    async def test_refuses_pid_fallback_when_tmux_fails(self) -> None:
        """A recorded pane with an unresolved tmux failure must block PID fallback."""
        ctx = {
            "tmux_pane": "%49",
            "parent_pid": "12345",
            "parent_create_time": 100.0,
            "parent_name": "codex",
        }

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"pane not found"))

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            patch("os.kill") as mock_kill,
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False
        mock_exec.assert_called_once()
        assert mock_exec.call_args.args[-3:] == ("kill-pane", "-t", "%49")
        mock_proc.communicate.assert_awaited_once()
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_pid_kill_only_when_no_tmux(self) -> None:
        """Should use PID kill directly when no tmux_pane available."""
        ctx = {
            "parent_pid": "9999",
            "parent_create_time": 100.0,
            "parent_name": "codex",
        }
        process = MagicMock()
        process.create_time.return_value = 100.0
        process.name.return_value = "codex"

        with (
            patch("gobby.sessions.terminal_kill.psutil.Process", return_value=process),
            patch("os.kill") as mock_kill,
        ):
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
        ctx = {
            "parent_pid": "12345",
            "parent_create_time": 100.0,
            "parent_name": "codex",
        }

        with patch(
            "gobby.sessions.terminal_kill.psutil.Process",
            side_effect=psutil.NoSuchProcess(12345),
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_tmux_not_installed(self) -> None:
        """A recorded pane must block PID fallback when tmux is unavailable."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        with (
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
            patch("os.kill") as mock_kill,
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_tmux_timeout(self) -> None:
        """A recorded pane must block PID fallback when tmux times out."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=TimeoutError,
            ),
            patch("os.kill") as mock_kill,
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_treats_missing_tmux_pane_as_already_cleaned_up(self) -> None:
        """Missing panes should count as success during resume cleanup."""
        ctx = {
            "tmux_pane": "%10",
            "parent_pid": "5678",
            "parent_create_time": 100.0,
            "parent_name": "codex",
        }

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
        assert mock_kill.call_count == 0
        assert not mock_kill.called

    @pytest.mark.asyncio
    async def test_tmux_failure_does_not_fall_back(self) -> None:
        """An unresolved tmux failure must return False without signaling the PID."""
        ctx = {"tmux_pane": "%10", "parent_pid": "5678"}

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            patch("os.kill") as mock_kill,
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False
        mock_exec.assert_called_once()
        assert mock_exec.call_args.args[-3:] == ("kill-pane", "-t", "%10")
        mock_proc.communicate.assert_awaited_once()
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("create_time", "name"),
        [(101.0, "codex"), (100.0, "python")],
    )
    async def test_refuses_pid_fallback_when_process_identity_changed(
        self,
        create_time: float,
        name: str,
    ) -> None:
        ctx = {
            "parent_pid": "9999",
            "parent_create_time": 100.0,
            "parent_name": "codex",
        }
        process = MagicMock()
        process.create_time.return_value = create_time
        process.name.return_value = name

        with (
            patch("gobby.sessions.terminal_kill.psutil.Process", return_value=process),
            patch("os.kill") as mock_kill,
        ):
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_pid_fallback_without_recorded_identity(self) -> None:
        ctx = {"parent_pid": "9999"}

        with patch("os.kill") as mock_kill:
            result = await kill_terminal_session(ctx, "test-session-id")

        assert result is False
        mock_kill.assert_not_called()


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
        host.completion_registry = None
        host.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
        return host

    @pytest.mark.asyncio
    async def test_continue_in_chat_rejects_missing_source_session(self) -> None:
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=None)

        host = self._make_host()
        host.session_manager = session_manager
        host._send_error = AsyncMock()
        host._create_chat_session = AsyncMock()

        await SessionControlMixin._handle_continue_in_chat(
            host,
            ws,
            {
                "source_session_id": "missing-source",
                "conversation_id": "new-conv",
            },
        )

        host._send_error.assert_awaited_once_with(
            ws,
            "Source session not found: missing-source",
            code="NOT_FOUND",
        )
        host._create_chat_session.assert_not_awaited()
        assert host._chat_sessions == {}
        assert host._active_chat_tasks == {}

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
        assert mock_kill.call_count == 1
        assert mock_kill.call_args is not None
        session_manager.update_status.assert_not_called()
        assert session_manager.update_status.call_count == 0
        assert not session_manager.update_status.called
        session_manager.update.assert_any_call(
            "source-uuid",
            source="claude",
            model="sonnet",
            chat_mode="accept_edits",
            session_type="web_chat",
            status="active",
            terminal_context={},
            project_id="proj-1",
            sandbox_enabled=False,
            sandbox_policy_hash=ANY,
        )
        assert session_manager.update.call_count >= 1
        assert session_manager.update.call_args is not None
        session_manager.update_parent_session_id.assert_not_called()
        assert session_manager.update_parent_session_id.call_count == 0
        assert not session_manager.update_parent_session_id.called

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
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue."
                "deliver_existing_terminal_run_in_scope",
                new=AsyncMock(return_value=True),
            ) as deliver_terminal_run,
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
        assert mock_kill_agent.call_count == 1
        assert mock_kill_agent.call_args is not None
        mock_kill_terminal.assert_not_called()
        assert mock_kill_terminal.call_count == 0
        assert not mock_kill_terminal.called
        assert deliver_terminal_run.await_args.kwargs["run_id"] == mock_run.id
        session_manager.update_parent_session_id.assert_not_called()
        assert session_manager.update_parent_session_id.call_count == 0
        assert not session_manager.update_parent_session_id.called

    @pytest.mark.asyncio
    async def test_continue_in_chat_defaults_to_source_provider_and_normalizes_target_row(
        self,
    ) -> None:
        """Continuation should preserve the source provider when the client omits it."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.external_id = None
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.title = "Manual Investigation"
        source_session.title_source = "manual"
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
                    "project_id": "client-proj",
                },
            )

        assert captured["provider"] == "codex"
        assert captured["project_id"] == "proj-1"
        session_manager.update.assert_any_call(
            "new-conv",
            source="codex",
            model=None,
            title="Manual Investigation",
            title_source="manual",
            chat_mode=None,
        )
        assert session_manager.update.call_count >= 1
        assert session_manager.update.call_args is not None
        session_manager.update_parent_session_id.assert_called_once_with(
            "new-db-id",
            "source-uuid",
        )
        assert session_manager.update_parent_session_id.call_count == 1
        assert session_manager.update_parent_session_id.call_args is not None

    @pytest.mark.asyncio
    async def test_continue_in_chat_stops_when_terminal_release_fails(self) -> None:
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.session_type = "terminal"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.terminal_context = {"parent_pid": "123"}

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None
        host._send_error = AsyncMock()
        host._create_chat_session = AsyncMock()

        with (
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
                return_value=None,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.kill_terminal_session",
                new_callable=AsyncMock,
                return_value=False,
            ),
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

        host._send_error.assert_awaited_once()
        assert "Failed to release source session" in host._send_error.await_args.args[1]
        host._create_chat_session.assert_not_awaited()
        assert host._chat_sessions == {}
        assert host._active_chat_tasks == {}

    @pytest.mark.asyncio
    async def test_continue_in_chat_refuses_without_local_checkout(self) -> None:
        from gobby.servers.websocket.chat._session_checkout import ChatCheckoutRequiredError
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.session_type = "terminal"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "proj-1"
        source_session.transcript_path = None
        source_session.source = "codex"
        source_session.terminal_context = {"parent_pid": "123"}

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)

        host = self._make_host()
        host.session_manager = session_manager
        host.agent_run_manager = None
        host._send_error = AsyncMock()
        host._create_chat_session = AsyncMock(side_effect=ChatCheckoutRequiredError("proj-1"))

        with (
            patch(
                "gobby.storage.agents.LocalAgentRunManager.get_by_session",
                return_value=None,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.kill_terminal_session",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe.check_resume_blocked",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "gobby.servers.websocket.handlers.session_observe_continue.asyncio.sleep",
                new=AsyncMock(),
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

        host._create_chat_session.assert_awaited_once()
        host._send_error.assert_awaited_once_with(
            ws, "No checkout for this project on this machine", code="checkout_required"
        )
        assert host._chat_sessions == {}
        assert host._active_chat_tasks == {}

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
            terminal_context={},
            project_id="proj-1",
            sandbox_enabled=False,
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
            terminal_context={},
            project_id="proj-1",
            sandbox_enabled=False,
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
        session_manager.get = MagicMock(
            side_effect=lambda session_id: source_session if session_id == "source-uuid" else None
        )
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
        source_session.handoff_markdown = "## Handoff fallback"
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
        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["conversation_id"] == "source-uuid"

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
        source_session.handoff_markdown = "## Handoff fallback"
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

        assert host._pending_inject_contexts["source-uuid"] == "## Handoff fallback"
        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["conversation_id"] == "source-uuid"

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
        source_session.handoff_markdown = "## Handoff fallback"
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
    async def test_continue_in_chat_has_no_hidden_context_when_no_summary_or_digest_exists(
        self,
    ) -> None:
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
        source_session.handoff_markdown = None
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
        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["conversation_id"] == "source-uuid"

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
        source_session.project_id = "project-1"
        source_session.seq_num = 42
        source_session.source = "claude"
        source_session.title = "Observed Session"
        source_session.status = "active"
        source_session.model = "sonnet"
        source_session.chat_mode = "accept_edits"
        source_session.git_branch = "main"
        source_session.context_window = 200000
        source_session.context_used_tokens = 53535
        source_session.context_usage_ratio = 0.2072
        source_session.context_usage_source = "codex_token_event"
        source_session.context_usage_confidence = "reported"
        source_session.last_prompt_input_tokens = 53535
        source_session.last_prompt_uncached_input_tokens = 1234
        source_session.last_prompt_cache_read_tokens = 50000
        source_session.last_prompt_cache_creation_tokens = 2301
        source_session.last_completion_output_tokens = 456
        source_session.session_type = "terminal"
        # Proxy attach requires real tmux liveness, so a bare MagicMock attribute
        # reports False. Model the live pane the way sibling attach tests do.
        source_session.terminal_context = {"tmux_pane": "%5"}
        source_session.workflow_name = "release-checks"
        source_session.agent_run_id = "run-auto-1"

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.db = MagicMock()

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {"user_id": "local-cli", "project_id": "project-1"}}
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
        assert response["context_used_tokens"] == 53535
        assert response["context_usage_ratio"] == 0.2072
        assert response["context_usage_source"] == "codex_token_event"
        assert response["context_usage_confidence"] == "reported"
        assert response["last_prompt_input_tokens"] == 53535
        assert response["last_prompt_uncached_input_tokens"] == 1234
        assert response["last_prompt_cache_read_tokens"] == 50000
        assert response["last_prompt_cache_creation_tokens"] == 2301
        assert response["last_completion_output_tokens"] == 456

    @pytest.mark.asyncio
    async def test_attach_to_session_hydrates_live_session_variables(self) -> None:
        """Attached status should reflect live variables when stored metadata is stale."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = set()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "project-1"
        source_session.seq_num = 42
        source_session.source = "codex"
        source_session.title = "Observed Session"
        source_session.status = "active"
        source_session.model = "old-model"
        source_session.reasoning_effort = "low"
        source_session.chat_mode = "plan"
        source_session.git_branch = "main"
        source_session.context_window = 128000
        source_session.session_type = "terminal"
        source_session.terminal_context = {"tmux_pane": "%8"}
        source_session.workflow_name = None
        source_session.agent_run_id = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.db = MagicMock()

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {"user_id": "local-cli", "project_id": "project-1"}}
        host._send_error = AsyncMock()

        with patch(
            "gobby.workflows.state_manager.SessionVariableManager.get_variables",
            return_value={
                "model_id": "gpt-5.4",
                "_effective_reasoning_effort": "high",
                "mode_level": 2,
                "model_context_window": 200000,
            },
        ):
            await SessionControlMixin._handle_attach_to_session(
                host,
                ws,
                {"session_id": "source-uuid"},
            )

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["model"] == "gpt-5.4"
        assert response["reasoning_effort"] == "high"
        assert response["chat_mode"] == "bypass"
        assert response["context_window"] == 200000

    @pytest.mark.asyncio
    async def test_attach_context_window_override(self) -> None:
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = set()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "project-1"
        source_session.seq_num = 42
        source_session.source = "codex"
        source_session.title = "Observed Session"
        source_session.status = "active"
        source_session.model = "future-model"
        source_session.reasoning_effort = "high"
        source_session.chat_mode = "plan"
        source_session.git_branch = "main"
        source_session.context_window = 200000
        source_session.session_type = "terminal"
        source_session.terminal_context = {"tmux_pane": "%8"}
        source_session.workflow_name = None
        source_session.agent_run_id = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.db = MagicMock()

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {"user_id": "local-cli", "project_id": "project-1"}}
        host._send_error = AsyncMock()
        host.daemon_config = MagicMock()
        host.daemon_config.context_window_overrides = {"future-model": 444_000}

        with patch(
            "gobby.workflows.state_manager.SessionVariableManager.get_variables",
            return_value={},
        ):
            await SessionControlMixin._handle_attach_to_session(
                host,
                ws,
                {"session_id": "source-uuid"},
            )

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["type"] == "attach_to_session_result"
        assert response["context_window"] == 444_000

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
        source_session.project_id = "project-1"
        source_session.seq_num = 43
        source_session.source = "agy"
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
        session_manager.db.fetchone.return_value = None

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {"user_id": "local-cli", "project_id": "project-1"}}
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

    @pytest.mark.parametrize(
        "client_metadata",
        [
            {},
            {"project_id": "project-1"},
        ],
    )
    async def test_attach_to_session_rejects_unauthenticated_connection(
        self,
        client_metadata: dict[str, str],
    ) -> None:
        """Attach must reject connections without an authenticated user_id."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = set()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "project-1"
        source_session.session_type = "terminal"

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: client_metadata}
        host._send_error = AsyncMock()

        await SessionControlMixin._handle_attach_to_session(
            host,
            ws,
            {"session_id": "source-uuid"},
        )

        host._send_error.assert_awaited_once_with(
            ws,
            "Not authorized to observe session",
            code="FORBIDDEN",
        )
        ws.send.assert_not_awaited()
        assert ws.subscriptions == set()
        assert "attached_session_id" not in client_metadata

    @pytest.mark.parametrize(
        "client_metadata",
        [
            # Phone / non-localhost repro (gobby-#20062): a fresh browser
            # origin connects, authenticates, and attaches straight from the
            # Sessions list without ever declaring a project scope.
            {"user_id": "local-cli"},
            # Stale scope: the client last declared a different project
            # (project switch in another tab, or a reconnect raced set_project).
            {"user_id": "local-cli", "project_id": "project-2"},
        ],
    )
    async def test_attach_to_session_allows_undeclared_or_stale_project_scope(
        self,
        client_metadata: dict[str, str],
    ) -> None:
        """Authenticated connections attach regardless of declared project scope."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = set()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.external_id = "cli-session-123"
        source_session.project_id = "project-1"
        source_session.seq_num = 42
        source_session.source = "claude"
        source_session.title = "Observed Session"
        source_session.status = "active"
        source_session.model = "claude-sonnet-5"
        source_session.chat_mode = "plan"
        source_session.git_branch = "main"
        source_session.context_window = 200000
        source_session.session_type = "terminal"
        source_session.terminal_context = {"tmux_pane": "%8"}
        source_session.workflow_name = None
        source_session.agent_run_id = None

        session_manager = MagicMock()
        session_manager.get = MagicMock(return_value=source_session)
        session_manager.db = MagicMock()
        session_manager.db.fetchone.return_value = None

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: client_metadata}
        host._send_error = AsyncMock()

        await SessionControlMixin._handle_attach_to_session(
            host,
            ws,
            {"session_id": "source-uuid"},
        )

        host._send_error.assert_not_awaited()
        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["type"] == "attach_to_session_result"
        assert client_metadata["attached_session_id"] == "source-uuid"
        assert ws.subscriptions

    async def test_set_project_updates_connection_scope(self) -> None:
        """A project switch should bind the registered client to that project."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        host = self._make_host()
        host.clients = {ws: {"user_id": "local-cli"}}
        host._chat_sessions = {}
        host._pending_projects = {}

        await SessionControlMixin._handle_set_project(
            host,
            ws,
            {"conversation_id": "conversation-1", "project_id": "project-1"},
        )

        assert host.clients[ws]["conversation_id"] == "conversation-1"
        assert host.clients[ws]["project_id"] == "project-1"

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
        assert host._send_error.await_count == 1
        assert host._send_error.await_args is not None
        ws.send.assert_not_awaited()
        assert ws.send.await_count == 0
        assert ws.send.await_args is None

    async def test_detach_from_session_cleans_attached_tts(self) -> None:
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.subscriptions = {"session:source-uuid", "other"}

        host = self._make_host()
        host.clients = {ws: {"attached_session_id": "source-uuid"}}
        host._cleanup_attached_tts = AsyncMock()

        await SessionControlMixin._handle_detach_from_session(
            host,
            ws,
            {"session_id": "source-uuid"},
        )

        host._cleanup_attached_tts.assert_awaited_once_with("source-uuid")
        assert host.clients[ws] == {}
        assert ws.subscriptions == {"other"}

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
        assert host._send_error.await_count == 1
        assert host._send_error.await_args is not None
        ws.send.assert_not_awaited()
        assert ws.send.await_count == 0
        assert ws.send.await_args is None

    @pytest.mark.asyncio
    async def test_send_to_cli_session_uses_recorded_tmux_socket(self) -> None:
        """CLI proxy send should target the tmux server recorded on the session."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.project_id = "proj-1"
        source_session.terminal_context = {
            "tmux_pane": "%7",
            "tmux_socket_path": "/tmp/tmux-1000/gobby",
        }
        source_session.metadata = None

        attached_session = MagicMock()
        attached_session.id = "web-123"
        session_manager = MagicMock()
        session_manager.get = MagicMock(
            side_effect=lambda session_id: attached_session
            if session_id == "web-123"
            else source_session
        )
        session_manager.db = MagicMock()

        inter_message = MagicMock()
        inter_message.id = "msg-1"
        inter_msg_manager = MagicMock()
        inter_msg_manager.create_message.return_value = inter_message

        tmux_manager = MagicMock()
        tmux_manager.dispatch_keys = AsyncMock(return_value=True)

        host = self._make_host()
        host.session_manager = session_manager
        host.clients = {ws: {"attached_session_id": "web-123"}}
        host._send_error = AsyncMock()
        host.inter_session_msg_manager = inter_msg_manager

        with (
            patch(
                "gobby.storage.inter_session_messages.InterSessionMessageManager",
            ) as manager_class,
            patch(
                "gobby.servers.websocket.handlers.session_observe.manager_for_terminal_context",
                return_value=tmux_manager,
            ) as mock_get_tmux_manager,
        ):
            await SessionControlMixin._handle_send_to_cli_session(
                host,
                ws,
                {"session_id": "source-uuid", "content": "hello"},
            )

        mock_get_tmux_manager.assert_called_once_with(source_session.terminal_context)
        manager_class.assert_not_called()
        tmux_manager.dispatch_keys.assert_awaited_once_with("%7", "hello\n")
        assert inter_msg_manager.create_message.call_args.kwargs["from_session"] == "web-123"
        inter_msg_manager.mark_delivered.assert_called_once_with("msg-1", "source-uuid")
        host._send_error.assert_not_awaited()

        payload = ws.send.await_args_list[0].args[0]
        response = json.loads(payload)
        assert response["type"] == "send_to_cli_session_result"
        assert response["session_id"] == "source-uuid"
        assert response["delivered"] is True
        assert response["delivery_method"] == "tmux"
        assert response["message_id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_send_to_cli_session_stores_attachments_and_appends_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Attached proxy uploads should be persisted and relayed as local paths."""
        from gobby.servers.websocket.session_control import SessionControlMixin

        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        ws = MagicMock()
        ws.send = AsyncMock()

        source_session = MagicMock()
        source_session.id = "source-uuid"
        source_session.session_type = "terminal"
        source_session.project_id = "proj-1"
        source_session.terminal_context = {"tmux_pane": "%7"}
        source_session.metadata = None

        attached_session = MagicMock()
        attached_session.id = "web-123"
        session_manager = MagicMock()
        session_manager.get = MagicMock(
            side_effect=lambda session_id: attached_session
            if session_id == "web-123"
            else source_session
        )
        session_manager.db = MagicMock()

        inter_message = MagicMock()
        inter_message.id = "msg-1"
        inter_msg_manager = MagicMock()
        inter_msg_manager.create_message.return_value = inter_message

        tmux_manager = MagicMock()
        tmux_manager.dispatch_keys = AsyncMock(return_value=True)

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
                "gobby.servers.websocket.handlers.session_observe.manager_for_terminal_context",
                return_value=tmux_manager,
            ),
        ):
            await SessionControlMixin._handle_send_to_cli_session(
                host,
                ws,
                {
                    "session_id": "source-uuid",
                    "content": "please inspect",
                    "attachments": [
                        {
                            "name": "../note.txt",
                            "mime_type": "text/plain",
                            "size": 5,
                            "base64": "aGVsbG8=",
                        }
                    ],
                },
            )

        delivered_content = tmux_manager.dispatch_keys.await_args.args[1]
        assert delivered_content.startswith("please inspect\n\nAttachments:\n")
        attached_path = delivered_content.removesuffix("\n").splitlines()[-1]
        assert attached_path.endswith("_note.txt")
        assert (tmp_path / "attachments" / "attached-sessions" / "source-uuid").is_dir()
        assert inter_msg_manager.create_message.call_args.kwargs["from_session"] == "web-123"
        assert inter_msg_manager.create_message.call_args.kwargs["content"].endswith(attached_path)
        assert host._send_error.await_count == 0


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
