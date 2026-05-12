"""Tests for ChatSession can_use_tool callback and pending question state."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.config.app import DaemonConfig
from gobby.config.features import ChatConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.servers.chat_session import ChatSession
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture
def session() -> ChatSession:
    return ChatSession(conversation_id="test-conv-123")


class TestCanUseTool:
    """Tests for the _can_use_tool callback integration."""

    @pytest.mark.asyncio
    async def test_start_uses_can_use_tool_not_bypass(self, session: ChatSession) -> None:
        """start() should pass can_use_tool callback, not permission_mode=bypassPermissions."""
        captured_options = {}

        def capture_options(**kwargs):
            captured_options.update(kwargs)
            return MagicMock()

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch(
                "gobby.servers.chat_session._build_gobby_mcp_entry",
                return_value={"command": "gobby", "args": ["mcp-server"]},
            ),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch(
                "gobby.servers.chat_session._load_chat_system_prompt", return_value="test prompt"
            ),
            patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            await session.start()

            # Should have permission_mode matching initial chat_mode (plan by default)
            assert captured_options.get("permission_mode") == "plan"
            # Should have can_use_tool callback
            assert captured_options.get("can_use_tool") is not None
            assert callable(captured_options["can_use_tool"])

    @pytest.mark.asyncio
    async def test_auto_approves_non_ask_user_question(self, session: ChatSession) -> None:
        """_can_use_tool should auto-approve tools that aren't AskUserQuestion."""
        from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

        result = await session._can_use_tool(
            "mcp__gobby__create_task",
            {"title": "test"},
            ToolPermissionContext(),
        )

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input == {"title": "test"}

    @pytest.mark.asyncio
    async def test_ask_user_question_blocks_until_answer(self, session: ChatSession) -> None:
        """_can_use_tool should block on AskUserQuestion until provide_answer() is called."""
        from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

        input_data = {"questions": [{"question": "Which auth?", "options": [{"label": "OAuth"}]}]}
        answers = {"Which auth?": "OAuth"}

        answer_task = asyncio.create_task(
            session._can_use_tool(
                "AskUserQuestion",
                input_data,
                ToolPermissionContext(),
            )
        )
        await wait_for_async_condition(
            lambda: session.has_pending_question,
            description="pending user question",
        )
        session.provide_answer(answers)
        result = await answer_task

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input is not None
        assert result.updated_input["questions"] == input_data["questions"]
        assert result.updated_input["answers"] == answers

    @pytest.mark.asyncio
    async def test_has_pending_question_true_while_blocked(self, session: ChatSession) -> None:
        """has_pending_question should be True while waiting for answer."""
        from claude_agent_sdk import ToolPermissionContext

        assert session.has_pending_question is False

        input_data = {"questions": [{"question": "Pick one"}]}

        answer_task = asyncio.create_task(
            session._can_use_tool("AskUserQuestion", input_data, ToolPermissionContext())
        )
        await wait_for_async_condition(
            lambda: session.has_pending_question,
            description="pending user question",
        )
        assert session.has_pending_question is True
        session.provide_answer({"Pick one": "A"})
        await answer_task

        assert session.has_pending_question is False


class TestDefaultModelResolution:
    """Tests for chat config default model resolution."""

    def test_prefers_chat_model_when_provider_matches(self) -> None:
        session = ChatSession(conversation_id="chat-default", provider="claude")
        session._config = DaemonConfig(
            chat=ChatConfig(provider="claude", model="sonnet"),
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="haiku,sonnet,opus"),
                default_model="opus",
            ),
        )

        assert session._default_model == "sonnet"

    def test_falls_back_to_global_default_when_chat_provider_mismatch(self) -> None:
        session = ChatSession(conversation_id="chat-default", provider="claude")
        session._config = DaemonConfig(
            chat=ChatConfig(provider="codex", model="gpt-5"),
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="haiku,sonnet,opus"),
                codex=LLMProviderConfig(models="gpt-5"),
                default_model="opus",
            ),
        )

        assert session._default_model == "opus"

    @pytest.mark.asyncio
    async def test_provide_answer_stores_answers(self, session: ChatSession) -> None:
        """provide_answer() should store answers and unblock the callback."""
        from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

        input_data = {"questions": [{"question": "Name?"}]}
        answers = {"Name?": "Alice"}

        task = asyncio.create_task(
            session._can_use_tool("AskUserQuestion", input_data, ToolPermissionContext())
        )
        await wait_for_async_condition(
            lambda: session.has_pending_question,
            description="pending user question",
        )
        session.provide_answer(answers)
        result = await task

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"] == answers


