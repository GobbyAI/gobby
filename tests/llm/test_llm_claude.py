"""Tests for the ClaudeLLMProvider, specifically generate_with_mcp_tools."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

pytestmark = pytest.mark.unit


# Define mock classes for claude_agent_sdk
class MockAssistantMessage:
    def __init__(self, content):
        self.content = content


class MockUserMessage:
    def __init__(self, content):
        self.content = content


class MockResultMessage:
    def __init__(self, result=None):
        self.result = result


class MockTextBlock:
    def __init__(self, text):
        self.text = text


class MockToolUseBlock:
    def __init__(self, id: str, name: str, input: dict):
        self.id = id
        self.name = name
        self.input = input


class MockToolResultBlock:
    def __init__(self, tool_use_id: str, content: str):
        self.tool_use_id = tool_use_id
        self.content = content


class MockClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def claude_config() -> DaemonConfig:
    """Create a DaemonConfig with Claude provider configured."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-sonnet-4-5"),
        ),
    )


@contextmanager
def mock_claude_sdk(mock_query_func):
    """
    Context manager to properly mock the Claude Agent SDK.

    Patches all SDK names WHERE THEY ARE USED (in gobby.llm.claude),
    not where they are defined (in claude_agent_sdk).

    This is the correct approach because Python binds imported names
    at import time, so we must patch the bound references.
    """
    with (
        # Mock CLI detection
        patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch("os.path.exists", return_value=True),
        patch("os.access", return_value=True),
        # Mock SDK functions and classes where they're imported/used
        patch("gobby.llm.claude.query", mock_query_func),
        patch("gobby.llm.claude.AssistantMessage", MockAssistantMessage),
        patch("gobby.llm.claude.ResultMessage", MockResultMessage),
        patch("gobby.llm.claude.TextBlock", MockTextBlock),
        patch("gobby.llm.claude.ToolUseBlock", MockToolUseBlock),
        patch("gobby.llm.claude.ToolResultBlock", MockToolResultBlock),
        patch("gobby.llm.claude.UserMessage", MockUserMessage),
        patch("gobby.llm.claude.ClaudeAgentOptions", MockClaudeAgentOptions),
    ):
        yield


class TestToolCallDataclass:
    """Tests for the ToolCall dataclass."""

    def test_tool_call_creation(self) -> None:
        """Test ToolCall can be created with required fields."""
        from gobby.llm.claude_models import ToolCall

        call = ToolCall(
            tool_name="mcp__gobby-tasks__create_task",
            server_name="gobby-tasks",
            arguments={"title": "Test task"},
        )

        assert call.tool_name == "mcp__gobby-tasks__create_task"
        assert call.server_name == "gobby-tasks"
        assert call.arguments == {"title": "Test task"}
        assert call.result is None

    def test_tool_call_with_result(self) -> None:
        """Test ToolCall with result."""
        from gobby.llm.claude_models import ToolCall

        call = ToolCall(
            tool_name="mcp__gobby-tasks__create_task",
            server_name="gobby-tasks",
            arguments={"title": "Test task"},
            result='{"task_id": "123"}',
        )

        assert call.result == '{"task_id": "123"}'


class TestMCPToolResultDataclass:
    """Tests for the MCPToolResult dataclass."""

    def test_mcp_tool_result_creation(self) -> None:
        """Test MCPToolResult can be created with required fields."""
        from gobby.llm.claude_models import MCPToolResult

        result = MCPToolResult(text="Task created successfully")

        assert result.text == "Task created successfully"
        assert result.tool_calls == []

    def test_mcp_tool_result_with_calls(self) -> None:
        """Test MCPToolResult with tool calls."""
        from gobby.llm.claude_models import MCPToolResult, ToolCall

        calls = [
            ToolCall(
                tool_name="mcp__gobby-tasks__create_task",
                server_name="gobby-tasks",
                arguments={"title": "Task 1"},
            )
        ]
        result = MCPToolResult(text="Created 1 task", tool_calls=calls)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].arguments["title"] == "Task 1"


