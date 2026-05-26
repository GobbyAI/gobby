"""Tests for ClaudeLLMProvider edge cases and error handling.

Focuses on auth_mode selection, _is_transient_error classification,
_retry_async logic, _format_summary_context, _prepare_image_data,
generate_json, stream_with_mcp_tools, and describe_image.
"""

import logging
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
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.settings: str | None = None
        self.setting_sources: list[str] | None = None
        self.stderr: object = None


class MockExitCodeError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


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

    def test_error_result_success_is_not_retried(self) -> None:
        """Known Claude SDK error-result-success failures are not retried noisily."""
        from gobby.llm.claude import ClaudeLLMProvider

        error = Exception("Claude Code returned an error result: success")
        assert ClaudeLLMProvider._is_transient_error(error) is False

    def test_sigterm_exit_code_is_not_retried(self) -> None:
        """Claude SDK SIGTERM exits are shutdown cancellation, not transient LLM errors."""
        from gobby.llm.claude import ClaudeLLMProvider

        attr_error = MockExitCodeError("Claude process exited", 143)
        message_error = Exception("Claude process exited with exit code 143")

        assert ClaudeLLMProvider._is_sdk_sigterm_shutdown(attr_error) is True
        assert ClaudeLLMProvider._is_sdk_sigterm_shutdown(message_error) is True
        assert ClaudeLLMProvider._is_transient_error(attr_error) is False
        assert ClaudeLLMProvider._is_transient_error(message_error) is False


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


class TestExecuteSdkQuery:
    """Tests for SDK query execution failure classification."""

    @pytest.mark.asyncio
    async def test_sigterm_exit_does_not_retry_or_warn(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exit code 143 is raised as cancellation without retry warning noise."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider, ClaudeSDKShutdownCancellation

            provider = ClaudeLLMProvider(claude_config)
            options = MockClaudeAgentOptions()
            call_count = 0
            caplog.clear()

            async def terminated() -> str:
                nonlocal call_count
                call_count += 1
                raise MockExitCodeError("Claude process exited", 143)

            with (
                patch("gobby.llm.claude.asyncio.sleep", new_callable=AsyncMock) as sleep,
                caplog.at_level(logging.INFO, logger="gobby.llm.claude"),
                pytest.raises(ClaudeSDKShutdownCancellation, match="generate_json cancelled"),
            ):
                await provider._execute_sdk_query(
                    "generate_json",
                    terminated,
                    options,
                    max_retries=3,
                    retry_delay=0.01,
                )

        sleep.assert_not_awaited()
        assert call_count == 1
        assert "retrying" not in caplog.text
        assert not any(
            record.name == "gobby.llm.claude" and record.levelno >= logging.WARNING
            for record in caplog.records
        )


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
    async def test_generate_json_sdk_disables_filesystem_settings_with_empty_list(
        self, claude_config: DaemonConfig
    ) -> None:
        """Internal SDK calls isolate settings with setting_sources=[]."""
        captured_sources: list[list[str] | None] = []

        async def mock_query(prompt: str, options: object) -> object:
            captured_sources.append(options.setting_sources)
            yield MockAssistantMessage([MockTextBlock('{"isolated": true}')])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider._generate_json_sdk("Generate JSON")

        assert result == {"isolated": True}
        assert captured_sources == [[]]

    @pytest.mark.asyncio
    async def test_generate_json_sdk_passes_system_prompt_output_format_and_caller(
        self, claude_config: DaemonConfig
    ) -> None:
        """Feature JSON calls should make their instruction contract visible to Claude."""
        captured: dict[str, object] = {}

        async def mock_query(prompt: str, options: Any) -> object:
            captured["prompt"] = prompt
            captured["system_prompt"] = options.system_prompt
            captured["output_format"] = options.output_format
            yield MockAssistantMessage([MockTextBlock('{"entities": []}')])

        async def execute_sdk_query(
            operation: str,
            query_fn: Any,
            options: object,
            **kwargs: object,
        ) -> str:
            captured["operation"] = operation
            return await query_fn()

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with patch.object(provider, "_execute_sdk_query", side_effect=execute_sdk_query):
                result = await provider._generate_json_sdk(
                    "rendered entity extraction prompt",
                    "strict entity extraction system prompt",
                    "haiku",
                    caller="memory.kg.extract_entities",
                )

        assert result == {"entities": []}
        assert captured["prompt"] == "rendered entity extraction prompt"
        assert captured["system_prompt"] == "strict entity extraction system prompt"
        assert captured["output_format"] == {"type": "json_object"}
        assert captured["operation"] == "generate_json[memory.kg.extract_entities]"

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

    @pytest.mark.asyncio
    async def test_generate_json_sdk_classifies_error_result_success(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known SDK error-result-success failures log one warning and no traceback."""

        async def mock_query(prompt: str, options: object) -> object:
            raise Exception("Claude Code returned an error result: success")
            yield

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider, ClaudeSDKProviderFailure

            provider = ClaudeLLMProvider(claude_config)

            with (
                patch("gobby.llm.claude.asyncio.sleep", new_callable=AsyncMock) as sleep,
                caplog.at_level(logging.WARNING, logger="gobby.llm.claude"),
                pytest.raises(ClaudeSDKProviderFailure, match="generate_json provider degraded"),
            ):
                await provider._generate_json_sdk("Generate JSON")

        sleep.assert_not_awaited()
        assert "provider degraded: Claude SDK returned error-result-success" in caplog.text
        assert "retrying" not in caplog.text
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)


class TestGenerateTextProviderFailures:
    """Tests for generate_text provider failure classification."""

    @pytest.mark.asyncio
    async def test_code_index_summary_failure_classified_without_retry_noise(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """code_index.symbol_summary calls get one typed degradation warning."""

        async def mock_query(prompt: str, options: object) -> object:
            raise Exception("Claude Code returned an error result: success")
            yield

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider, ClaudeSDKProviderFailure

            provider = ClaudeLLMProvider(claude_config)

            with (
                patch("gobby.llm.claude.asyncio.sleep", new_callable=AsyncMock) as sleep,
                caplog.at_level(logging.WARNING, logger="gobby.llm.claude"),
                pytest.raises(
                    ClaudeSDKProviderFailure,
                    match=r"generate_text\[code_index\.symbol_summary\] provider degraded",
                ),
            ):
                await provider.generate_text(
                    "Summarize",
                    caller="code_index.symbol_summary",
                )

        sleep.assert_not_awaited()
        assert "provider degraded: Claude SDK returned error-result-success" in caplog.text
        assert "retrying" not in caplog.text
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)


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
