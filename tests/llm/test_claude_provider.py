"""Tests for ClaudeLLMProvider methods: generate_summary, synthesize_title."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.config.sessions import SessionSummaryConfig
from gobby.llm.claude import ClaudeLLMProvider

pytestmark = pytest.mark.unit

# --- Mocks for claude_agent_sdk ---


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
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class MockToolResultBlock:
    def __init__(self, tool_use_id, content):
        self.tool_use_id = tool_use_id
        self.content = content


class MockClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


# --- Fixtures ---


@pytest.fixture
def claude_config():
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-3-5-sonnet"),
        ),
        session_summary=SessionSummaryConfig(enabled=True),
    )


@contextmanager
def mock_claude_sdk(mock_query_func):
    with (
        patch("gobby.llm.claude_cli.shutil.which", return_value="/mock/claude"),
        patch("os.path.exists", return_value=True),
        patch("os.access", return_value=True),
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


# --- Tests ---


@pytest.mark.asyncio
async def test_generate_summary_success(claude_config):
    async def mock_query(prompt, options):
        yield MockAssistantMessage([MockTextBlock("Summary of session.")])
        yield MockResultMessage(result="Summary of session.")

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        context = {"transcript_summary": "prev", "last_messages": []}
        summary = await provider.generate_summary(
            context, prompt_template="Sum: {transcript_summary}"
        )
        assert summary == "Summary of session."


@pytest.mark.asyncio
async def test_generate_summary_no_cli(claude_config):
    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(claude_config)
        summary = await provider.generate_summary({}, prompt_template="test")
        assert "unavailable" in summary.lower()


@pytest.mark.asyncio
async def test_verify_cli_path_retry(claude_config):
    """Test race condition handling in _verify_cli_path."""

    # Mock shutil.which to fail twice then succeed
    side_effects = [None, None, "/found/now"]

    with patch("gobby.llm.claude_cli.shutil.which", side_effect=side_effects) as mock_which:
        with patch("gobby.llm.claude_cli.time.sleep") as mock_sleep:
            with patch("os.path.exists", return_value=True):
                provider = ClaudeLLMProvider(claude_config)
                # Manually trigger verify logic
                # Initial find failed (in __init__ which calls _find_cli_path)
                # provider._claude_cli_path is None or whatever __init__ set it to.
                # Let's force it to be set but then "disappear"

                provider._claude_cli_path = "/old/path"
                # Patch exists to return False for /old/path

                def exists_side_effect(path):
                    return path == "/found/now"

                with patch("os.path.exists", side_effect=exists_side_effect):
                    path = provider._verify_cli_path()
                    assert path == "/found/now"
                    assert mock_which.call_count >= 1
                    assert mock_sleep.call_count >= 1


@pytest.mark.asyncio
async def test_generate_text(claude_config):
    async def mock_query(prompt, options):
        yield MockAssistantMessage([MockTextBlock("Generated text")])

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        text = await provider.generate_text("prompt")
        assert text == "Generated text"


# --- api_key Mode Tests ---


@pytest.fixture
def api_key_config():
    """Config with api_key auth mode."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5", auth_mode="api_key"),
            api_keys={"ANTHROPIC_API_KEY": "sk-ant-test-key"},
        ),
        session_summary=SessionSummaryConfig(enabled=True),
    )


class MockLiteLLMResponse:
    """Mock LiteLLM response object."""

    def __init__(self, content: str):
        self.choices = [MockChoice(content)]


class MockChoice:
    """Mock choice in LiteLLM response."""

    def __init__(self, content: str):
        self.message = MockMessage(content)


class MockMessage:
    """Mock message in LiteLLM response."""

    def __init__(self, content: str):
        self.content = content


class MockLiteLLM:
    """Mock LiteLLM module."""

    def __init__(self, response_content: str = "Generated response"):
        self.response_content = response_content
        self.call_count = 0
        self.last_call_args = None

    async def acompletion(self, **kwargs):
        self.call_count += 1
        self.last_call_args = kwargs
        return MockLiteLLMResponse(self.response_content)


def test_auth_mode_default_is_subscription(claude_config):
    """Test default auth_mode is subscription."""

    async def mock_query(prompt, options):
        return
        yield  # Makes this an async generator that yields nothing

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        assert provider.auth_mode == "subscription"


def test_auth_mode_from_config(api_key_config):
    """Test auth_mode read from config."""
    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(api_key_config)
        assert provider.auth_mode == "api_key"


def test_auth_mode_override_parameter(claude_config):
    """Test auth_mode can be overridden via parameter."""
    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(claude_config, auth_mode="api_key")
        assert provider.auth_mode == "api_key"


def test_api_key_mode_no_cli_needed(api_key_config):
    """Test api_key mode does not require Claude CLI."""
    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(api_key_config)
        assert provider._claude_cli_path is None
        assert provider.auth_mode == "api_key"


@pytest.mark.asyncio
async def test_generate_text_api_key_mode(api_key_config):
    """Test generate_text uses LiteLLM in api_key mode."""
    mock_litellm = MockLiteLLM("LiteLLM generated text")

    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(api_key_config)
        provider._litellm = mock_litellm

        text = await provider.generate_text("test prompt", system_prompt="Be helpful")
        assert text == "LiteLLM generated text"
        assert mock_litellm.call_count == 1
        assert mock_litellm.last_call_args["model"] == "anthropic/claude-haiku-4-5"


@pytest.mark.asyncio
async def test_generate_summary_api_key_mode(api_key_config):
    """Test generate_summary uses LiteLLM in api_key mode."""
    mock_litellm = MockLiteLLM("Session summary via LiteLLM")

    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(api_key_config)
        provider._litellm = mock_litellm

        context = {"transcript_summary": "test", "last_messages": []}
        summary = await provider.generate_summary(
            context, prompt_template="Summarize: {transcript_summary}"
        )
        assert summary == "Session summary via LiteLLM"
        assert mock_litellm.call_count == 1


@pytest.mark.asyncio
async def test_generate_with_mcp_tools_api_key_mode_returns_error(api_key_config):
    """Test generate_with_mcp_tools returns error in api_key mode."""
    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(api_key_config)

        result = await provider.generate_with_mcp_tools(
            prompt="test",
            allowed_tools=["mcp__test__tool"],
        )
        assert "subscription mode" in result.text
        assert result.tool_calls == []


@pytest.mark.asyncio
async def test_describe_image_subscription_mode(claude_config, tmp_path):
    """Test describe_image uses SDK in subscription mode."""
    # Create a test image file
    test_image = tmp_path / "test.png"
    test_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    async def mock_query(prompt, options):
        yield MockAssistantMessage([MockTextBlock("Image description from SDK")])

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        description = await provider.describe_image(str(test_image))
        assert "Image description from SDK" in description


@pytest.mark.asyncio
async def test_describe_image_api_key_mode(api_key_config, tmp_path):
    """Test describe_image uses LiteLLM in api_key mode."""
    # Create a test image file
    test_image = tmp_path / "test.png"
    test_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    mock_litellm = MockLiteLLM("Image description from LiteLLM")

    with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
        provider = ClaudeLLMProvider(api_key_config)
        provider._litellm = mock_litellm

        description = await provider.describe_image(str(test_image))
        assert description == "Image description from LiteLLM"
        assert mock_litellm.call_count == 1
        # Verify model used
        assert "anthropic/claude-haiku" in mock_litellm.last_call_args["model"]