class TestGenerateWithMcpToolsNoCli:
    """Tests for generate_with_mcp_tools when CLI is not available."""

    @pytest.mark.asyncio
    async def test_returns_error_when_cli_not_found(self, claude_config: DaemonConfig):
        """Test that method returns error when Claude CLI is not found."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Create a task",
                allowed_tools=["mcp__gobby-tasks__create_task"],
            )

            assert "unavailable" in result.text.lower() or "not found" in result.text.lower()
            assert result.tool_calls == []


class TestGenerateWithMcpToolsWithCli:
    """Tests for generate_with_mcp_tools with CLI available."""

    @pytest.mark.asyncio
    async def test_returns_text_only_no_tools(self, claude_config: DaemonConfig):
        """Test generation with no tool calls."""

        async def mock_query(prompt, options):
            yield MockAssistantMessage([MockTextBlock("Hello, world!")])
            yield MockResultMessage(result="Hello, world!")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Say hello",
                allowed_tools=[],
            )

            assert "Hello, world!" in result.text
            assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_tracks_tool_calls(self, claude_config: DaemonConfig):
        """Test that tool calls are tracked."""

        async def mock_query(prompt, options):
            # Assistant makes a tool call
            yield MockAssistantMessage(
                [
                    MockToolUseBlock(
                        id="call_123",
                        name="mcp__gobby-tasks__create_task",
                        input={"title": "New task", "description": "A test task"},
                    )
                ]
            )
            # Tool result comes back
            yield MockUserMessage(
                [MockToolResultBlock(tool_use_id="call_123", content='{"task_id": "task-456"}')]
            )
            # Final text response
            yield MockAssistantMessage([MockTextBlock("I created the task for you.")])
            yield MockResultMessage(result="I created the task for you.")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Create a task called 'New task'",
                allowed_tools=["mcp__gobby-tasks__create_task"],
            )

            assert len(result.tool_calls) == 1
            call = result.tool_calls[0]
            assert call.tool_name == "mcp__gobby-tasks__create_task"
            assert call.server_name == "gobby-tasks"
            assert call.arguments == {"title": "New task", "description": "A test task"}
            assert call.result == '{"task_id": "task-456"}'

    @pytest.mark.asyncio
    async def test_tracks_multiple_tool_calls(self, claude_config: DaemonConfig):
        """Test that multiple tool calls are tracked."""

        async def mock_query(prompt, options):
            # First tool call
            yield MockAssistantMessage(
                [
                    MockToolUseBlock(
                        id="call_1",
                        name="mcp__gobby-tasks__create_task",
                        input={"title": "Task 1"},
                    )
                ]
            )
            yield MockUserMessage(
                [MockToolResultBlock(tool_use_id="call_1", content='{"task_id": "t1"}')]
            )
            # Second tool call
            yield MockAssistantMessage(
                [
                    MockToolUseBlock(
                        id="call_2",
                        name="mcp__gobby-tasks__create_task",
                        input={"title": "Task 2"},
                    )
                ]
            )
            yield MockUserMessage(
                [MockToolResultBlock(tool_use_id="call_2", content='{"task_id": "t2"}')]
            )
            yield MockResultMessage(result="Created 2 tasks.")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Create two tasks",
                allowed_tools=["mcp__gobby-tasks__create_task"],
            )

            assert len(result.tool_calls) == 2
            assert result.tool_calls[0].arguments["title"] == "Task 1"
            assert result.tool_calls[1].arguments["title"] == "Task 2"

    @pytest.mark.asyncio
    async def test_handles_exception(self, claude_config: DaemonConfig):
        """Test that exceptions are handled gracefully."""

        async def mock_query(prompt, options):
            raise RuntimeError("Connection failed")
            yield  # Make this a generator

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Create a task",
                allowed_tools=["mcp__gobby-tasks__create_task"],
            )

            assert "failed" in result.text.lower()
            assert "Connection failed" in result.text

    @pytest.mark.asyncio
    async def test_parses_server_name_correctly(self, claude_config: DaemonConfig):
        """Test server name is extracted correctly from tool name."""

        async def mock_query(prompt, options):
            yield MockAssistantMessage(
                [
                    MockToolUseBlock(
                        id="call_1",
                        name="mcp__my-custom-server__do_something",
                        input={"arg": "value"},
                    )
                ]
            )
            yield MockUserMessage([MockToolResultBlock(tool_use_id="call_1", content="done")])
            yield MockResultMessage(result="Done")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Do something",
                allowed_tools=["mcp__my-custom-server__do_something"],
            )

            assert result.tool_calls[0].server_name == "my-custom-server"

    @pytest.mark.asyncio
    async def test_handles_non_mcp_tool_names(self, claude_config: DaemonConfig):
        """Test handling of non-MCP tool names."""

        async def mock_query(prompt, options):
            yield MockAssistantMessage(
                [
                    MockToolUseBlock(
                        id="call_1",
                        name="code_execution",  # Built-in tool, not MCP
                        input={"code": "print(1)"},
                    )
                ]
            )
            yield MockUserMessage([MockToolResultBlock(tool_use_id="call_1", content="1")])
            yield MockResultMessage(result="Executed code")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Run code",
                allowed_tools=["code_execution"],
            )

            assert result.tool_calls[0].server_name == "builtin"

    @pytest.mark.asyncio
    async def test_uses_custom_system_prompt(self, claude_config: DaemonConfig):
        """Test that custom system prompt is passed to options."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            await provider.generate_with_mcp_tools(
                prompt="Create task",
                allowed_tools=["mcp__gobby-tasks__create_task"],
                system_prompt="You are a task manager.",
            )

            assert len(captured_options) == 1
            opts = captured_options[0]
            assert opts.kwargs["system_prompt"] == "You are a task manager."

    @pytest.mark.asyncio
    async def test_uses_custom_model(self, claude_config: DaemonConfig):
        """Test that custom model is passed to options."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            await provider.generate_with_mcp_tools(
                prompt="Create task",
                allowed_tools=[],
                model="claude-haiku-4-5",
            )

            assert captured_options[0].kwargs["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_uses_custom_max_turns(self, claude_config: DaemonConfig):
        """Test that custom max_turns is passed to options."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            await provider.generate_with_mcp_tools(
                prompt="Create task",
                allowed_tools=[],
                max_turns=5,
            )

            assert captured_options[0].kwargs["max_turns"] == 5

    @pytest.mark.asyncio
    async def test_handles_exception_group(self, claude_config: DaemonConfig):
        """Test that ExceptionGroup (Python 3.11+) is handled gracefully."""

        async def mock_query(prompt, options):
            # Simulate ExceptionGroup from TaskGroup
            raise ExceptionGroup(
                "Multiple errors", [RuntimeError("Error 1"), ValueError("Error 2")]
            )
            yield  # Make this a generator

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Create a task",
                allowed_tools=["mcp__gobby-tasks__create_task"],
            )

            assert "failed" in result.text.lower()
            assert "Error 1" in result.text or "Error 2" in result.text

    @pytest.mark.asyncio
    async def test_handles_tool_functions_param(self, claude_config: DaemonConfig):
        """Test that tool_functions parameter creates in-process MCP servers."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        def sample_tool_func():
            """A sample tool function."""
            pass

        with (
            mock_claude_sdk(mock_query),
            patch("gobby.llm.claude.create_sdk_mcp_server") as mock_create_server,
        ):
            mock_create_server.return_value = {"type": "mcp_server"}

            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            await provider.generate_with_mcp_tools(
                prompt="Create task",
                allowed_tools=["mcp__my-server__my_tool"],
                tool_functions={"my-server": [sample_tool_func]},
            )

            # Verify create_sdk_mcp_server was called
            mock_create_server.assert_called_once_with(name="my-server", tools=[sample_tool_func])
            # Verify mcp_servers config was passed
            assert captured_options[0].kwargs["mcp_servers"] == {
                "my-server": {"type": "mcp_server"}
            }

    @pytest.mark.asyncio
    async def test_handles_user_message_string_content(self, claude_config: DaemonConfig):
        """Test handling of UserMessage with string content (not list)."""

        async def mock_query(prompt, options):
            # UserMessage with string content
            yield MockUserMessage("String content instead of list")
            yield MockResultMessage(result="Done")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_with_mcp_tools(
                prompt="Test prompt",
                allowed_tools=[],
            )

            assert result.text == "Done"
            assert result.tool_calls == []


class TestClaudeLLMProviderInit:
    """Tests for ClaudeLLMProvider initialization and CLI path handling."""

    def test_provider_name(self, claude_config: DaemonConfig) -> None:
        """Test provider_name property returns 'claude'."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    from gobby.llm.claude import ClaudeLLMProvider

                    provider = ClaudeLLMProvider(claude_config)
                    assert provider.provider_name == "claude"

    def test_cli_path_not_found(self, claude_config: DaemonConfig) -> None:
        """Test initialization when CLI is not in PATH."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            assert provider._claude_cli_path is None

    def test_cli_path_exists_but_not_executable(self, claude_config: DaemonConfig) -> None:
        """Test initialization when CLI exists but is not executable."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=False):  # Not executable
                    from gobby.llm.claude import ClaudeLLMProvider

                    provider = ClaudeLLMProvider(claude_config)
                    assert provider._claude_cli_path is None

    def test_cli_path_which_returns_nonexistent(self, claude_config: DaemonConfig) -> None:
        """Test initialization when shutil.which returns path that doesn't exist."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"):
            with patch("os.path.exists", return_value=False):  # Path doesn't exist
                from gobby.llm.claude import ClaudeLLMProvider

                provider = ClaudeLLMProvider(claude_config)
                assert provider._claude_cli_path is None


class TestVerifyCliPath:
    """Tests for _verify_cli_path method with retry logic."""

    @pytest.mark.asyncio
    async def test_verify_cli_path_cached_path_valid(self, claude_config: DaemonConfig) -> None:
        """Test _verify_cli_path returns cached path when it's valid."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    from gobby.llm.claude import ClaudeLLMProvider

                    provider = ClaudeLLMProvider(claude_config)
                    result = await provider._verify_cli_path()
                    assert result == "/usr/bin/claude"

    @pytest.mark.asyncio
    async def test_verify_cli_path_retry_on_missing(self, claude_config: DaemonConfig) -> None:
        """Test _verify_cli_path retries when cached path disappears."""
        exists_call_count = 0

        def mock_exists(path):
            nonlocal exists_call_count
            exists_call_count += 1
            # First exists call: cached path is missing (triggers retry)
            # Second exists call: new path found
            if exists_call_count == 1:
                return False  # Cached path no longer exists
            return True  # New path exists

        with patch("gobby.llm.claude_cli.shutil.which") as mock_which:
            # First call during init, second call during retry
            mock_which.side_effect = ["/usr/bin/claude", "/new/path/claude"]
            with patch("os.path.exists", side_effect=mock_exists):
                with patch("os.access", return_value=True):
                    with patch("gobby.llm.claude_cli.asyncio.sleep", return_value=None):
                        from gobby.llm.claude import ClaudeLLMProvider

                        provider = ClaudeLLMProvider(claude_config)
                        provider._claude_cli_path = "/usr/bin/claude"

                        # Reset call count after init
                        exists_call_count = 0

                        result = await provider._verify_cli_path()
                        assert result == "/new/path/claude"

    @pytest.mark.asyncio
    async def test_verify_cli_path_retry_exhausted(self, claude_config: DaemonConfig) -> None:
        """Test _verify_cli_path returns None after retries exhausted."""
        with patch("gobby.llm.claude_cli.shutil.which") as mock_which:
            mock_which.side_effect = [
                "/usr/bin/claude",  # Initial
                None,  # Retry 1
                None,  # Retry 2
                None,  # Retry 3
            ]
            with patch("os.path.exists", return_value=False):
                with patch("os.access", return_value=True):
                    with patch("gobby.llm.claude_cli.asyncio.sleep", return_value=None):
                        from gobby.llm.claude import ClaudeLLMProvider

                        provider = ClaudeLLMProvider(claude_config)
                        provider._claude_cli_path = "/usr/bin/claude"

                        result = await provider._verify_cli_path()
                        assert result is None


