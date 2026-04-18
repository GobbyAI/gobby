"""Tests for ClaudeLLMProvider edge cases and error handling.

Focuses on auth_mode selection, _is_transient_error classification,
_retry_async logic, _format_summary_context, _prepare_image_data,
generate_json, stream_with_mcp_tools, and describe_image.
"""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

pytestmark = pytest.mark.unit


# ─── Mock SDK classes ───────────────────────────────────────────────────


class MockAssistantMessage:
    def __init__(self, content: list) -> None:
        self.content = content


class MockResultMessage:
    def __init__(self, result: str | None = None) -> None:
        self.result = result


class MockTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class MockToolUseBlock:
    def __init__(self, id: str, name: str, input: dict) -> None:
        self.id = id
        self.name = name
        self.input = input


class MockClaudeAgentOptions:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.settings: str | None = None
        self.setting_sources: list[str] | None = None
        self.stderr: object = None


@pytest.fixture
def claude_config() -> DaemonConfig:
    """DaemonConfig with Claude provider."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-sonnet-4-5"),
        ),
    )


@contextmanager
def mock_claude_sdk(mock_query_func: Any) -> Generator[None]:
    """Mock the Claude Agent SDK for testing."""
    with (
        patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"),
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


# ─── Auth mode tests ────────────────────────────────────────────────────


class TestAuthModeSelection:
    """Tests for auth_mode determination."""

    def test_auth_mode_default_subscription(self) -> None:
        """Default auth_mode is subscription."""
        config = DaemonConfig()
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(config)
            assert provider.auth_mode == "subscription"


# ─── _is_transient_error tests ──────────────────────────────────────────


class TestIsTransientError:
    """Tests for _is_transient_error classification."""

    def test_permanent_errors(self) -> None:
        """Auth/permission errors are permanent (not transient)."""
        from gobby.llm.claude import ClaudeLLMProvider

        assert ClaudeLLMProvider._is_transient_error(Exception("401 Unauthorized")) is False
        assert ClaudeLLMProvider._is_transient_error(Exception("403 Forbidden")) is False
        assert ClaudeLLMProvider._is_transient_error(Exception("invalid_api_key")) is False
        assert ClaudeLLMProvider._is_transient_error(Exception("authentication failed")) is False
        assert ClaudeLLMProvider._is_transient_error(Exception("permission denied")) is False
        assert ClaudeLLMProvider._is_transient_error(Exception("not_found 404")) is False

    def test_transient_errors(self) -> None:
        """Timeout/server errors are transient."""
        from gobby.llm.claude import ClaudeLLMProvider

        assert ClaudeLLMProvider._is_transient_error(Exception("timeout")) is True
        assert ClaudeLLMProvider._is_transient_error(Exception("rate limit exceeded")) is True
        assert ClaudeLLMProvider._is_transient_error(Exception("500 Internal Server Error")) is True
        assert ClaudeLLMProvider._is_transient_error(Exception("connection reset")) is True


# ─── _retry_async tests ─────────────────────────────────────────────────


class TestRetryAsync:
    """Tests for _retry_async method."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_first_attempt(self, claude_config: DaemonConfig) -> None:
        """No retries needed when first attempt succeeds."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            async def success() -> str:
                return "ok"

            result = await provider._retry_async(success, max_retries=3)
            assert result == "ok"

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, claude_config: DaemonConfig) -> None:
        """Retries on transient errors with exponential backoff."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            call_count = 0

            async def flaky() -> str:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise Exception("timeout")
                return "ok"

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await provider._retry_async(flaky, max_retries=3, delay=0.01)

            assert result == "ok"
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_permanent_error(self, claude_config: DaemonConfig) -> None:
        """Permanent errors raise immediately without retry."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            async def permanent_fail() -> str:
                raise Exception("401 Unauthorized")

            with pytest.raises(Exception, match="401 Unauthorized"):
                await provider._retry_async(permanent_fail, max_retries=3)

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self, claude_config: DaemonConfig) -> None:
        """After max retries, the last exception is raised."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            async def always_fail() -> str:
                raise Exception("timeout again")

            with (
                patch("asyncio.sleep", new_callable=AsyncMock),
                pytest.raises(Exception, match="timeout again"),
            ):
                await provider._retry_async(always_fail, max_retries=2, delay=0.01)

    @pytest.mark.asyncio
    async def test_retry_calls_on_retry_callback(self, claude_config: DaemonConfig) -> None:
        """on_retry callback is called on each retry attempt."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            retry_calls: list[tuple[int, Exception]] = []

            def on_retry(attempt: int, error: Exception) -> None:
                retry_calls.append((attempt, error))

            call_count = 0

            async def flaky() -> str:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise Exception("timeout")
                return "ok"

            with patch("asyncio.sleep", new_callable=AsyncMock):
                await provider._retry_async(flaky, max_retries=3, delay=0.01, on_retry=on_retry)

            assert len(retry_calls) == 2
            assert retry_calls[0][0] == 0
            assert retry_calls[1][0] == 1


# ─── _format_summary_context tests ──────────────────────────────────────


class TestFormatSummaryContext:
    """Tests for _format_summary_context."""

    def test_format_with_jinja2(self, claude_config: DaemonConfig) -> None:
        """Renders context with Jinja2 template syntax."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            context = {
                "transcript_summary": "User asked about Python",
                "last_messages": [{"role": "user", "content": "hi"}],
                "git_status": "clean",
                "file_changes": "none",
            }
            result = provider._format_summary_context(context, "Summary: {{ transcript_summary }}")
            assert "User asked about Python" in result

    def test_format_raises_on_none_template(self, claude_config: DaemonConfig) -> None:
        """Raises ValueError when prompt_template is None."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with pytest.raises(ValueError, match="prompt_template is required"):
                provider._format_summary_context({}, None)

    def test_format_extra_context_keys(self, claude_config: DaemonConfig) -> None:
        """Extra context keys are passed through."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            context = {"custom_key": "custom_value"}
            result = provider._format_summary_context(context, "Custom: {{ custom_key }}")
            assert "custom_value" in result