class TestMcpServerInjection:
    """Tests for gobby MCP server always being injected."""

    @pytest.mark.asyncio
    async def test_start_always_injects_gobby_mcp_server(self, session: ChatSession) -> None:
        """start() should always pass gobby MCP server in mcp_servers dict."""
        captured_options: dict[str, Any] = {}

        def capture_options(**kwargs: Any) -> MagicMock:
            captured_options.update(kwargs)
            return MagicMock()

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch(
                "gobby.servers.chat_session._build_gobby_mcp_entry",
                return_value={"command": "/usr/local/bin/gobby", "args": ["mcp-server"]},
            ),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch(
                "gobby.servers.chat_session._load_chat_system_prompt", return_value="test prompt"
            ),
            patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            await session.start()

            mcp = captured_options["mcp_servers"]
            assert isinstance(mcp, dict)
            assert "gobby" in mcp
            assert mcp["gobby"]["command"] == "/usr/local/bin/gobby"
            assert mcp["gobby"]["args"] == ["mcp-server"]


class TestToolApproval:
    """Tests for the tool approval flow."""

    @pytest.mark.asyncio
    async def test_tool_rejection_returns_deny(self, session: ChatSession) -> None:
        """_wait_for_tool_approval should return PermissionResultDeny on rejection."""
        from claude_agent_sdk import PermissionResultDeny

        # Enable tool approval for all tools
        config = MagicMock()
        config.enabled = True
        config.default_policy = "ask"
        config.policies = []
        session._tool_approval_config = config

        task = asyncio.create_task(
            session._wait_for_tool_approval("Write", {"file_path": "/tmp/test"})
        )
        await wait_for_async_condition(
            lambda: session.has_pending_approval, description="pending approval"
        )
        session.provide_approval("reject")
        result = await task

        assert isinstance(result, PermissionResultDeny)
        assert "Write" in result.message

    @pytest.mark.asyncio
    async def test_tool_approval_returns_allow(self, session: ChatSession) -> None:
        """_wait_for_tool_approval should return PermissionResultAllow on approval."""
        from claude_agent_sdk import PermissionResultAllow

        task = asyncio.create_task(
            session._wait_for_tool_approval("Write", {"file_path": "/tmp/test"})
        )
        await wait_for_async_condition(
            lambda: session.has_pending_approval, description="pending approval"
        )
        session.provide_approval("approve")
        result = await task

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input == {"file_path": "/tmp/test"}

    @pytest.mark.asyncio
    async def test_tool_timeout_returns_deny(self, session: ChatSession) -> None:
        """_wait_for_tool_approval should return PermissionResultDeny on timeout."""
        from claude_agent_sdk import PermissionResultDeny

        # Patch the timeout to be very short
        with patch("gobby.servers.chat_session.asyncio.wait_for", side_effect=TimeoutError):
            result = await session._wait_for_tool_approval("Write", {"file_path": "/tmp/test"})

        assert isinstance(result, PermissionResultDeny)
        assert "Write" in result.message


