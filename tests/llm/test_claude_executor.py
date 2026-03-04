"""Tests for ClaudeExecutor class.

Supports both subscription (CLI auth) and api_key (direct API key) modes.
Both use claude-agent-sdk — the SDK handles auth differences internally.
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.llm.executor import AgentResult, ToolResult, ToolSchema

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_anthropic_module():
    """Mock the anthropic module."""
    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic = MagicMock()

    with patch.dict(
        sys.modules,
        {
            "anthropic": mock_anthropic,
            "anthropic.types": mock_anthropic.types,
        },
    ):
        yield mock_anthropic


class TestClaudeExecutorInit:
    """Tests for ClaudeExecutor initialization."""

    def test_init_with_subscription_mode(self, mock_anthropic_module) -> None:
        """ClaudeExecutor initializes with subscription mode."""
        import shutil

        with patch.object(shutil, "which", return_value="/usr/bin/claude"):
            from gobby.llm.claude_executor import ClaudeExecutor

            executor = ClaudeExecutor(auth_mode="subscription")

            assert executor.auth_mode == "subscription"
            assert executor._cli_path == "/usr/bin/claude"

    def test_init_default_is_subscription_mode(self, mock_anthropic_module) -> None:
        """ClaudeExecutor defaults to subscription mode."""
        import shutil

        with patch.object(shutil, "which", return_value="/usr/bin/claude"):
            from gobby.llm.claude_executor import ClaudeExecutor

            executor = ClaudeExecutor()

            assert executor.auth_mode == "subscription"

    def test_init_with_custom_model(self, mock_anthropic_module) -> None:
        """ClaudeExecutor accepts custom default model."""
        import shutil

        with patch.object(shutil, "which", return_value="/usr/bin/claude"):
            from gobby.llm.claude_executor import ClaudeExecutor

            executor = ClaudeExecutor(
                auth_mode="subscription", default_model="claude-opus-4-5-20251101"
            )

            assert executor.default_model == "claude-opus-4-5-20251101"

    def test_init_subscription_mode_without_cli_raises(self, mock_anthropic_module) -> None:
        """ClaudeExecutor raises ValueError when CLI not found in subscription mode."""
        import shutil

        with patch.object(shutil, "which", return_value=None):
            from gobby.llm.claude_executor import ClaudeExecutor

            with pytest.raises(ValueError, match="Claude CLI not found"):
                ClaudeExecutor(auth_mode="subscription")

    def test_init_api_key_mode(self, mock_anthropic_module) -> None:
        """ClaudeExecutor initializes with api_key mode."""
        from gobby.llm.claude_executor import ClaudeExecutor

        executor = ClaudeExecutor(auth_mode="api_key", api_key="sk-ant-test")
        assert executor.auth_mode == "api_key"
        assert executor.api_key == "sk-ant-test"
        assert executor._cli_path == ""

    def test_init_api_key_mode_without_key_raises(self, mock_anthropic_module) -> None:
        """ClaudeExecutor raises ValueError for api_key mode without key."""
        from gobby.llm.claude_executor import ClaudeExecutor

        with pytest.raises(ValueError, match="api_key is required"):
            ClaudeExecutor(auth_mode="api_key")

    def test_init_unknown_auth_mode_raises(self, mock_anthropic_module) -> None:
        """ClaudeExecutor raises ValueError for unknown auth mode."""
        from gobby.llm.claude_executor import ClaudeExecutor

        with pytest.raises(ValueError, match="Unsupported auth_mode"):
            ClaudeExecutor(auth_mode="unknown")  # type: ignore[arg-type]


class TestClaudeExecutorSDKMode:
    """Tests for ClaudeExecutor subscription/SDK mode."""

    @pytest.fixture
    def simple_tools(self) -> list[ToolSchema]:
        """Create simple tool schemas for testing."""
        return [
            ToolSchema(
                name="get_weather",
                description="Get the current weather",
                input_schema={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"},
                    },
                    "required": ["location"],
                },
            ),
        ]

    @pytest.fixture
    def executor_sdk_mode(self, mock_anthropic_module):
        """Create a ClaudeExecutor instance in SDK mode."""
        import shutil

        with patch.object(shutil, "which", return_value="/usr/bin/claude"):
            from gobby.llm.claude_executor import ClaudeExecutor

            return ClaudeExecutor(auth_mode="subscription")

    def test_sdk_mode_uses_cli_path(self, mock_anthropic_module) -> None:
        """ClaudeExecutor in SDK mode stores CLI path."""
        import shutil

        with patch.object(shutil, "which", return_value="/usr/local/bin/claude"):
            from gobby.llm.claude_executor import ClaudeExecutor

            executor = ClaudeExecutor(auth_mode="subscription")

            assert executor._cli_path == "/usr/local/bin/claude"

    def test_sdk_mode_provider_name(self, executor_sdk_mode) -> None:
        """SDK mode executor returns correct provider name."""
        assert executor_sdk_mode.provider_name == "claude"

    @pytest.mark.asyncio
    async def test_run_with_sdk_mode_delegates_to_sdk(
        self, executor_sdk_mode, simple_tools
    ) -> None:
        """SDK mode run() delegates to _run_with_sdk method."""
        # Verify the executor is in subscription mode
        assert executor_sdk_mode.auth_mode == "subscription"

        async def dummy_handler(name: str, args: dict) -> ToolResult:
            return ToolResult(tool_name=name, success=True, result={"ok": True})

        # Mock _run_with_sdk to avoid needing the actual SDK
        with patch.object(
            executor_sdk_mode,
            "_run_with_sdk",
            new_callable=AsyncMock,
            return_value=AgentResult(
                output="SDK response",
                status="success",
                turns_used=1,
            ),
        ) as mock_sdk_run:
            result = await executor_sdk_mode.run(
                prompt="Hello",
                tools=simple_tools,
                tool_handler=dummy_handler,
            )

            # Verify _run_with_sdk was called
            mock_sdk_run.assert_called_once()
            assert result.status == "success"
            assert result.output == "SDK response"


class TestClaudeExecutorApiKeyMode:
    """Tests for ClaudeExecutor api_key mode."""

    @pytest.fixture
    def simple_tools(self) -> list[ToolSchema]:
        """Create simple tool schemas for testing."""
        return [
            ToolSchema(
                name="get_weather",
                description="Get the current weather",
                input_schema={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name"},
                    },
                    "required": ["location"],
                },
            ),
        ]

    def test_api_key_mode_no_cli_required(self, mock_anthropic_module) -> None:
        """api_key mode does not require Claude CLI."""
        from gobby.llm.claude_executor import ClaudeExecutor

        # No shutil.which mock needed — api_key mode skips CLI check
        executor = ClaudeExecutor(auth_mode="api_key", api_key="sk-ant-test")
        assert executor._cli_path == ""

    @pytest.mark.asyncio
    async def test_run_api_key_mode_delegates_to_sdk(self, mock_anthropic_module) -> None:
        """api_key mode run() delegates to _run_with_sdk."""
        from gobby.llm.claude_executor import ClaudeExecutor

        executor = ClaudeExecutor(auth_mode="api_key", api_key="sk-ant-test")

        async def dummy_handler(name: str, args: dict) -> ToolResult:
            return ToolResult(tool_name=name, success=True, result={"ok": True})

        with patch.object(
            executor,
            "_run_with_sdk",
            new_callable=AsyncMock,
            return_value=AgentResult(
                output="API key response",
                status="success",
                turns_used=1,
            ),
        ) as mock_sdk_run:
            result = await executor.run(
                prompt="Hello",
                tools=[
                    ToolSchema(
                        name="test",
                        description="test",
                        input_schema={"type": "object"},
                    )
                ],
                tool_handler=dummy_handler,
            )

            mock_sdk_run.assert_called_once()
            assert result.status == "success"
            assert result.output == "API key response"


class TestClaudeExecutorProviderName:
    """Tests for provider_name property."""

    def test_provider_name_subscription_mode(self, mock_anthropic_module) -> None:
        """Provider name is 'claude' in subscription mode."""
        import shutil

        with patch.object(shutil, "which", return_value="/usr/bin/claude"):
            from gobby.llm.claude_executor import ClaudeExecutor

            executor = ClaudeExecutor(auth_mode="subscription")
            assert executor.provider_name == "claude"

    def test_provider_name_api_key_mode(self, mock_anthropic_module) -> None:
        """Provider name is 'claude' in api_key mode."""
        from gobby.llm.claude_executor import ClaudeExecutor

        executor = ClaudeExecutor(auth_mode="api_key", api_key="sk-test")
        assert executor.provider_name == "claude"