# ─── _prepare_image_data tests ──────────────────────────────────────────


class TestPrepareImageData:
    """Tests for _prepare_image_data."""

    def test_image_not_found(self, claude_config: DaemonConfig) -> None:
        """Returns error string when image doesn't exist."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = provider._prepare_image_data("/nonexistent/image.png")
            assert isinstance(result, str)
            assert "not found" in result.lower()

    def test_valid_image(self, claude_config: DaemonConfig, tmp_path: Path) -> None:
        """Returns (base64, mime_type) for valid image."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            img_path = tmp_path / "test.png"
            img_path.write_bytes(b"\x89PNG\r\n")

            result = provider._prepare_image_data(str(img_path))
            assert isinstance(result, tuple)
            assert len(result) == 2
            assert result[1] == "image/png"

    def test_unknown_mime_defaults_to_png(
        self, claude_config: DaemonConfig, tmp_path: Path
    ) -> None:
        """Unknown extensions default to image/png."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            img_path = tmp_path / "test.xyz"
            img_path.write_bytes(b"data")

            result = provider._prepare_image_data(str(img_path))
            assert isinstance(result, tuple)
            assert result[1] == "image/png"

    def test_read_error(self, claude_config: DaemonConfig, tmp_path: Path) -> None:
        """Returns error string when file can't be read."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            img_path = tmp_path / "test.png"
            img_path.write_bytes(b"data")

            with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
                result = provider._prepare_image_data(str(img_path))
                assert isinstance(result, str)
                assert "Failed to read" in result


# ─── generate_json tests ────────────────────────────────────────────────


class TestGenerateJson:
    """Tests for generate_json method."""

    @pytest.mark.asyncio
    async def test_generate_json_no_backend(self, claude_config: DaemonConfig) -> None:
        """Raises RuntimeError when CLI not available."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            with pytest.raises(RuntimeError, match="unavailable"):
                await provider.generate_json("Generate JSON")

    @pytest.mark.asyncio
    async def test_generate_json_sdk_parses_json(self, claude_config: DaemonConfig) -> None:
        """SDK path parses JSON response using output_format constraint."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockAssistantMessage([MockTextBlock('{"key": "value"}')])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider._generate_json_sdk("Generate JSON")

            assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_generate_json_sdk_invalid_json(self, claude_config: DaemonConfig) -> None:
        """SDK path raises ValueError with response snippet on invalid JSON."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockAssistantMessage([MockTextBlock("not json")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            with pytest.raises(ValueError, match="not json"):
                await provider._generate_json_sdk("Generate JSON")

    @pytest.mark.asyncio
    async def test_generate_json_sdk_markdown_fence_fallback(
        self, claude_config: DaemonConfig
    ) -> None:
        """SDK path extracts JSON from markdown-fenced response."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockAssistantMessage([MockTextBlock('```json\n{"entities": []}\n```')])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider._generate_json_sdk("Generate JSON")

            assert result == {"entities": []}


# ─── describe_image tests ───────────────────────────────────────────────


class TestDescribeImage:
    """Tests for describe_image method."""

    @pytest.mark.asyncio
    async def test_describe_image_sdk_no_cli(self, claude_config: DaemonConfig) -> None:
        """Returns unavailable message when CLI not found."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider._describe_image_sdk("/path/to/image.png")
            assert "unavailable" in result.lower()


# ─── generate_text no backend ────────────────────────────────────────────


class TestGenerateTextNoBackend:
    """Tests for generate_text when no backend is available."""

    @pytest.mark.asyncio
    async def test_no_cli_raises(self, claude_config: DaemonConfig) -> None:
        """Raises RuntimeError when CLI is not available."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            with pytest.raises(RuntimeError, match="unavailable"):
                await provider.generate_text("Hello")


# ─── generate_summary routing ────────────────────────────────────────────


class TestGenerateSummaryRouting:
    """Tests for generate_summary routing logic."""

    @pytest.mark.asyncio
    async def test_no_cli_returns_unavailable(self, claude_config: DaemonConfig) -> None:
        """Returns unavailable message when CLI is not available."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            result = await provider.generate_summary(
                context={},
                prompt_template="test",
            )
            assert "unavailable" in result.lower()