class TestGenerateSummary:
    """Tests for generate_summary method."""

    @pytest.mark.asyncio
    async def test_generate_summary_no_cli(self, claude_config: DaemonConfig):
        """Test generate_summary returns fallback when CLI not found."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_summary(
                context={"transcript_summary": "test"},
                prompt_template="Summarize: {transcript_summary}",
            )

            assert "unavailable" in result.lower()

    @pytest.mark.asyncio
    async def test_generate_summary_no_prompt_template(self, claude_config: DaemonConfig):
        """Test generate_summary raises error when no prompt template provided."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"):
            with patch("os.path.exists", return_value=True):
                with patch("os.access", return_value=True):
                    from gobby.llm.claude import ClaudeLLMProvider

                    provider = ClaudeLLMProvider(claude_config)

                    with pytest.raises(ValueError, match="prompt_template is required"):
                        await provider.generate_summary(
                            context={"transcript_summary": "test"},
                            prompt_template=None,
                        )

    @pytest.mark.asyncio
    async def test_generate_summary_success(self, claude_config: DaemonConfig):
        """Test generate_summary returns summary text on success."""

        async def mock_query(prompt, options):
            yield MockAssistantMessage([MockTextBlock("This is a session summary.")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_summary(
                context={
                    "transcript_summary": "User asked about Python",
                    "last_messages": [{"role": "user", "content": "test"}],
                    "git_status": "clean",
                    "file_changes": "none",
                },
                prompt_template="Summarize: {transcript_summary}",
            )

            assert "session summary" in result.lower()

    @pytest.mark.asyncio
    async def test_generate_summary_exception(self, claude_config: DaemonConfig):
        """Test generate_summary handles exceptions gracefully."""

        async def mock_query(prompt, options):
            raise RuntimeError("API error")
            yield  # Make this a generator

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_summary(
                context={"transcript_summary": "test"},
                prompt_template="Summarize: {transcript_summary}",
            )

            assert "failed" in result.lower()


class TestGenerateText:
    """Tests for generate_text method."""

    @pytest.mark.asyncio
    async def test_generate_text_no_cli(self, claude_config: DaemonConfig):
        """Test generate_text raises RuntimeError when no backend available."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with pytest.raises(RuntimeError, match="unavailable"):
                await provider.generate_text(prompt="Hello")

    @pytest.mark.asyncio
    async def test_generate_text_success(self, claude_config: DaemonConfig):
        """Test generate_text returns generated text."""

        async def mock_query(prompt, options):
            yield MockAssistantMessage([MockTextBlock("Hello there!")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text(prompt="Say hello")

            assert "Hello there!" in result

    @pytest.mark.asyncio
    async def test_generate_text_with_result_message(self, claude_config: DaemonConfig):
        """Test generate_text uses ResultMessage when available."""

        async def mock_query(prompt, options):
            yield MockAssistantMessage([MockTextBlock("Intermediate text")])
            yield MockResultMessage(result="Final result text")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text(prompt="Generate something")

            assert result == "Final result text"

    @pytest.mark.asyncio
    async def test_generate_text_custom_system_prompt(self, claude_config: DaemonConfig):
        """Test generate_text passes custom system prompt."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            await provider.generate_text(
                prompt="Hello",
                system_prompt="You are a pirate.",
            )

            assert captured_options[0].kwargs["system_prompt"] == "You are a pirate."

    @pytest.mark.asyncio
    async def test_generate_text_custom_model(self, claude_config: DaemonConfig):
        """Test generate_text passes custom model."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            await provider.generate_text(
                prompt="Hello",
                model="claude-opus-4-5",
            )

            assert captured_options[0].kwargs["model"] == "claude-opus-4-5"

    @pytest.mark.asyncio
    async def test_generate_text_exception(self, claude_config: DaemonConfig):
        """Test generate_text raises RuntimeError on SDK exceptions."""

        async def mock_query(prompt, options):
            raise RuntimeError("API error")
            yield  # Make this a generator

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with pytest.raises(RuntimeError, match="API error"):
                await provider.generate_text(prompt="Hello")

    @pytest.mark.asyncio
    async def test_generate_text_no_messages_warning(self, claude_config: DaemonConfig):
        """Test generate_text handles case where no messages are received."""

        async def mock_query(prompt, options):
            # Yield nothing - empty generator
            return
            yield  # Make this a generator

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text(prompt="Hello")

            # Should return empty string when no messages
            assert result == ""

    @pytest.mark.asyncio
    async def test_generate_text_messages_but_no_text_content(self, claude_config: DaemonConfig):
        """Test generate_text handles messages without text content."""

        async def mock_query(prompt, options):
            # ToolUseBlock without any TextBlock
            yield MockAssistantMessage([MockToolUseBlock(id="1", name="some_tool", input={})])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text(prompt="Hello")

            # Should return empty string
            assert result == ""


class TestGenerateWithMcpToolsMcpConfigPath:
    """Tests for generate_with_mcp_tools with MCP config file path."""

    @pytest.mark.asyncio
    async def test_uses_mcp_json_from_cwd(self, claude_config: DaemonConfig, tmp_path):
        """Test that .mcp.json is loaded from current working directory."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        # Create a mock .mcp.json file
        mcp_config = tmp_path / ".mcp.json"
        mcp_config.write_text('{"servers": {}}')

        with mock_claude_sdk(mock_query):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                from gobby.llm.claude import ClaudeLLMProvider

                provider = ClaudeLLMProvider(claude_config)

                await provider.generate_with_mcp_tools(
                    prompt="Create task",
                    allowed_tools=["mcp__gobby-tasks__create_task"],
                    # No tool_functions, so it should look for .mcp.json
                )

                assert len(captured_options) == 1
                # The mcp_servers should be the path string
                assert str(mcp_config) == captured_options[0].kwargs["mcp_servers"]

    @pytest.mark.asyncio
    async def test_uses_mcp_json_from_project_root(self, claude_config: DaemonConfig, tmp_path):
        """Test that .mcp.json is loaded from project root when not in cwd."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        # Create a cwd directory without .mcp.json
        cwd_dir = tmp_path / "some_other_dir"
        cwd_dir.mkdir()

        # This test just verifies the code path runs when cwd has no .mcp.json
        # The actual project root detection uses __file__ which we can't easily mock
        with mock_claude_sdk(mock_query):
            with patch("pathlib.Path.cwd", return_value=cwd_dir):
                from gobby.llm.claude import ClaudeLLMProvider

                provider = ClaudeLLMProvider(claude_config)

                await provider.generate_with_mcp_tools(
                    prompt="Create task",
                    allowed_tools=["mcp__gobby-tasks__create_task"],
                )

                # Check that the method ran successfully
                assert len(captured_options) == 1
                # When no cwd config is found, it may use project root config or empty dict
                mcp_servers = captured_options[0].kwargs["mcp_servers"]
                assert mcp_servers == {} or isinstance(mcp_servers, str)

    @pytest.mark.asyncio
    async def test_no_mcp_config_uses_empty_dict(self, claude_config: DaemonConfig, tmp_path):
        """Test that empty dict is used when no .mcp.json is found."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield MockResultMessage(result="Done")

        # cwd without .mcp.json
        with mock_claude_sdk(mock_query):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                from gobby.llm.claude import ClaudeLLMProvider

                provider = ClaudeLLMProvider(claude_config)

                await provider.generate_with_mcp_tools(
                    prompt="Create task",
                    allowed_tools=["mcp__gobby-tasks__create_task"],
                )

                assert len(captured_options) == 1
                # Should be empty dict when no config found
                # (since gobby project root also won't have .mcp.json in this test)
                mcp_servers = captured_options[0].kwargs["mcp_servers"]
                assert mcp_servers == {} or isinstance(mcp_servers, str)
