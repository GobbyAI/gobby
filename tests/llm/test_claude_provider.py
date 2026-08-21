"""Tests for ClaudeLLMProvider provider primitives."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.llm.claude import ClaudeLLMProvider
from gobby.llm.claude_payloads import normalize_claude_usage

pytestmark = pytest.mark.unit

# --- Mocks for claude_agent_sdk ---


class MockAssistantMessage:
    def __init__(self, content: list[object]) -> None:
        self.content = content


class MockResultMessage:
    def __init__(self, result: str | None = None, usage: dict[str, Any] | None = None) -> None:
        self.result = result
        self.usage = usage


class MockTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class MockToolUseBlock:
    def __init__(self, id: str, name: str, input: dict[str, Any]) -> None:
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
def claude_config() -> DaemonConfig:
    return DaemonConfig()


@contextmanager
def mock_claude_sdk(
    mock_query_func: Callable[[str, object], AsyncIterator[object]],
) -> Iterator[None]:
    async def query_wrapper(*args: object, **kwargs: object) -> AsyncIterator[object]:
        prompt = kwargs.get("prompt", args[0] if args else "")
        options = kwargs.get("options", args[1] if len(args) > 1 else None)
        async for message in mock_query_func(str(prompt), options):
            yield message

    with (
        patch("gobby.llm.claude_cli.shutil.which", return_value="/mock/claude"),
        patch("os.path.exists", return_value=True),
        patch("os.access", return_value=True),
        patch("gobby.llm.claude_sdk.query", query_wrapper),
        patch("gobby.llm.claude_sdk.AssistantMessage", MockAssistantMessage),
        patch("gobby.llm.claude_sdk.ResultMessage", MockResultMessage),
        patch("gobby.llm.claude_sdk.TextBlock", MockTextBlock),
        patch("gobby.llm.claude_sdk.ToolUseBlock", MockToolUseBlock),
        patch("gobby.llm.claude_sdk.ClaudeAgentOptions", MockClaudeAgentOptions),
    ):
        yield


# --- Tests ---


@pytest.mark.asyncio
async def test_verify_cli_path_retry(claude_config: DaemonConfig) -> None:
    """Test race condition handling in _verify_cli_path."""

    # Mock shutil.which to fail twice then succeed
    side_effects = [None, None, "/found/now"]

    with patch("gobby.llm.claude_cli.shutil.which", side_effect=side_effects) as mock_which:
        with patch("gobby.llm.claude_cli.asyncio.sleep", return_value=None) as mock_sleep:
            with patch("os.path.exists", return_value=True), patch("os.access", return_value=True):
                provider = ClaudeLLMProvider(claude_config)

                provider._claude_cli_path = "/old/path"

                def exists_side_effect(path: str) -> bool:
                    return path == "/found/now"

                with patch("os.path.exists", side_effect=exists_side_effect):
                    path = await provider._verify_cli_path()
                    assert path == "/found/now"
                    assert provider._claude_cli_path == "/found/now"
                    assert mock_which.call_count == 3
                    assert mock_sleep.call_count == 1


async def test_generate_text_discovers_cli_installed_after_startup(
    claude_config: DaemonConfig,
) -> None:
    async def mock_query(_prompt: str, _options: object) -> AsyncIterator[object]:
        yield MockAssistantMessage([MockTextBlock("Generated after install")])

    with mock_claude_sdk(mock_query):
        with (
            patch(
                "gobby.llm.claude_cli.shutil.which",
                side_effect=[None, "/new/claude"],
            ) as mock_which,
            patch("gobby.llm.claude_cli.os.path.exists", return_value=True),
        ):
            provider = ClaudeLLMProvider(claude_config)
            assert provider._claude_cli_path is None

            assert await provider.generate_text("prompt") == "Generated after install"
            assert provider._claude_cli_path == "/new/claude"

            assert await provider.generate_text("prompt again") == "Generated after install"
            assert mock_which.call_count == 2


async def test_generate_text_still_fails_when_cli_remains_missing(
    claude_config: DaemonConfig,
) -> None:
    with (
        patch("gobby.llm.claude_cli.shutil.which", return_value=None) as mock_which,
        patch("gobby.llm.claude_cli.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        provider = ClaudeLLMProvider(claude_config)

        with pytest.raises(RuntimeError, match="Claude CLI not found"):
            await provider.generate_text("prompt")

        assert provider._claude_cli_path is None
        assert mock_which.call_count == 2
        mock_sleep.assert_not_awaited()


async def test_concurrent_invalid_cache_rediscovery_does_not_clobber_success(
    claude_config: DaemonConfig,
) -> None:
    entered_retry = asyncio.Event()
    release_retry = asyncio.Event()

    async def wait_for_retry(_delay: float) -> None:
        entered_retry.set()
        await release_retry.wait()

    with patch.object(ClaudeLLMProvider, "_find_cli_path", return_value=None):
        provider = ClaudeLLMProvider(claude_config)
    provider._claude_cli_path = "/old/claude"

    with (
        patch(
            "gobby.llm.claude_cli.shutil.which",
            side_effect=[None, "/new/claude"],
        ) as mock_which,
        patch(
            "gobby.llm.claude_cli.os.path.exists",
            side_effect=lambda path: path == "/new/claude",
        ),
        patch("gobby.llm.claude_cli.os.access", return_value=True),
        patch("gobby.llm.claude_cli.asyncio.sleep", side_effect=wait_for_retry),
    ):
        first = asyncio.create_task(provider._verify_cli_path())
        await entered_retry.wait()
        second = asyncio.create_task(provider._verify_cli_path())
        release_retry.set()

        assert await asyncio.gather(first, second) == ["/new/claude", "/new/claude"]

    assert provider._claude_cli_path == "/new/claude"
    assert mock_which.call_count == 2


@pytest.mark.asyncio
async def test_generate_text(claude_config: DaemonConfig) -> None:
    async def mock_query(_prompt: str, _options: object) -> AsyncIterator[object]:
        yield MockAssistantMessage([MockTextBlock("Generated text")])

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        text = await provider.generate_text("prompt")
        assert text == "Generated text"


def test_normalize_claude_usage_maps_anthropic_fields() -> None:
    assert normalize_claude_usage({"input_tokens": 10, "output_tokens": 5}) == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "input_tokens": 10,
        "output_tokens": 5,
    }


def test_normalize_claude_usage_preserves_openai_total_and_cache_fields() -> None:
    assert normalize_claude_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 99,
            "cache_read_input_tokens": 4,
        }
    ) == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 99,
        "cache_read_input_tokens": 4,
    }


def test_normalize_claude_usage_counts_cache_tokens_in_computed_total() -> None:
    assert normalize_claude_usage(
        {
            "input_tokens": 100,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 10,
            "output_tokens": 25,
        }
    ) == {
        "prompt_tokens": 100,
        "completion_tokens": 25,
        "total_tokens": 175,
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 40,
    }


def test_normalize_claude_usage_returns_none_without_counts() -> None:
    assert normalize_claude_usage(None) is None
    assert normalize_claude_usage({}) is None
    assert normalize_claude_usage({"foo": "bar"}) is None


@pytest.mark.asyncio
async def test_generate_text_result_surfaces_anthropic_usage(
    claude_config: DaemonConfig,
) -> None:
    async def mock_query(_prompt: str, _options: object) -> AsyncIterator[object]:
        yield MockAssistantMessage([MockTextBlock("Generated text")])
        yield MockResultMessage(
            result="Generated text",
            usage={"input_tokens": 120, "output_tokens": 30},
        )

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        result = await provider.generate_text_result("prompt")

    assert result.text == "Generated text"
    assert result.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "input_tokens": 120,
        "output_tokens": 30,
    }


@pytest.mark.asyncio
async def test_generate_text_result_usage_none_without_result_message(
    claude_config: DaemonConfig,
) -> None:
    async def mock_query(_prompt: str, _options: object) -> AsyncIterator[object]:
        yield MockAssistantMessage([MockTextBlock("Generated text")])

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        result = await provider.generate_text_result("prompt")

    assert result.text == "Generated text"
    assert result.usage is None


@pytest.mark.asyncio
async def test_generate_text_threads_caller_into_operation_name(
    claude_config: DaemonConfig,
) -> None:
    async def mock_query(_prompt: str, _options: object) -> AsyncIterator[object]:
        yield MockAssistantMessage([MockTextBlock("Generated text")])

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        with patch(
            "gobby.llm.claude_sdk.execute_sdk_query",
            AsyncMock(return_value="Generated text"),
        ) as mock_execute:
            text = await provider.generate_text("prompt", caller="code_index.symbol_summary")

        assert text == "Generated text"
        assert mock_execute.await_args.args[0] == "generate_text[code_index.symbol_summary]"


def test_auth_mode_default_is_subscription(claude_config: DaemonConfig) -> None:
    """Test default auth_mode is subscription."""

    async def mock_query(_prompt: str, _options: object) -> AsyncIterator[object]:
        return
        yield  # Makes this an async generator that yields nothing

    with mock_claude_sdk(mock_query):
        provider = ClaudeLLMProvider(claude_config)
        assert provider.auth_mode == "subscription"