class TestProjectRouting:
    """Tests for project_id and project_path propagation in ChatSession.start()."""

    @pytest.mark.asyncio
    async def test_start_uses_project_path_as_cwd(self, session: ChatSession) -> None:
        """When project_path is set, start() uses it as CWD and sets GOBBY_PROJECT_ID in env."""
        session.project_id = "proj-abc"
        session.project_path = "/home/user/my-project"
        session.db_session_id = "db-session-1"

        captured_options: dict[str, Any] = {}

        def capture_options(**kwargs: Any) -> MagicMock:
            captured_options.update(kwargs)
            return MagicMock()

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch(
                "gobby.servers.chat_session._build_gobby_mcp_entry",
                return_value={"command": "gobby", "args": ["mcp-server"]},
            ),
            patch(
                "gobby.servers.chat_session._load_chat_system_prompt", return_value="test prompt"
            ),
            patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            await session.start()

            assert captured_options["cwd"] == "/home/user/my-project"
            assert captured_options["env"]["GOBBY_PROJECT_ID"] == "proj-abc"
            assert captured_options["env"]["GOBBY_SESSION_ID"] == "db-session-1"

    @pytest.mark.asyncio
    async def test_start_falls_back_without_project_path(self, session: ChatSession) -> None:
        """When project_path is not set, start() falls back to _find_project_root()."""
        session.project_id = None
        session.project_path = None

        captured_options: dict[str, Any] = {}

        def capture_options(**kwargs: Any) -> MagicMock:
            captured_options.update(kwargs)
            return MagicMock()

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch(
                "gobby.servers.chat_session._build_gobby_mcp_entry",
                return_value={"command": "gobby", "args": ["mcp-server"]},
            ),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch(
                "gobby.servers.chat_session._load_chat_system_prompt", return_value="test prompt"
            ),
            patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            expected_cwd = str(Path.cwd())

            await session.start()

            # Should not have GOBBY_PROJECT_ID in env
            env = captured_options.get("env", {})
            assert "GOBBY_PROJECT_ID" not in env
            # CWD should fall back to Path.cwd() since _find_project_root returns None
            assert captured_options["cwd"] == expected_cwd

    @pytest.mark.asyncio
    async def test_start_passes_reasoning_effort_to_claude_options(
        self, session: ChatSession
    ) -> None:
        """Claude sessions should forward supported effort levels to the SDK."""
        session.reasoning_effort = "high"

        captured_options: dict[str, Any] = {}

        def capture_options(**kwargs: Any) -> MagicMock:
            captured_options.update(kwargs)
            return MagicMock()

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch(
                "gobby.servers.chat_session._build_gobby_mcp_entry",
                return_value={"command": "gobby", "args": ["mcp-server"]},
            ),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch(
                "gobby.servers.chat_session._load_chat_system_prompt", return_value="test prompt"
            ),
            patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            await session.start(model="sonnet")

        assert captured_options["effort"] == "high"
        assert captured_options["model"] == "sonnet"

    @pytest.mark.asyncio
    async def test_start_materializes_sandbox_settings_file(
        self, session: ChatSession, tmp_path: Path
    ) -> None:
        """Sandboxed Claude web chat should point the SDK at a generated settings file."""
        session.sandbox_config = SandboxConfig(enabled=True)

        headless = tmp_path / "headless.json"
        headless.write_text('{"hooks":{"SessionStart":[]}}', encoding="utf-8")

        captured_options: dict[str, Any] = {}

        def capture_options(**kwargs: Any) -> MagicMock:
            captured_options.update(kwargs)
            return MagicMock()

        with (
            patch("gobby.servers.chat_session._find_cli_path", return_value="/usr/bin/claude"),
            patch(
                "gobby.servers.chat_session._build_gobby_mcp_entry",
                return_value={"command": "gobby", "args": ["mcp-server"]},
            ),
            patch("gobby.servers.chat_session._find_project_root", return_value=None),
            patch(
                "gobby.servers.chat_session._load_chat_system_prompt", return_value="test prompt"
            ),
            patch("gobby.servers.chat_session._HEADLESS_SETTINGS", headless),
            patch("gobby.servers.chat_session.ClaudeAgentOptions", side_effect=capture_options),
            patch("gobby.servers.chat_session.ClaudeSDKClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            await session.start()

        settings_path = captured_options.get("settings")
        assert isinstance(settings_path, str)
        payload = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        assert payload["sandbox"]["enabled"] is True
        assert payload["hooks"]["SessionStart"] == []


class TestHistoryInjection:
    """Tests for history injection on session recreation."""

    def test_needs_history_injection_default_false(self) -> None:
        """New sessions should not need history injection by default."""
        s = ChatSession(conversation_id="test-default")
        assert s._needs_history_injection is False

    @pytest.mark.asyncio
    async def test_load_history_context_no_manager(self, session: ChatSession) -> None:
        """_load_history_context returns None when no message_manager is set."""
        session.db_session_id = "some-id"
        session._message_manager = None
        result = await session._load_history_context()
        assert result is None

    @pytest.mark.asyncio
    async def test_load_history_context_no_db_session_id(self, session: ChatSession) -> None:
        """_load_history_context returns None when db_session_id is not set."""
        session._message_manager = AsyncMock()
        session.db_session_id = None
        result = await session._load_history_context()
        assert result is None

    @pytest.mark.asyncio
    async def test_load_history_context_formats_messages(self, session: ChatSession) -> None:
        """_load_history_context correctly formats user/assistant messages."""
        mock_manager = AsyncMock()
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": "Hello there"},
            {"role": "assistant", "content_type": "text", "content": "Hi! How can I help?"},
            {"role": "user", "content_type": "text", "content": "Tell me about Python"},
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is not None
        assert "<conversation-history>" in result
        assert "</conversation-history>" in result
        assert "**User:** Hello there" in result
        assert "**Assistant:** Hi! How can I help?" in result
        assert "**User:** Tell me about Python" in result

    @pytest.mark.asyncio
    async def test_load_history_context_truncates_long_messages(self, session: ChatSession) -> None:
        """Messages longer than 2000 chars should be truncated."""
        long_text = "x" * 3000
        mock_manager = AsyncMock()
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": long_text},
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is not None
        # 2000 chars + "..." = truncated
        assert "x" * 2000 + "..." in result
        assert "x" * 2001 not in result

    @pytest.mark.asyncio
    async def test_load_history_context_filters_non_text(self, session: ChatSession) -> None:
        """Tool use and tool result messages should be excluded."""
        mock_manager = AsyncMock()
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": "Do something"},
            {"role": "assistant", "content_type": "tool_use", "content": '{"name": "Read"}'},
            {"role": "user", "content_type": "tool_result", "content": "file contents"},
            {"role": "assistant", "content_type": "text", "content": "Done!"},
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is not None
        assert "Do something" in result
        assert "Done!" in result
        assert "Read" not in result
        assert "file contents" not in result

    @pytest.mark.asyncio
    async def test_load_history_context_empty_messages(self, session: ChatSession) -> None:
        """Returns None when no messages exist."""
        mock_manager = AsyncMock()
        mock_manager.get_messages.return_value = []
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is None

    @pytest.mark.asyncio
    async def test_load_history_context_respects_total_limit(self, session: ChatSession) -> None:
        """History should stop before exceeding 30KB total."""
        mock_manager = AsyncMock()
        # Each message is ~1100 chars, 30 messages = ~33KB > 30KB limit
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": "a" * 1100} for _ in range(30)
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is not None
        # Should have fewer than 30 entries due to 30KB cap
        count = result.count("**User:**")
        assert count < 30
        assert count > 0

    @pytest.mark.asyncio
    async def test_load_history_context_respects_custom_message_limit(
        self, session: ChatSession
    ) -> None:
        """Per-message truncation should respect _max_history_message_chars."""
        session._max_history_message_chars = 50
        mock_manager = AsyncMock()
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": "a" * 200},
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is not None
        assert "a" * 50 + "..." in result
        assert "a" * 51 not in result

    @pytest.mark.asyncio
    async def test_load_history_context_respects_custom_total_limit(
        self, session: ChatSession
    ) -> None:
        """History should stop before exceeding _max_history_total_chars."""
        session._max_history_total_chars = 500
        mock_manager = AsyncMock()
        # Each message is ~110 chars with role label, 10 messages = ~1100 > 500
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": "b" * 100} for _ in range(10)
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is not None
        count = result.count("**User:**")
        assert count < 10
        assert count > 0

    @pytest.mark.asyncio
    async def test_load_history_context_respects_budget_param(self, session: ChatSession) -> None:
        """max_total_chars parameter should override instance default."""
        mock_manager = AsyncMock()
        # Each entry is ~110 chars with role label, 10 messages = ~1100 chars content
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": "c" * 100} for _ in range(10)
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager
        # Instance default is 30K, but pass a tight budget
        result = await session._load_history_context(max_total_chars=500)
        assert result is not None
        count = result.count("**User:**")
        assert count < 10
        assert count > 0
        # Total result should be under the budget
        assert len(result) <= 500

    @pytest.mark.asyncio
    async def test_load_history_context_returns_none_for_tiny_budget(
        self, session: ChatSession
    ) -> None:
        """Returns None when budget is too small for even the wrapper."""
        mock_manager = AsyncMock()
        mock_manager.get_messages.return_value = [
            {"role": "user", "content_type": "text", "content": "hello"},
        ]
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager
        result = await session._load_history_context(max_total_chars=100)
        assert result is None

    @pytest.mark.asyncio
    async def test_load_history_context_handles_error(self, session: ChatSession) -> None:
        """Returns None on error instead of raising."""
        mock_manager = AsyncMock()
        mock_manager.get_messages.side_effect = RuntimeError("DB error")
        session.db_session_id = "test-db-id"
        session._message_manager = mock_manager

        result = await session._load_history_context()
        assert result is None
