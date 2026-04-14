"""Tests for WebSocket ChatSessionMixin (lifecycle of chat sessions)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEventType
from gobby.servers.websocket.chat._session import (
    ChatSessionMixin,
    _resolve_git_branch,
)

pytestmark = pytest.mark.unit


class DummyMixin(ChatSessionMixin):
    def __init__(self) -> None:
        self.clients: dict = {}
        self._chat_sessions: dict = {}
        self._active_chat_tasks: dict = {}
        self._pending_modes: dict = {}
        self._pending_worktree_paths: dict = {}
        self._pending_agents: dict = {}
        self._session_create_locks: dict = {}
        self.session_manager = None
        self.daemon_config = None
        self.web_chat_runtime_manager = None

    async def _fire_lifecycle(self, cid: str, event_type: str, data: object) -> None:
        pass


@pytest.fixture
def mixin() -> DummyMixin:
    return DummyMixin()


class TestResolveGitBranch:
    @pytest.mark.asyncio
    async def test_resolve_git_branch_none(self):
        branch, path = await _resolve_git_branch(None)
        assert branch is None
        assert path is None

    @pytest.mark.asyncio
    async def test_resolve_git_branch_success(self):
        async def mock_communicate():
            return b"main\n", b""

        proc = MagicMock()
        proc.communicate = mock_communicate

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            branch, path = await _resolve_git_branch("/test/path")
            assert branch == "main"
            assert path == "/test/path"

    @pytest.mark.asyncio
    async def test_resolve_git_branch_detached(self):
        # First call (branch --show-current) returns empty string (detached HEAD)
        async def mock_communicate_1():
            return b"\n", b""

        # Second call (rev-parse --short HEAD) returns sha
        async def mock_communicate_2():
            return b"a1b2c3d\n", b""

        # We need a side_effect to return different procs
        proc1 = MagicMock()
        proc1.communicate = mock_communicate_1
        proc2 = MagicMock()
        proc2.communicate = mock_communicate_2

        with patch("asyncio.create_subprocess_exec", side_effect=[proc1, proc2]):
            branch, path = await _resolve_git_branch("/test/path")
            assert branch == "detached:a1b2c3d"
            assert path == "/test/path"

    @pytest.mark.asyncio
    async def test_resolve_git_branch_error(self):
        with patch("asyncio.create_subprocess_exec", side_effect=ValueError("git not found")):
            branch, path = await _resolve_git_branch("/test/path")
            assert branch is None
            assert path is None


class TestCancelActiveChat:
    @pytest.mark.asyncio
    async def test_cancel_active_chat_no_session(self, mixin: DummyMixin):
        await mixin._cancel_active_chat("conv-xyz")
        # should pass silently

    @pytest.mark.asyncio
    async def test_cancel_active_chat_with_session(self, mixin: DummyMixin):
        session = AsyncMock()
        mixin._chat_sessions["conv-xyz"] = session

        task = asyncio.create_task(asyncio.sleep(10))
        mixin._active_chat_tasks["conv-xyz"] = task

        # Add TTS cancel mock to test that branch too
        mixin._cancel_tts = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mixin._cancel_active_chat("conv-xyz")

        # Await the task to ensure cancellation is fully observed
        try:
            await task
        except asyncio.CancelledError:
            pass

        session.interrupt.assert_awaited_once()
        assert task.cancelled()
        session.drain_pending_response.assert_awaited_once()
        mixin._cancel_tts.assert_awaited_once_with("conv-xyz")

    @pytest.mark.asyncio
    async def test_cancel_active_chat_skips_interrupt_without_live_task(self, mixin: DummyMixin):
        session = AsyncMock()
        mixin._chat_sessions["conv-xyz"] = session
        mixin._cancel_tts = AsyncMock()

        await mixin._cancel_active_chat("conv-xyz")

        session.interrupt.assert_not_awaited()
        session.drain_pending_response.assert_awaited_once()
        mixin._cancel_tts.assert_awaited_once_with("conv-xyz")


class TestCreateChatSessionInner:
    @pytest.mark.asyncio
    async def test_create_chat_session_no_db(self, mixin: DummyMixin):
        with patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass:
            mock_session = AsyncMock()
            # chat_mode must be a real string for JSON serialization in mode_changed broadcast
            mock_session.chat_mode = "code"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            MockSessionClass.return_value = mock_session

            # Fire lifecycle needs to be awaited inside the method so we mock it
            mixin._fire_lifecycle = AsyncMock()

            session = await mixin._create_chat_session_inner("conv-abc", model="opus")

            assert session == mock_session
            mock_session.start.assert_awaited_once_with(model="opus")

    @pytest.mark.asyncio
    async def test_create_chat_session_with_pending_websocket_broadcast(self, mixin: DummyMixin):
        """Test chat mode, plan ready, and mode change hooks are wired and behave as expected."""
        with patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass:
            mock_session = AsyncMock()
            # chat_mode must be a real string for JSON serialization in mode_changed broadcast
            mock_session.chat_mode = "code"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            MockSessionClass.return_value = mock_session

            # Add a mock websocket client to the mixin to test broadcast
            mock_ws = AsyncMock()
            mixin.clients[mock_ws] = {"conversation_id": "conv-1"}

            session = await mixin._create_chat_session_inner("conv-1")

            # Emulate the mode changed hook firing
            await session._on_mode_changed("accept_edits", "testing")
            mock_ws.send.assert_called()
            call_args = mock_ws.send.call_args[0][0]
            assert "mode_changed" in call_args
            assert "accept_edits" in call_args

            # Check that plan ready is broadcast
            await session._on_plan_ready("plan data", {"allowedPrompts": ["y"]})
            call_args_plan = mock_ws.send.call_args[0][0]
            assert "plan_pending_approval" in call_args_plan

    @pytest.mark.asyncio
    async def test_create_chat_session_auto_resume(self, mixin: DummyMixin):
        """Test that a DB session with prior usage automatically sets resume_session_id."""
        with (
            patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass,
            patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
        ):
            mock_session = AsyncMock()
            mock_session.model = "sonnet"
            MockSessionClass.return_value = mock_session

            # Mock DB
            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-123"
            mock_db_sess.usage_output_tokens = 500  # Will trigger auto-resume
            mock_db_sess.chat_mode = "accept_edits"

            mixin.session_manager = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess

            await mixin._create_chat_session_inner("conv-res", model="sonnet")

            assert mock_session.resume_session_id == "conv-res"
            assert mock_session.chat_mode == "accept_edits"
            assert mock_session._accumulated_output_tokens == 500
            mixin.session_manager.update_model.assert_called_once_with("db-id-123", "sonnet")

    @pytest.mark.asyncio
    async def test_register_passes_session_type_web_chat(self, mixin: DummyMixin):
        """Web chat sessions must register with session_type='web_chat'."""
        with (
            patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass,
            patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
        ):
            mock_session = AsyncMock()
            mock_session.chat_mode = "code"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            MockSessionClass.return_value = mock_session

            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-456"
            mock_db_sess.usage_output_tokens = 0
            mock_db_sess.chat_mode = "plan"

            mixin.session_manager = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess

            await mixin._create_chat_session_inner("conv-web")

            mixin.session_manager.register.assert_called_once()
            call_kwargs = mixin.session_manager.register.call_args
            assert call_kwargs.kwargs.get("session_type") == "web_chat"

    @pytest.mark.asyncio
    async def test_create_chat_session_persists_selected_model(self, mixin: DummyMixin):
        with (
            patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass,
            patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
        ):
            mock_session = AsyncMock()
            mock_session.chat_mode = "plan"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            mock_session.model = "opus"
            MockSessionClass.return_value = mock_session

            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-789"
            mock_db_sess.usage_output_tokens = 0
            mock_db_sess.chat_mode = "plan"

            mixin.session_manager = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess

            await mixin._create_chat_session_inner("conv-model", model="opus")

            mixin.session_manager.update_model.assert_called_once_with("db-id-789", "opus")

    @pytest.mark.asyncio
    async def test_create_chat_session_persists_runtime_metadata(self, mixin: DummyMixin):
        with (
            patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass,
            patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
        ):
            mock_session = AsyncMock()
            mock_session.chat_mode = "plan"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            mock_session.model = "opus"
            mock_session.sdk_session_id = "sdk-session-123"
            mock_session._transcript_path = "/tmp/runtime-session.jsonl"
            MockSessionClass.return_value = mock_session

            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-meta"
            mock_db_sess.usage_output_tokens = 0
            mock_db_sess.chat_mode = "plan"
            mock_db_sess.approved_tools_json = None

            mixin.session_manager = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess

            await mixin._create_chat_session_inner("conv-meta", model="opus")

            mixin.session_manager.update.assert_called_once_with(
                "db-id-meta",
                external_id="sdk-session-123",
                transcript_path="/tmp/runtime-session.jsonl",
            )
            mixin.session_manager.update_model.assert_called_once_with("db-id-meta", "opus")

    @pytest.mark.asyncio
    async def test_create_gemini_chat_session_uses_identity_only_prompt(self, mixin: DummyMixin):
        with (
            patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
            patch("gobby.workflows.agent_resolver.resolve_agent") as mock_resolve_agent,
            patch(
                "gobby.servers.websocket.chat._session._inject_agent_skills",
                return_value="## Skills\nCanvas",
            ) as mock_inject_skills,
        ):
            agent_body = MagicMock()
            agent_body.provider = "gemini"
            agent_body.role = "You are Gobby"
            agent_body.goal = "Fix the daemon"
            agent_body.personality = "Blunt and technical"
            agent_body.instructions = "Use tools"
            agent_body.build_prompt_preamble.return_value = (
                "## Role\nYou are Gobby\n\n## Instructions\nUse tools"
            )
            mock_resolve_agent.return_value = agent_body

            mock_session = AsyncMock()
            mock_session.provider = "gemini"
            mock_session.chat_mode = "plan"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            mixin.web_chat_runtime_manager = MagicMock()
            mixin.web_chat_runtime_manager.create_session.return_value = mock_session

            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-gemini"
            mock_db_sess.seq_num = 42
            mock_db_sess.usage_output_tokens = 0
            mock_db_sess.chat_mode = "plan"
            mock_db_sess.approved_tools_json = None

            mixin.session_manager = MagicMock()
            mixin.session_manager.db = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess

            await mixin._create_chat_session_inner("conv-gemini", provider="gemini")

            mixin.web_chat_runtime_manager.create_session.assert_called_once_with(
                provider="gemini",
                conversation_id="conv-gemini",
                model=None,
            )
            assert mock_session.system_prompt_override == (
                "## Role\nYou are Gobby\n\n"
                "## Goal\nFix the daemon\n\n"
                "## Personality\nBlunt and technical"
            )
            agent_body.build_prompt_preamble.assert_not_called()
            mock_inject_skills.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_web_chat_source_wins_over_stale_message_provider(
        self, mixin: DummyMixin
    ):
        existing_db_sess = MagicMock()
        existing_db_sess.id = "db-existing"
        existing_db_sess.seq_num = 88
        existing_db_sess.session_type = "web_chat"
        existing_db_sess.source = "codex"
        existing_db_sess.project_id = "proj-1"
        existing_db_sess.external_id = None
        existing_db_sess.usage_output_tokens = 0
        existing_db_sess.chat_mode = None
        existing_db_sess.approved_tools_json = None

        mock_session = AsyncMock()
        mock_session.provider = "codex"
        mock_session.chat_mode = "plan"
        mock_session.db_session_id = None
        mock_session.resume_session_id = None
        mock_session.project_path = None
        mock_session.project_id = None
        mock_session.system_prompt_override = None

        mixin.web_chat_runtime_manager = MagicMock()
        mixin.web_chat_runtime_manager.create_session.return_value = mock_session
        mixin.session_manager = MagicMock()
        mixin.session_manager.db = MagicMock()
        mixin.session_manager.get.return_value = existing_db_sess

        await mixin._create_chat_session_inner("conv-existing", provider="gemini")

        mixin.web_chat_runtime_manager.create_session.assert_called_once_with(
            provider="codex",
            conversation_id="conv-existing",
            model=None,
        )

    @pytest.mark.asyncio
    async def test_fire_session_end(self, mixin: DummyMixin):
        mixin._fire_lifecycle = AsyncMock()
        await mixin._fire_session_end("conv-end")
        mixin._fire_lifecycle.assert_awaited_once_with("conv-end", HookEventType.SESSION_END, {})
