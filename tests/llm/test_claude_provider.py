"""Tests for ClaudeLLMProvider methods: generate_summary, synthesize_title."""

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

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


class MockClaudeAgentOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs: dict[str, Any] = kwargs
        self.settings: str | None = None
        self.setting_sources: list[str] | None = None
        self.stderr: Any = None


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
        with patch("gobby.llm.claude_cli.asyncio.sleep", return_value=None) as mock_sleep:
            with patch("os.path.exists", return_value=True):
                provider = ClaudeLLMProvider(claude_config)

                provider._claude_cli_path = "/old/path"

                def exists_side_effect(path):
                    return path == "/found/now"

                with patch("os.path.exists", side_effect=exists_side_effect):
                    path = await provider._verify_cli_path()
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


@pytest.mark.asyncio
async def test_generate_text_threads_caller_into_operation_name(claude_config):
    async def mock_query(prompt, options):
        yield MockAssistantMessage([MockTextBlock("Generated text")])

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        with patch.object(
            provider,
            "_execute_sdk_query",
            AsyncMock(return_value="Generated text"),
        ) as mock_execute:
            text = await provider.generate_text("prompt", caller="code_index.symbol_summary")

        assert text == "Generated text"
        assert mock_execute.await_args.args[0] == "generate_text[code_index.symbol_summary]"


def test_auth_mode_default_is_subscription(claude_config):
    """Test default auth_mode is subscription."""

    async def mock_query(prompt, options):
        return
        yield  # Makes this an async generator that yields nothing

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        assert provider.auth_mode == "subscription"


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
