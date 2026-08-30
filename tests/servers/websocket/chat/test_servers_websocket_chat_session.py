"""Tests for WebSocket ChatSessionMixin (lifecycle of chat sessions)."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEventType
from gobby.providers.capabilities.resolve import (
    CapabilityResolver,
    ReasoningResolution,
    ReasoningStatus,
)
from gobby.servers.websocket.chat._session import ChatSessionMixin
from gobby.servers.websocket.chat._session_binding import _resolve_web_chat_reasoning
from gobby.servers.websocket.chat._session_runtime import _resolve_git_branch
from gobby.servers.websocket.chat._streaming import ChatStreamingMixin
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from tests._timing import drain_asyncio_tasks, wait_forever

pytestmark = pytest.mark.unit


class DummyMixin(ChatStreamingMixin, ChatSessionMixin):
    def __init__(self) -> None:
        self.clients: dict = {}
        self._chat_sessions: dict = {}
        self._active_chat_tasks: dict = {}
        self._pending_modes: dict = {}
        self._pending_worktree_paths: dict = {}
        self._pending_agents: dict = {}
        self._pending_projects: dict = {}
        self._pending_providers: dict = {}
        self._session_create_locks: dict = {}
        self.session_manager: Any = None
        self.daemon_config: Any = None
        self.web_chat_runtime_manager: Any = None
        self.config_runtime: Any = None

    async def _fire_lifecycle(self, cid: str, event_type: str, data: object) -> None:
        pass


@pytest.fixture
def mixin() -> DummyMixin:
    return DummyMixin()


class TestResolveWebChatReasoning:
    def test_none_skips_resolution(self) -> None:
        with patch(
            "gobby.agents.reasoning._get_capability_resolver",
            side_effect=AssertionError("unset reasoning must not resolve defaults"),
        ):
            assert _resolve_web_chat_reasoning("codex", "gpt-5.6-luna", None) is None

    def test_auto_omits_effort(self) -> None:
        resolver = MagicMock(spec=CapabilityResolver)
        resolver.resolve_reasoning.return_value = ReasoningResolution(
            "auto",
            None,
            ReasoningStatus.VERIFIED,
            None,
        )
        with patch("gobby.agents.reasoning._get_capability_resolver", return_value=resolver):
            result = _resolve_web_chat_reasoning("codex", "gpt-5.6-luna", "auto")

        assert result is None
        resolver.resolve_reasoning.assert_called_once_with(
            "codex",
            "gpt-5.6-luna",
            "auto",
            transport_supports_effort=True,
        )

    def test_rejected_pin_fails_before_backend(self) -> None:
        resolver = MagicMock(spec=CapabilityResolver)
        resolver.resolve_reasoning.return_value = ReasoningResolution(
            "extreme",
            None,
            ReasoningStatus.REJECTED,
            "unsupported reasoning effort: extreme",
        )
        with (
            patch("gobby.agents.reasoning._get_capability_resolver", return_value=resolver),
            pytest.raises(ValueError, match="unsupported reasoning effort"),
        ):
            _resolve_web_chat_reasoning("codex", "gpt-5.6-luna", "extreme")


class TestResolveGitBranch:
    @pytest.mark.asyncio
    async def test_resolve_git_branch_none(self) -> None:
        branch, path = await _resolve_git_branch(None)
        assert branch is None
        assert path is None

    @pytest.mark.asyncio
    async def test_resolve_git_branch_success(self) -> None:
        async def mock_communicate():
            return b"main\n", b""

        proc = MagicMock()
        proc.communicate = mock_communicate

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            branch, path = await _resolve_git_branch("/test/path")
            assert branch == "main"
            assert path == "/test/path"

    @pytest.mark.asyncio
    async def test_resolve_git_branch_detached(self) -> None:
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
    async def test_resolve_git_branch_error(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=ValueError("git not found")):
            branch, path = await _resolve_git_branch("/test/path")
            assert branch is None
            assert path is None


class TestCancelActiveChat:
    @pytest.mark.asyncio
    async def test_cancel_active_chat_no_session(self, mixin: DummyMixin) -> None:
        result = await mixin._cancel_active_chat("conv-xyz")
        assert result is None
        assert "conv-xyz" not in mixin._active_chat_tasks

    @pytest.mark.asyncio
    async def test_cancel_active_chat_with_session(self, mixin: DummyMixin) -> None:
        session = AsyncMock()
        mixin._chat_sessions["conv-xyz"] = session

        task = asyncio.create_task(wait_forever())
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
    async def test_cancel_active_chat_skips_interrupt_without_live_task(
        self, mixin: DummyMixin
    ) -> None:
        session = AsyncMock()
        mixin._chat_sessions["conv-xyz"] = session
        mixin._cancel_tts = AsyncMock()

        await mixin._cancel_active_chat("conv-xyz")

        session.interrupt.assert_not_awaited()
        assert session.interrupt.await_count == 0
        assert session.interrupt.await_args is None
        session.drain_pending_response.assert_awaited_once()
        assert session.drain_pending_response.await_count == 1
        assert session.drain_pending_response.await_args is not None
        mixin._cancel_tts.assert_awaited_once_with("conv-xyz")
        assert mixin._cancel_tts.await_count == 1
        assert mixin._cancel_tts.await_args is not None


class TestConfigureChatSession:
    @pytest.mark.asyncio
    async def test_queues_surface_context_for_new_session(self, mixin: DummyMixin) -> None:
        await mixin.configure_chat_session(
            "conv-comms",
            chat_mode="normal",
            agent_name="comms-agent",
            project_id="project-1",
        )

        assert mixin._pending_modes["conv-comms"] == "normal"
        assert mixin._pending_agents["conv-comms"] == "comms-agent"
        assert mixin._pending_projects["conv-comms"] == "project-1"

    @pytest.mark.asyncio
    async def test_reuses_matching_existing_session(self, mixin: DummyMixin) -> None:
        session = MagicMock(
            chat_mode="normal",
            project_id="project-1",
            _pending_agent_name="comms-agent",
        )
        session.stop = AsyncMock()
        mixin._chat_sessions["conv-comms"] = session

        await mixin.configure_chat_session(
            "conv-comms",
            chat_mode="normal",
            agent_name="comms-agent",
            project_id="project-1",
        )

        session.stop.assert_not_awaited()
        assert mixin._chat_sessions["conv-comms"] is session

    @pytest.mark.asyncio
    async def test_restarts_mismatched_existing_session(self, mixin: DummyMixin) -> None:
        session = MagicMock(
            chat_mode="plan",
            project_id="project-old",
            _pending_agent_name="default",
        )
        session.stop = AsyncMock()
        mixin._chat_sessions["conv-comms"] = session

        await mixin.configure_chat_session(
            "conv-comms",
            chat_mode="normal",
            agent_name="comms-agent",
            project_id="project-new",
        )

        session.stop.assert_awaited_once()
        assert "conv-comms" not in mixin._chat_sessions
        assert mixin._pending_modes["conv-comms"] == "normal"
        assert mixin._pending_agents["conv-comms"] == "comms-agent"
        assert mixin._pending_projects["conv-comms"] == "project-new"

    @pytest.mark.asyncio
    async def test_rejects_unknown_mode(self, mixin: DummyMixin) -> None:
        with pytest.raises(ValueError, match="Unsupported chat mode"):
            await mixin.configure_chat_session(
                "conv-comms",
                chat_mode="auto",
                agent_name="comms-agent",
                project_id="project-1",
            )


class TestCreateChatSessionInner:
    @pytest.mark.asyncio
    async def test_create_chat_session_no_db(self, mixin: DummyMixin) -> None:
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
    @pytest.mark.parametrize(
        ("requested", "backend_effort", "session_effort"),
        [
            ("auto", None, "unset"),
            ("high", "high", "high"),
        ],
    )
    async def test_resolves_reasoning_before_runtime_backend(
        self,
        mixin: DummyMixin,
        requested: str,
        backend_effort: str | None,
        session_effort: str,
    ) -> None:
        mock_session = AsyncMock()
        mock_session.provider = "codex"
        mock_session.chat_mode = "code"
        mock_session.db_session_id = None
        mock_session.resume_session_id = None
        mock_session.project_path = None
        mock_session.project_id = None
        mock_session.system_prompt_override = None
        mock_session.model = "gpt-5.6-luna"
        mock_session.reasoning_effort = "unset"
        mixin.web_chat_runtime_manager = MagicMock()
        mixin.web_chat_runtime_manager.create_session.return_value = mock_session
        mixin._fire_lifecycle = AsyncMock()

        session = await mixin._create_chat_session_inner(
            "conv-auto",
            model="gpt-5.6-luna",
            provider="codex",
            reasoning_effort=requested,
        )

        assert session is mock_session
        assert session.reasoning_effort == session_effort
        assert mixin.web_chat_runtime_manager.create_session.call_args.kwargs == {
            "provider": "codex",
            "conversation_id": "conv-auto",
            "model": "gpt-5.6-luna",
            "reasoning_effort": backend_effort,
        }

    @pytest.mark.asyncio
    async def test_create_chat_session_registers_in_shared_registry(
        self, mixin: DummyMixin
    ) -> None:
        registry = WebChatSessionRegistry()
        mixin.web_chat_session_registry = registry
        mixin._chat_sessions = registry.sessions
        mixin._active_chat_tasks = registry.active_tasks

        with patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass:
            mock_session = AsyncMock()
            mock_session.chat_mode = "code"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            MockSessionClass.return_value = mock_session

            session = await mixin._create_chat_session_inner("conv-shared", model="opus")

            assert session == mock_session
            assert registry.find_session("conv-shared") == ("conv-shared", mock_session)

    @pytest.mark.asyncio
    async def test_create_chat_session_with_pending_websocket_broadcast(
        self, mixin: DummyMixin
    ) -> None:
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
            # provider must be a real string so the plan-pending payload can
            # serialize source + per-CLI options (#15637).
            mock_session.provider = "claude"
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
            await session._on_plan_ready("plan data", {"allowedPrompts": ["y"]}, "plan-tool")
            call_args_plan = mock_ws.send.call_args[0][0]
            assert "plan_pending_approval" in call_args_plan
            assert session._pending_plan_content == "plan data"
            assert session._pending_plan_allowed_prompts == ["y"]
            # The payload carries the CLI source and the uniform YOLO/Act accept
            # option set so the frontend renders the fixed approval buttons.
            plan_payload = json.loads(call_args_plan)
            assert plan_payload["source"] == "claude"
            option_ids = {opt["id"] for opt in plan_payload["options"]}
            assert option_ids == {"approve_yolo", "approve_act"}
            emphasis_by_id = {opt["id"]: opt["emphasis"] for opt in plan_payload["options"]}
            assert emphasis_by_id["approve_yolo"] == "primary"
            assert emphasis_by_id["approve_act"] == "accent"

    @pytest.mark.asyncio
    async def test_create_chat_session_auto_resume(self, mixin: DummyMixin) -> None:
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
            assert mock_session.chat_mode == "normal"
            mock_session.set_accumulated_output_tokens.assert_called_once_with(500)
            mixin.session_manager.update_model.assert_called_once_with("db-id-123", "sonnet")

    @pytest.mark.asyncio
    async def test_register_passes_session_type_web_chat(self, mixin: DummyMixin) -> None:
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
            assert mixin.session_manager.register.call_count == 1
            assert mixin.session_manager.register.call_args is not None
            call_kwargs = mixin.session_manager.register.call_args
            assert call_kwargs.kwargs.get("session_type") == "web_chat"

    @pytest.mark.asyncio
    async def test_create_chat_session_persists_selected_model(self, mixin: DummyMixin) -> None:
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
            assert mixin.session_manager.update_model.call_count == 1
            assert mixin.session_manager.update_model.call_args is not None

    @pytest.mark.asyncio
    async def test_create_chat_session_persists_runtime_metadata(self, mixin: DummyMixin) -> None:
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
            mock_session.sandbox_metadata = {
                "backend": "provider-native",
                "enforced": True,
                "policy_hash": "verified-hash",
            }
            MockSessionClass.return_value = mock_session

            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-meta"
            mock_db_sess.usage_output_tokens = 0
            mock_db_sess.chat_mode = "plan"
            mock_db_sess.approved_tools_json = None
            mock_db_sess.workspace_path = None
            mock_db_sess.workspace_generation = 0

            mixin.session_manager = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess

            await mixin._create_chat_session_inner("conv-meta", model="opus")

            mixin.session_manager.update.assert_called_once_with(
                "db-id-meta",
                external_id="sdk-session-123",
                transcript_path="/tmp/runtime-session.jsonl",
                sandbox_enabled=True,
                sandbox_policy_hash="verified-hash",
                terminal_context={"sandbox": mock_session.sandbox_metadata},
                workspace_path=".",
                workspace_generation=1,
            )
            assert mixin.session_manager.update.call_count == 1
            assert mixin.session_manager.update.call_args is not None
            mixin.session_manager.update_model.assert_called_once_with("db-id-meta", "opus")
            assert mixin.session_manager.update_model.call_count == 1
            assert mixin.session_manager.update_model.call_args is not None

    @pytest.mark.asyncio
    async def test_create_qwen_default_agent_defers_prompt_to_first_lifecycle(
        self, mixin: DummyMixin
    ) -> None:
        with (
            patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
            patch("gobby.workflows.agent_resolver.resolve_agent") as mock_resolve_agent,
        ):
            agent_body = MagicMock()
            agent_body.provider = "qwen"
            agent_body.prompt_for.return_value = "DEFAULT PERSONA PROMPT"
            mock_resolve_agent.return_value = agent_body

            mock_session = AsyncMock()
            mock_session.provider = "qwen"
            mock_session.chat_mode = "plan"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            mixin.web_chat_runtime_manager = MagicMock()
            mixin.web_chat_runtime_manager.create_session.return_value = mock_session

            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-qwen"
            mock_db_sess.seq_num = 42
            mock_db_sess.usage_output_tokens = 0
            mock_db_sess.chat_mode = "plan"
            mock_db_sess.approved_tools_json = None

            mixin.session_manager = MagicMock()
            mixin.session_manager.db = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess

            await mixin._create_chat_session_inner("conv-qwen", provider="qwen")

            assert mock_resolve_agent.call_args is not None
            assert mock_resolve_agent.call_args.args[0] == "default"
            mixin.web_chat_runtime_manager.create_session.assert_called_once_with(
                provider="qwen",
                conversation_id="conv-qwen",
                model=None,
                reasoning_effort=None,
            )
            assert mock_session.system_prompt_override is None
            agent_body.prompt_for.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_persona_uses_next_session_provider_and_project_context(
        self, mixin: DummyMixin
    ) -> None:
        mixin._pending_agents["conv-persona"] = "planner"
        mixin._pending_projects["conv-persona"] = "proj-queued"
        mixin._pending_providers["conv-persona"] = "qwen"

        with (
            patch("gobby.servers.websocket.chat._session.get_machine_id", return_value="mach1"),
            patch("gobby.workflows.agent_resolver.resolve_agent") as mock_resolve_agent,
            patch(
                "gobby.mcp_proxy.tools.apply_persona.build_session_persona_context",
                return_value=("## Role\nPlanner", None),
            ),
            patch(
                "gobby.mcp_proxy.tools.apply_persona.apply_persona_impl",
                new=AsyncMock(return_value={"success": True}),
            ) as mock_apply_persona,
        ):
            agent_body = MagicMock()
            agent_body.name = "planner"
            agent_body.supports_surface.return_value = True
            mock_resolve_agent.return_value = agent_body

            mock_session = AsyncMock()
            mock_session.provider = "qwen"
            mock_session.chat_mode = "plan"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            mixin.web_chat_runtime_manager = MagicMock()
            mixin.web_chat_runtime_manager.create_session.return_value = mock_session

            mock_db_sess = MagicMock()
            mock_db_sess.id = "db-id-persona"
            mock_db_sess.seq_num = 11
            mock_db_sess.usage_output_tokens = 0
            mock_db_sess.chat_mode = "plan"
            mock_db_sess.approved_tools_json = None

            mixin.session_manager = MagicMock()
            mixin.session_manager.db = MagicMock()
            mixin.session_manager.register.return_value = mock_db_sess
            mixin._fire_lifecycle = AsyncMock()

            await mixin._create_chat_session_inner("conv-persona")
            await drain_asyncio_tasks()

            assert mock_resolve_agent.call_args is not None
            assert mock_resolve_agent.call_args.kwargs["cli_source"] == "qwen"
            assert mock_resolve_agent.call_args.kwargs["project_id"] == "proj-queued"
            mixin.web_chat_runtime_manager.create_session.assert_called_once_with(
                provider="qwen",
                conversation_id="conv-persona",
                model=None,
                reasoning_effort=None,
            )
            assert mock_session.system_prompt_override == "## Role\nPlanner"
            mock_apply_persona.assert_awaited_once()
            mixin._fire_lifecycle.assert_awaited_once_with(
                "conv-persona",
                HookEventType.SESSION_START,
                {"skip_default_agent_activation": True},
            )

    @pytest.mark.asyncio
    async def test_existing_web_chat_source_wins_over_stale_message_provider(
        self, mixin: DummyMixin
    ) -> None:
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
        existing_db_sess.workspace_path = None
        existing_db_sess.workspace_generation = 0

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

        await mixin._create_chat_session_inner("conv-existing", provider="unknown")

        mixin.web_chat_runtime_manager.create_session.assert_called_once_with(
            provider="codex",
            conversation_id="conv-existing",
            model=None,
            reasoning_effort=None,
        )
        assert mixin.web_chat_runtime_manager.create_session.call_count == 1
        assert mixin.web_chat_runtime_manager.create_session.call_args is not None

    @pytest.mark.asyncio
    async def test_cleared_web_chat_reattaches_live_successor(self, mixin: DummyMixin) -> None:
        predecessor = MagicMock()
        predecessor.id = "pred-db"
        predecessor.seq_num = 88
        predecessor.session_type = "web_chat"
        predecessor.status = "expired"
        predecessor.source = "claude"
        predecessor.project_id = "project-old"
        predecessor.external_id = None
        predecessor.usage_output_tokens = 0
        predecessor.chat_mode = None
        predecessor.approved_tools_json = None
        predecessor.sandbox_policy_hash = None

        successor = MagicMock()
        successor.id = "succ-db"
        successor.seq_num = 89
        successor.session_type = "web_chat"
        successor.status = "active"
        successor.source = "codex"
        successor.project_id = "project-new"
        successor.external_id = None
        successor.usage_output_tokens = 0
        successor.chat_mode = "code"
        successor.approved_tools_json = None
        successor.sandbox_policy_hash = None

        mock_session = AsyncMock()
        mock_session.provider = "codex"
        mock_session.chat_mode = "plan"
        mock_session.db_session_id = None
        mock_session.resume_session_id = None
        mock_session.project_path = None
        mock_session.project_id = None
        mock_session.system_prompt_override = None
        mock_session.model = None
        mock_session.sandbox_metadata = {}

        mixin.web_chat_runtime_manager = MagicMock()
        mixin.web_chat_runtime_manager.create_session.return_value = mock_session
        mixin.session_manager = MagicMock()
        mixin.session_manager.db = MagicMock()
        mixin.session_manager.db.fetchone.side_effect = [
            {
                "status": "expired",
                "variables": {"clear_attempt": {"consumed_by": "succ-db"}},
            },
            {"status": "active", "variables": {}},
        ]
        mixin.session_manager.get.side_effect = [predecessor, successor]
        activated_predecessor = MagicMock()
        activated_predecessor.status = "active"
        mixin.session_manager.activate_web_chat_session.return_value = activated_predecessor

        session = await mixin._create_chat_session_inner("pred-db", provider="claude")

        assert session is mock_session
        assert session.db_session_id == "succ-db"
        assert session.seq_num == 89
        assert session.project_id == "project-new"
        assert session.chat_mode == "code"
        mixin.web_chat_runtime_manager.create_session.assert_called_once_with(
            provider="codex",
            conversation_id="pred-db",
            model=None,
            reasoning_effort=None,
        )
        mixin.session_manager.activate_web_chat_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_project_switch_starts_fresh_and_persists_project(
        self, mixin: DummyMixin
    ) -> None:
        existing_db_sess = MagicMock()
        existing_db_sess.id = "db-existing"
        existing_db_sess.seq_num = 88
        existing_db_sess.session_type = "web_chat"
        existing_db_sess.status = "active"
        existing_db_sess.source = "codex"
        existing_db_sess.project_id = "project-old"
        existing_db_sess.external_id = "codex-old"
        existing_db_sess.usage_output_tokens = 100
        existing_db_sess.chat_mode = "normal"
        existing_db_sess.approved_tools_json = None
        existing_db_sess.workspace_path = None
        existing_db_sess.workspace_generation = 0

        mock_session = AsyncMock()
        mock_session.provider = "codex"
        mock_session.chat_mode = "plan"
        mock_session.db_session_id = None
        mock_session.resume_session_id = None
        mock_session.project_path = None
        mock_session.project_id = None
        mock_session.system_prompt_override = None
        mock_session.model = None

        mixin.web_chat_runtime_manager = MagicMock()
        mixin.web_chat_runtime_manager.create_session.return_value = mock_session
        mixin.session_manager = MagicMock()
        mixin.session_manager.db = MagicMock()
        mixin.session_manager.get.return_value = existing_db_sess

        await mixin._create_chat_session_inner(
            "conv-existing",
            project_id="project-new",
            provider="codex",
        )

        assert mock_session.resume_session_id is None
        assert mock_session.project_id == "project-new"
        mixin.session_manager.update.assert_called_once_with(
            "db-existing",
            project_id="project-new",
            workspace_path=".",
            workspace_generation=1,
        )

    @pytest.mark.asyncio
    async def test_expired_web_chat_activates_after_successful_hydration(
        self, mixin: DummyMixin
    ) -> None:
        existing_db_sess = MagicMock()
        existing_db_sess.id = "db-expired"
        existing_db_sess.seq_num = 89
        existing_db_sess.session_type = "web_chat"
        existing_db_sess.status = "expired"
        existing_db_sess.source = "codex"
        existing_db_sess.project_id = "proj-1"
        existing_db_sess.external_id = None
        existing_db_sess.usage_output_tokens = 0
        existing_db_sess.chat_mode = None
        existing_db_sess.approved_tools_json = None

        activated_db_sess = MagicMock()
        activated_db_sess.status = "active"
        lifecycle_order: list[str] = []
        mock_session = AsyncMock()
        mock_session.provider = "codex"
        mock_session.chat_mode = "plan"
        mock_session.db_session_id = None
        mock_session.resume_session_id = None
        mock_session.project_path = None
        mock_session.project_id = None
        mock_session.system_prompt_override = None
        mock_session.start.side_effect = lambda **_kwargs: lifecycle_order.append("start")

        mixin.web_chat_runtime_manager = MagicMock()
        mixin.web_chat_runtime_manager.create_session.return_value = mock_session
        mixin.session_manager = MagicMock()
        mixin.session_manager.db = MagicMock()
        mixin.session_manager.get.return_value = existing_db_sess
        mixin.session_manager.activate_web_chat_session.side_effect = (
            lambda _session_id: lifecycle_order.append("activate") or activated_db_sess
        )

        session = await mixin._create_chat_session_inner("db-expired")

        assert session is mock_session
        mock_session.start.assert_awaited_once_with(model=None)
        mixin.session_manager.activate_web_chat_session.assert_called_once_with("db-expired")
        assert lifecycle_order == ["start", "activate"]

    @pytest.mark.asyncio
    async def test_resume_reuses_existing_terminal_session_row(self, mixin: DummyMixin) -> None:
        existing_terminal = MagicMock()
        existing_terminal.id = "term-row-id"
        existing_terminal.seq_num = 27
        existing_terminal.session_type = "terminal"
        existing_terminal.source = "claude"
        existing_terminal.project_id = "proj-1"
        existing_terminal.external_id = "sdk-session-123"
        existing_terminal.usage_output_tokens = 0
        existing_terminal.chat_mode = "accept_edits"
        existing_terminal.approved_tools_json = None
        existing_terminal.model = "claude-opus-4-6"

        normalized_session = MagicMock()
        normalized_session.id = "term-row-id"
        normalized_session.seq_num = 27
        normalized_session.session_type = "web_chat"
        normalized_session.source = "claude"
        normalized_session.project_id = "proj-1"
        normalized_session.external_id = "sdk-session-123"
        normalized_session.usage_output_tokens = 0
        normalized_session.chat_mode = "accept_edits"
        normalized_session.approved_tools_json = None
        normalized_session.model = "claude-opus-4-6"

        with patch("gobby.servers.websocket.chat._session.ChatSession") as MockSessionClass:
            mock_session = AsyncMock()
            mock_session.chat_mode = "plan"
            mock_session.db_session_id = None
            mock_session.resume_session_id = None
            mock_session.project_path = None
            mock_session.project_id = None
            mock_session.system_prompt_override = None
            mock_session.model = "claude-opus-4-6"
            MockSessionClass.return_value = mock_session

            mixin.session_manager = MagicMock()
            mixin.session_manager.db = MagicMock()
            mixin.session_manager.get.return_value = existing_terminal
            mixin.session_manager.continue_terminal_session_as_web_chat.return_value = (
                normalized_session
            )

            session = await mixin._create_chat_session_inner(
                "term-row-id",
                model="claude-opus-4-6",
                project_id="proj-1",
                resume_session_id="sdk-session-123",
                provider="claude",
            )

            assert session == mock_session
            assert mock_session.db_session_id == "term-row-id"
            assert mock_session.seq_num == 27
            assert mock_session.resume_session_id == "sdk-session-123"
            assert mock_session.chat_mode == "normal"
            mock_session.start.assert_awaited_once_with(model="claude-opus-4-6")
            mixin.session_manager.register.assert_not_called()
            update_args = mixin.session_manager.continue_terminal_session_as_web_chat.call_args
            assert update_args is not None
            assert update_args.args == ("term-row-id",)
            assert update_args.kwargs["source"] == "claude"
            assert update_args.kwargs["model"] == "claude-opus-4-6"
            assert update_args.kwargs["project_id"] == "proj-1"
            assert update_args.kwargs["sandbox_policy_hash"] == mock_session.sandbox_policy_hash
            assert isinstance(update_args.kwargs["sandbox_policy_hash"], str)
            assert update_args.kwargs["sandbox_policy_hash"]
            mixin.session_manager.update_model.assert_called_once_with(
                "term-row-id",
                "claude-opus-4-6",
            )

    @pytest.mark.asyncio
    async def test_create_chat_session_uses_machine_checkout(  # tdd-red window
        self,
        mixin: DummyMixin,
        temp_db: Any,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pathlib import Path

        from tests.fixtures.isolated_checkout import install_isolated_checkout_project

        isolated = install_isolated_checkout_project(
            temp_db, Path(tmp_path) / "repo", monkeypatch=monkeypatch
        )
        existing = MagicMock()
        existing.id = "db-checkout"
        existing.seq_num = 3
        existing.session_type = "web_chat"
        existing.source = "claude"
        existing.project_id = isolated.project.id
        existing.machine_id = isolated.machine_id
        existing.external_id = "ext-checkout"
        existing.usage_output_tokens = 0
        existing.chat_mode = "normal"
        existing.approved_tools_json = None
        existing.status = "active"
        existing.model = None
        existing.sandbox_policy_hash = None

        mock_session = AsyncMock()
        mock_session.chat_mode = "code"
        mock_session.db_session_id = None
        mock_session.resume_session_id = None
        mock_session.project_path = None
        mock_session.project_id = None
        mock_session.system_prompt_override = None
        mixin.web_chat_runtime_manager = MagicMock()
        mixin.web_chat_runtime_manager.create_session.return_value = mock_session
        mixin.web_chat_runtime_manager.sandbox_policy_hash = "policy"
        mixin.session_manager = MagicMock()
        mixin.session_manager.db = temp_db
        mixin.session_manager.get.return_value = existing
        setattr(mixin, "_fire_lifecycle", AsyncMock())

        session = await mixin._create_chat_session_inner(
            "conv-checkout",
            project_id=isolated.project.id,
        )

        assert session is mock_session
        assert session.project_path == isolated.root_path

    @pytest.mark.asyncio
    async def test_fire_session_end(self, mixin: DummyMixin) -> None:
        with patch.object(mixin, "_fire_lifecycle", new_callable=AsyncMock) as fire_lifecycle:
            await mixin._fire_session_end("conv-end")

        fire_lifecycle.assert_awaited_once_with("conv-end", HookEventType.SESSION_END, {})
        assert fire_lifecycle.await_count == 1
        assert fire_lifecycle.await_args is not None
