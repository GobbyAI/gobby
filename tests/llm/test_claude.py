"""Tests for ClaudeLLMProvider edge cases and error handling.

Focuses on auth_mode selection, _is_transient_error classification,
_retry_async logic, _prepare_image_data, generate_json,
stream_with_mcp_tools, and describe_image.
"""

import logging
from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.llm.base import (
    LLMProviderError,
    VisionInputError,
    VisionProviderError,
    VisionProviderUnavailableError,
)

pytestmark = pytest.mark.unit

JSON_SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
    "additionalProperties": False,
}


# ─── Mock SDK classes ───────────────────────────────────────────────────


class MockAssistantMessage:
    def __init__(self, content: list) -> None:
        self.content = content


class MockResultMessage:
    def __init__(
        self,
        result: str | None = None,
        *,
        is_error: bool = False,
        subtype: str = "success",
        api_error_status: int | None = None,
        usage: dict[str, Any] | None = None,
        structured_output: object | None = None,
    ) -> None:
        self.result = result
        self.is_error = is_error
        self.subtype = subtype
        self.api_error_status = api_error_status
        self.usage = usage
        self.structured_output = structured_output


class MockRateLimitInfo:
    def __init__(
        self,
        *,
        status: str = "allowed",
        resets_at: int | None = None,
        rate_limit_type: str | None = None,
        utilization: float | None = None,
    ) -> None:
        self.status = status
        self.resets_at = resets_at
        self.rate_limit_type = rate_limit_type
        self.utilization = utilization


class MockRateLimitEvent:
    def __init__(self, rate_limit_info: MockRateLimitInfo) -> None:
        self.rate_limit_info = rate_limit_info


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


# Matches the ClaudeAgentOptions shape mypy expects at the execute_sdk_query
# boundary without coupling tests to the real SDK class (#14544: no ignores).
def _mock_agent_options() -> Any:
    return MockClaudeAgentOptions()


class MockExitCodeError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@pytest.fixture
def claude_config() -> DaemonConfig:
    """DaemonConfig with Claude provider."""
    return DaemonConfig()


@contextmanager
def mock_claude_sdk(mock_query_func: Any) -> Generator[None]:
    """Mock the Claude Agent SDK for testing."""
    with (
        patch("gobby.llm.claude_cli.shutil.which", return_value="/usr/bin/claude"),
        patch("os.path.exists", return_value=True),
        patch("os.access", return_value=True),
        patch("gobby.llm.claude_sdk.query", mock_query_func),
        patch("gobby.llm.claude_sdk.AssistantMessage", MockAssistantMessage),
        patch("gobby.llm.claude_sdk.ResultMessage", MockResultMessage),
        patch("gobby.llm.claude_sdk.TextBlock", MockTextBlock),
        patch("gobby.llm.claude_sdk.ToolUseBlock", MockToolUseBlock),
        patch("gobby.llm.claude_sdk.ClaudeAgentOptions", MockClaudeAgentOptions),
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
        from gobby.llm.claude_runtime import is_transient_error

        assert is_transient_error(Exception("401 Unauthorized")) is False
        assert is_transient_error(Exception("403 Forbidden")) is False
        assert is_transient_error(Exception("invalid_api_key")) is False
        assert is_transient_error(Exception("authentication failed")) is False
        assert is_transient_error(Exception("permission denied")) is False
        assert is_transient_error(Exception("not_found 404")) is False

    def test_transient_errors(self) -> None:
        """Timeout/server errors are transient."""
        from gobby.llm.claude_runtime import is_transient_error

        assert is_transient_error(Exception("timeout")) is True
        assert is_transient_error(Exception("rate limit exceeded")) is True
        assert is_transient_error(Exception("500 Internal Server Error")) is True
        assert is_transient_error(Exception("connection reset")) is True

    def test_sdk_not_found_is_permanent_but_connection_failure_is_transient(self) -> None:
        from claude_agent_sdk import CLIConnectionError, CLINotFoundError

        from gobby.llm.claude_runtime import is_transient_error

        assert is_transient_error(CLINotFoundError()) is False
        assert is_transient_error(CLIConnectionError("socket unavailable")) is True

    @pytest.mark.parametrize(
        "message",
        [
            "connection to localhost:4010 timed out",
            "connection reset after receiving 1404 bytes",
            "worker 4032 disconnected",
        ],
    )
    def test_status_code_substrings_do_not_suppress_retry(self, message: str) -> None:
        from gobby.llm.claude_runtime import is_transient_error

        assert is_transient_error(Exception(message)) is True

    def test_error_result_success_is_not_retried(self) -> None:
        """Known Claude SDK error-result-success failures are not retried noisily."""
        from gobby.llm.claude_runtime import is_transient_error

        error = Exception("Claude Code returned an error result: success")
        assert is_transient_error(error) is False

    def test_classified_provider_failures_are_not_transient(self) -> None:
        """Typed provider failures (incl. rate limits) fail fast without retry."""
        from gobby.llm.claude_errors import ClaudeSDKProviderFailure, ClaudeSDKRateLimited
        from gobby.llm.claude_runtime import is_transient_error

        assert is_transient_error(ClaudeSDKProviderFailure("x")) is False
        assert is_transient_error(ClaudeSDKRateLimited("x", retry_after=120.0)) is False

    def test_sigterm_exit_code_is_not_retried(self) -> None:
        """Claude SDK SIGTERM exits are shutdown cancellation, not transient LLM errors."""
        from gobby.llm.claude_runtime import is_sdk_sigterm_shutdown, is_transient_error

        attr_error = MockExitCodeError("Claude process exited", 143)
        message_error = Exception("Claude process exited with exit code 143")

        assert is_sdk_sigterm_shutdown(attr_error) is True
        assert is_sdk_sigterm_shutdown(message_error) is True
        assert is_transient_error(attr_error) is False
        assert is_transient_error(message_error) is False


# ─── _retry_async tests ─────────────────────────────────────────────────


class TestRetryAsync:
    """Tests for _retry_async method."""

    @pytest.mark.asyncio
    async def test_retry_rejects_zero_attempts_without_calling_operation(self) -> None:
        from gobby.llm.claude_runtime import retry_async

        operation = AsyncMock(return_value="unused")

        with pytest.raises(ValueError, match="max_retries must be at least 1"):
            await retry_async(operation, max_retries=0)

        operation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_succeeds_first_attempt(self, claude_config: DaemonConfig) -> None:
        """No retries needed when first attempt succeeds."""
        from gobby.llm.claude_runtime import retry_async

        async def success() -> str:
            return "ok"

        result = await retry_async(success, max_retries=3)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, claude_config: DaemonConfig) -> None:
        """Retries on transient errors with exponential backoff."""
        from gobby.llm.claude_runtime import retry_async

        call_count = 0

        async def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("timeout")
            return "ok"

        with patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock):
            result = await retry_async(flaky, max_retries=3, delay=0.01)

        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_permanent_error(self, claude_config: DaemonConfig) -> None:
        """Permanent errors raise immediately without retry."""
        from gobby.llm.claude_runtime import retry_async

        async def permanent_fail() -> str:
            raise Exception("401 Unauthorized")

        with pytest.raises(Exception, match="401 Unauthorized"):
            await retry_async(permanent_fail, max_retries=3)

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self, claude_config: DaemonConfig) -> None:
        """After max retries, the last exception is raised."""
        from gobby.llm.claude_runtime import retry_async

        async def always_fail() -> str:
            raise Exception("timeout again")

        with (
            patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(Exception, match="timeout again"),
        ):
            await retry_async(always_fail, max_retries=2, delay=0.01)

    @pytest.mark.asyncio
    async def test_retry_calls_on_retry_callback(self, claude_config: DaemonConfig) -> None:
        """on_retry callback is called on each retry attempt."""
        from gobby.llm.claude_runtime import retry_async

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

        with patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock):
            await retry_async(flaky, max_retries=3, delay=0.01, on_retry=on_retry)

        assert len(retry_calls) == 2
        assert retry_calls[0][0] == 0
        assert retry_calls[1][0] == 1


class TestExecuteSdkQuery:
    """Tests for SDK query execution failure classification."""

    @pytest.mark.asyncio
    async def test_failure_diagnostics_keep_only_bounded_stderr_tail(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.llm.claude_runtime import execute_sdk_query

        options = MockClaudeAgentOptions()

        async def failure_with_stderr() -> str:
            assert callable(options.stderr)
            for index in range(205):
                options.stderr(f"stderr-{index}")
            raise RuntimeError("401 Unauthorized")

        with (
            caplog.at_level(logging.ERROR, logger="gobby.llm.claude"),
            pytest.raises(RuntimeError, match="stderr-204") as exc_info,
        ):
            await execute_sdk_query(
                "generate_json",
                failure_with_stderr,
                options,
                logging.getLogger("gobby.llm.claude"),
                max_retries=1,
            )

        assert "stderr-0\n" not in str(exc_info.value)
        assert "stderr-4\n" not in str(exc_info.value)
        assert "stderr-5\n" in str(exc_info.value)
        assert "stderr-204" in caplog.text

    @pytest.mark.asyncio
    async def test_sigterm_exit_does_not_retry_or_warn(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exit code 143 is raised as cancellation without retry warning noise."""
        from gobby.llm.claude_runtime import (
            ClaudeSDKShutdownCancellation,
            execute_sdk_query,
        )

        options = MockClaudeAgentOptions()
        call_count = 0
        caplog.clear()

        async def terminated() -> str:
            nonlocal call_count
            call_count += 1
            raise MockExitCodeError("Claude process exited", 143)

        with (
            patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock) as sleep,
            caplog.at_level(logging.INFO, logger="gobby.llm.claude"),
            pytest.raises(ClaudeSDKShutdownCancellation, match="generate_json cancelled"),
        ):
            await execute_sdk_query(
                "generate_json",
                terminated,
                options,
                logging.getLogger("gobby.llm.claude"),
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

    @pytest.mark.asyncio
    async def test_planned_shutdown_error_result_does_not_retry_or_warn(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A reaped SDK child reports cancellation without provider warning noise."""
        from gobby.llm.claude_errors import ClaudeSDKProviderFailure
        from gobby.llm.claude_runtime import (
            ClaudeSDKShutdownCancellation,
            execute_sdk_query,
        )

        options = MockClaudeAgentOptions()
        call_count = 0
        caplog.clear()

        async def interrupted() -> str:
            nonlocal call_count
            call_count += 1
            raise ClaudeSDKProviderFailure(
                "generate_text[sessions.summary] provider degraded",
                classification="error_result",
                subtype="error_during_execution",
            )

        with (
            patch(
                "gobby.llm.claude_runtime.read_active_shutdown_intent",
                return_value=SimpleNamespace(stale=False, error=None),
            ),
            patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock) as sleep,
            caplog.at_level(logging.INFO, logger="gobby.llm.claude"),
            pytest.raises(
                ClaudeSDKShutdownCancellation,
                match=r"generate_text\[sessions\.summary\] cancelled",
            ),
        ):
            await execute_sdk_query(
                "generate_text[sessions.summary]",
                interrupted,
                options,
                logging.getLogger("gobby.llm.claude"),
                max_retries=3,
                retry_delay=0.01,
            )

        sleep.assert_not_awaited()
        assert call_count == 1
        assert "provider degraded" not in caplog.text
        assert not any(
            record.name == "gobby.llm.claude" and record.levelno >= logging.WARNING
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_execution_error_without_shutdown_marker_warns_once(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.llm.claude_errors import ClaudeSDKProviderFailure
        from gobby.llm.claude_runtime import execute_sdk_query

        options = MockClaudeAgentOptions()

        async def failed() -> str:
            raise ClaudeSDKProviderFailure(
                "generate_text[sessions.summary] provider degraded",
                classification="error_result",
                subtype="error_during_execution",
            )

        with (
            patch(
                "gobby.llm.claude_runtime.read_active_shutdown_intent",
                return_value=None,
            ),
            caplog.at_level(logging.WARNING, logger="gobby.llm.claude"),
            pytest.raises(ClaudeSDKProviderFailure),
        ):
            await execute_sdk_query(
                "generate_text[sessions.summary]",
                failed,
                options,
                logging.getLogger("gobby.llm.claude"),
                max_retries=3,
                retry_delay=0.01,
            )

        warnings = [
            record
            for record in caplog.records
            if record.name == "gobby.llm.claude" and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "provider degraded" in warnings[0].message

    @pytest.mark.asyncio
    async def test_non_sigterm_exception_group_gets_diagnostics(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-shutdown ExceptionGroups should follow the normal diagnostics path."""
        from gobby.llm.claude_runtime import execute_sdk_query

        options = MockClaudeAgentOptions()
        caplog.clear()

        async def grouped_failure() -> str:
            raise ExceptionGroup("sdk failure", [RuntimeError("boom")])

        with (
            patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR, logger="gobby.llm.claude"),
            pytest.raises(RuntimeError, match="generate_json failed"),
        ):
            await execute_sdk_query(
                "generate_json",
                grouped_failure,
                options,
                logging.getLogger("gobby.llm.claude"),
                max_retries=1,
                retry_delay=0.01,
            )

        assert "generate_json failed" in caplog.text

    @pytest.mark.asyncio
    async def test_connectivity_provider_failure_logs_debug_not_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DNS/network outages demote provider-degraded logging to DEBUG."""
        from gobby.llm.claude_errors import ClaudeSDKProviderFailure
        from gobby.llm.claude_runtime import execute_sdk_query

        options = _mock_agent_options()

        async def dns_failure() -> str:
            raise ClaudeSDKProviderFailure(
                "generate_text[code_index.symbol_summary] provider degraded: "
                "Claude SDK returned error result (subtype=error_during_execution): "
                "getaddrinfo ENOTFOUND api.anthropic.com",
                classification="error_result",
                subtype="error_during_execution",
            )

        with (
            caplog.at_level(logging.DEBUG, logger="gobby.llm.claude"),
            pytest.raises(ClaudeSDKProviderFailure),
        ):
            await execute_sdk_query(
                "generate_text[code_index.symbol_summary]",
                dns_failure,
                options,
                logging.getLogger("gobby.llm.claude"),
                max_retries=1,
                retry_delay=0.01,
            )

        records = [record for record in caplog.records if record.name == "gobby.llm.claude"]
        assert not any(record.levelno >= logging.WARNING for record in records)
        assert any(
            record.levelno == logging.DEBUG and "ENOTFOUND" in record.message for record in records
        )

    @pytest.mark.asyncio
    async def test_non_connectivity_provider_failure_still_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.llm.claude_errors import ClaudeSDKProviderFailure
        from gobby.llm.claude_runtime import execute_sdk_query

        options = _mock_agent_options()

        async def other_failure() -> str:
            raise ClaudeSDKProviderFailure(
                "generate_text provider degraded: Claude SDK returned error result "
                "(subtype=error_during_execution): internal provider error",
                classification="error_result",
                subtype="error_during_execution",
            )

        with (
            caplog.at_level(logging.DEBUG, logger="gobby.llm.claude"),
            pytest.raises(ClaudeSDKProviderFailure),
        ):
            await execute_sdk_query(
                "generate_text",
                other_failure,
                options,
                logging.getLogger("gobby.llm.claude"),
                max_retries=1,
                retry_delay=0.01,
            )

        warnings = [
            record
            for record in caplog.records
            if record.name == "gobby.llm.claude" and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_connectivity_error_result_success_logs_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Raw connectivity failures wrapped as error-result-success also demote."""
        from gobby.llm.claude_errors import ClaudeSDKProviderFailure
        from gobby.llm.claude_runtime import execute_sdk_query

        options = _mock_agent_options()

        async def socket_failure() -> str:
            raise RuntimeError(
                "Claude Code returned an error result: success "
                "(getaddrinfo ENOTFOUND api.anthropic.com)"
            )

        with (
            patch(
                "gobby.llm.claude_runtime.read_active_shutdown_intent",
                return_value=None,
            ),
            caplog.at_level(logging.DEBUG, logger="gobby.llm.claude"),
            pytest.raises(ClaudeSDKProviderFailure),
        ):
            await execute_sdk_query(
                "generate_text",
                socket_failure,
                options,
                logging.getLogger("gobby.llm.claude"),
                max_retries=1,
                retry_delay=0.01,
            )

        records = [record for record in caplog.records if record.name == "gobby.llm.claude"]
        assert not any(record.levelno >= logging.WARNING for record in records)


# ─── _prepare_image_data tests ──────────────────────────────────────────


class TestPrepareImageData:
    """Tests for _prepare_image_data."""

    async def test_image_not_found(self, claude_config: DaemonConfig) -> None:
        """Raises a structured input error when the image doesn't exist."""
        from gobby.llm.image_payloads import prepare_image_data

        with pytest.raises(VisionInputError, match="not found"):
            await prepare_image_data("/nonexistent/image.png")

    async def test_valid_image(self, claude_config: DaemonConfig, tmp_path: Path) -> None:
        """Returns (base64, mime_type) for valid image."""
        from gobby.llm.image_payloads import prepare_image_data

        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG\r\n")

        result = await prepare_image_data(str(img_path))
        assert isinstance(result, tuple)
        assert len(result) == 4
        assert result[1] == "image/png"

    async def test_unknown_mime_defaults_to_png(
        self, claude_config: DaemonConfig, tmp_path: Path
    ) -> None:
        """Unknown extensions default to image/png."""
        from gobby.llm.image_payloads import prepare_image_data

        img_path = tmp_path / "test.xyz"
        img_path.write_bytes(b"data")

        result = await prepare_image_data(str(img_path))
        assert isinstance(result, tuple)
        assert result[1] == "image/png"

    async def test_read_error(self, claude_config: DaemonConfig, tmp_path: Path) -> None:
        """Raises a structured input error when the file can't be read."""
        from gobby.llm.image_payloads import prepare_image_data

        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"data")

        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with pytest.raises(VisionInputError, match="Failed to read") as exc_info:
                await prepare_image_data(str(img_path))

        assert isinstance(exc_info.value.__cause__, PermissionError)


# ─── generate_json tests ────────────────────────────────────────────────


class TestGenerateText:
    """Tests for text-generation SDK option plumbing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [None, "", " \t\n"])
    async def test_generate_text_sdk_rejects_blank_content(
        self, claude_config: DaemonConfig, content: str | None
    ) -> None:
        async def mock_query(prompt: str, options: object) -> object:
            yield MockResultMessage(content)

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with pytest.raises(
                LLMProviderError,
                match=r"Claude generate_text\[unit-test\] returned blank content",
            ):
                await provider.generate_text_result("Generate text", caller="unit-test")

    @pytest.mark.asyncio
    async def test_generate_text_sdk_passes_reasoning_effort(
        self, claude_config: DaemonConfig
    ) -> None:
        captured_kwargs: list[dict[str, object]] = []

        async def mock_query(prompt: str, options: Any) -> object:
            captured_kwargs.append(options.kwargs)
            yield MockAssistantMessage([MockTextBlock("reply")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text_result("Generate text", reasoning_effort="xhigh")

        assert result.text == "reply"
        assert result.applied_reasoning_effort == "xhigh"
        assert captured_kwargs[0]["effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_generate_text_sdk_uses_fixed_cwd_and_disables_auto_memory(
        self, claude_config: DaemonConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One-shot textgen shares one stable cwd so Claude materializes a
        single ~/.claude/projects slug, and auto-memory stays off (#20450)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        captured_kwargs: list[dict[str, object]] = []

        async def mock_query(prompt: str, options: Any) -> AsyncIterator[object]:
            captured_kwargs.append(options.kwargs)
            yield MockAssistantMessage([MockTextBlock("reply")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            first = await provider.generate_text_result("Generate text")
            second = await provider.generate_text_result("Generate text")

        assert first.text == "reply"
        assert second.text == "reply"
        assert captured_kwargs[0]["cwd"] == captured_kwargs[1]["cwd"]
        assert "gobby-textgen-" not in str(captured_kwargs[0]["cwd"])
        env = captured_kwargs[0]["env"]
        assert isinstance(env, dict)
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"

    @pytest.mark.asyncio
    async def test_generate_text_sdk_uses_turn_headroom_with_tools_disabled(
        self, claude_config: DaemonConfig
    ) -> None:
        # gobby-#17698: the feature text-gen path must not be starved at
        # max_turns=1 — the Claude Agent SDK raises "Reached maximum number of
        # turns (1)" on reasoning/continuation-heavy prompts instead of returning
        # text. Guard bounded headroom (>1) AND that tools stay disabled so the
        # extra turns can never become an agent action-loop.
        captured_kwargs: list[dict[str, object]] = []

        async def mock_query(prompt: str, options: Any) -> object:
            captured_kwargs.append(options.kwargs)
            yield MockAssistantMessage([MockTextBlock("reply")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text_result("Generate a long page")

        assert result.text == "reply"
        assert captured_kwargs[0]["max_turns"] > 1
        assert captured_kwargs[0]["tools"] == []
        assert captured_kwargs[0]["allowed_tools"] == []

    @pytest.mark.asyncio
    async def test_generate_text_sdk_does_not_slice_complete_result_by_character_estimate(
        self, claude_config: DaemonConfig
    ) -> None:
        async def mock_query(prompt: str, options: object) -> object:
            yield MockResultMessage("complete reply")

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text_result("Generate text", max_tokens=1)

        assert result.text == "complete reply"

    @pytest.mark.asyncio
    async def test_generate_text_retry_discards_failed_attempt_usage(
        self, claude_config: DaemonConfig
    ) -> None:
        attempts = 0

        async def mock_query(prompt: str, options: object) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                yield MockResultMessage(
                    "partial",
                    usage={"input_tokens": 10, "output_tokens": 5},
                )
                raise RuntimeError("transient network reset")
            yield MockResultMessage("done")

        with (
            mock_claude_sdk(mock_query),
            patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock),
        ):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text_result("Generate text")

        assert attempts == 2
        assert result.text == "done"
        assert result.usage is None

    @pytest.mark.asyncio
    async def test_generate_text_sdk_omits_reasoning_effort_when_auto_or_unset(
        self, claude_config: DaemonConfig
    ) -> None:
        captured_kwargs: list[dict[str, object]] = []
        replies = iter(["auto reply", "default reply"])

        async def mock_query(prompt: str, options: Any) -> object:
            captured_kwargs.append(options.kwargs)
            yield MockAssistantMessage([MockTextBlock(next(replies))])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            auto_result = await provider.generate_text_result(
                "Generate text",
                reasoning_effort="auto",
            )
            unset_result = await provider.generate_text_result("Generate text")

        assert auto_result.text == "auto reply"
        assert unset_result.text == "default reply"
        assert auto_result.applied_reasoning_effort is None
        assert unset_result.applied_reasoning_effort is None
        assert ["effort" in kwargs for kwargs in captured_kwargs] == [False, False]

    @pytest.mark.asyncio
    async def test_generate_text_sdk_passes_unverified_reasoning_effort(
        self, claude_config: DaemonConfig
    ) -> None:
        captured_kwargs: list[dict[str, object]] = []

        async def mock_query(prompt: str, options: Any) -> object:
            captured_kwargs.append(options.kwargs)
            yield MockAssistantMessage([MockTextBlock("unused")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_text_result(
                "Generate text", reasoning_effort="extreme"
            )

        assert result.applied_reasoning_effort == "extreme"
        assert captured_kwargs[0]["effort"] == "extreme"


class TestGenerateAgentic:
    """Tests for agentic SDK option plumbing."""

    @pytest.mark.asyncio
    async def test_generate_agentic_defaults_to_readonly_tools(
        self, claude_config: DaemonConfig
    ) -> None:
        captured_kwargs: list[dict[str, object]] = []

        async def mock_query(prompt: str, options: Any) -> object:
            captured_kwargs.append(options.kwargs)
            yield MockAssistantMessage([MockTextBlock("done")])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_agentic(
                system_prompt=None,
                prompt="Investigate",
                project_path="/repo",
            )

        assert result.text == "done"
        assert captured_kwargs[0]["allowed_tools"] == ["Read", "Grep", "Glob"]
        assert "Bash" not in captured_kwargs[0]["allowed_tools"]

    @pytest.mark.asyncio
    async def test_generate_agentic_retry_discards_failed_attempt_counters(
        self, claude_config: DaemonConfig
    ) -> None:
        attempts = 0

        async def mock_query(prompt: str, options: Any) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                yield MockAssistantMessage(
                    [MockTextBlock("partial"), MockToolUseBlock("tool-1", "Read", {})]
                )
                raise RuntimeError("transient network reset")
            yield MockAssistantMessage([MockTextBlock("done")])

        async def retry_once(
            _operation: str,
            query_fn: Any,
            _options: object,
            _logger: logging.Logger,
            **_kwargs: object,
        ) -> str:
            try:
                await query_fn()
            except RuntimeError:
                pass
            return await query_fn()

        with (
            mock_claude_sdk(mock_query),
            patch("gobby.llm.claude_sdk.execute_sdk_query", side_effect=retry_once),
        ):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_agentic(
                system_prompt=None,
                prompt="Investigate",
                project_path="/repo",
            )

        assert attempts == 2
        assert result.text == "done"
        assert result.turns == 1
        assert result.tool_use_count == 0
        assert result.tools == {}


class TestGenerateJson:
    """Tests for generate_json method."""

    @pytest.mark.asyncio
    async def test_generate_json_no_backend(self, claude_config: DaemonConfig) -> None:
        """Raises RuntimeError when CLI not available."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            with pytest.raises(RuntimeError, match="unavailable"):
                await provider.generate_json("Generate JSON", json_schema=JSON_SCHEMA)

    @pytest.mark.asyncio
    async def test_generate_json_sdk_parses_json(self, claude_config: DaemonConfig) -> None:
        """SDK path returns the ResultMessage structured output."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockAssistantMessage([MockTextBlock("ignored text")])
            yield MockResultMessage(structured_output={"key": "value"})

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_json("Generate JSON", json_schema=JSON_SCHEMA)

            assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_generate_json_sdk_disables_filesystem_settings_with_empty_list(
        self, claude_config: DaemonConfig
    ) -> None:
        """Internal SDK calls isolate settings with setting_sources=[]."""
        captured_sources: list[list[str] | None] = []

        async def mock_query(prompt: str, options: object) -> object:
            captured_sources.append(options.setting_sources)
            yield MockResultMessage(structured_output={"isolated": True})

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_json("Generate JSON", json_schema=JSON_SCHEMA)

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
            captured["effort"] = options.kwargs.get("effort")
            captured["output_format"] = options.output_format
            captured["max_turns"] = options.max_turns
            captured["tools"] = options.tools
            captured["allowed_tools"] = options.allowed_tools
            captured["mcp_servers"] = options.mcp_servers
            yield MockResultMessage(structured_output={"entities": []})

        async def execute_sdk_query(
            operation: str,
            query_fn: Any,
            options: object,
            logger: logging.Logger,
            **kwargs: object,
        ) -> dict[str, Any]:
            captured["operation"] = operation
            return await query_fn()

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with patch("gobby.llm.claude_sdk.execute_sdk_query", side_effect=execute_sdk_query):
                result = await provider.generate_json(
                    "rendered entity extraction prompt",
                    "strict entity extraction system prompt",
                    "haiku",
                    json_schema=JSON_SCHEMA,
                    reasoning_effort="high",
                    caller="memory.kg.extract_entities",
                )

        assert result == {"entities": []}
        assert captured["prompt"] == "rendered entity extraction prompt"
        assert captured["system_prompt"] == "strict entity extraction system prompt"
        assert captured["effort"] == "high"
        assert captured["output_format"] == {"type": "json_schema", "schema": JSON_SCHEMA}
        assert captured["max_turns"] == 8
        assert captured["tools"] == []
        assert captured["allowed_tools"] == []
        assert captured["mcp_servers"] == {}
        assert captured["operation"] == "generate_json[memory.kg.extract_entities]"
        assert "applied_reasoning_effort" not in result

    @pytest.mark.asyncio
    async def test_generate_json_sdk_omits_reasoning_effort_when_auto(
        self, claude_config: DaemonConfig
    ) -> None:
        captured_kwargs: list[dict[str, object]] = []

        async def mock_query(prompt: str, options: Any) -> object:
            captured_kwargs.append(options.kwargs)
            yield MockResultMessage(structured_output={"ok": True})

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            result = await provider.generate_json(
                "Generate JSON",
                json_schema=JSON_SCHEMA,
                reasoning_effort="auto",
            )

        assert result == {"ok": True}
        assert "effort" not in captured_kwargs[0]

    @pytest.mark.asyncio
    async def test_generate_json_sdk_missing_structured_output(
        self, claude_config: DaemonConfig
    ) -> None:
        """SDK path rejects a result without structured output."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockAssistantMessage([MockTextBlock('{"key": "ignored"}')])
            yield MockResultMessage()

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)

            with pytest.raises(ValueError, match="no object structured output"):
                await provider.generate_json("Generate JSON", json_schema=JSON_SCHEMA)

    @pytest.mark.asyncio
    async def test_generate_json_sdk_rejects_non_object_structured_output(
        self, claude_config: DaemonConfig
    ) -> None:
        """SDK path rejects non-object structured output."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockResultMessage(structured_output=[])

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with pytest.raises(ValueError, match="no object structured output"):
                await provider.generate_json("Generate JSON", json_schema=JSON_SCHEMA)

    @pytest.mark.asyncio
    async def test_generate_json_sdk_classifies_error_result_success(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known SDK error-result-success failures log one warning and no traceback."""

        async def mock_query(prompt: str, options: object) -> object:
            raise Exception("Claude Code returned an error result: success")
            yield

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider
            from gobby.llm.claude_errors import ClaudeSDKProviderFailure

            provider = ClaudeLLMProvider(claude_config)

            with (
                patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock) as sleep,
                caplog.at_level(logging.WARNING, logger="gobby.llm.claude"),
                pytest.raises(ClaudeSDKProviderFailure, match="generate_json provider degraded"),
            ):
                await provider.generate_json("Generate JSON", json_schema=JSON_SCHEMA)

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
            from gobby.llm.claude import ClaudeLLMProvider
            from gobby.llm.claude_errors import ClaudeSDKProviderFailure

            provider = ClaudeLLMProvider(claude_config)

            with (
                patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock) as sleep,
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

    @pytest.mark.asyncio
    async def test_rate_limit_result_message_classified_with_reset_window(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rejected RateLimitEvent + error result raises a typed rate-limit failure."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockRateLimitEvent(
                MockRateLimitInfo(
                    status="rejected",
                    resets_at=4102444800,  # year 2100 — retry_after stays positive
                    rate_limit_type="five_hour",
                )
            )
            yield MockResultMessage(
                result="Claude AI usage limit reached",
                is_error=True,
                subtype="success",
            )

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider
            from gobby.llm.claude_errors import ClaudeSDKRateLimited

            provider = ClaudeLLMProvider(claude_config)

            with (
                patch("gobby.llm.claude_runtime.asyncio.sleep", new_callable=AsyncMock) as sleep,
                caplog.at_level(logging.WARNING, logger="gobby.llm.claude"),
                pytest.raises(ClaudeSDKRateLimited) as excinfo,
            ):
                await provider.generate_text("Summarize", caller="wiki")

        sleep.assert_not_awaited()
        assert excinfo.value.classification == "rate_limited"
        assert excinfo.value.retry_after is not None and excinfo.value.retry_after > 0.0
        assert "provider rate-limited" in caplog.text
        assert "window=five_hour" in caplog.text
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)

    @pytest.mark.asyncio
    async def test_api_429_result_message_classified_as_rate_limit(
        self, claude_config: DaemonConfig
    ) -> None:
        """An error result carrying api_error_status=429 is a rate limit even without body."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockResultMessage(result="", is_error=True, api_error_status=429)

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider
            from gobby.llm.claude_errors import ClaudeSDKRateLimited

            provider = ClaudeLLMProvider(claude_config)

            with pytest.raises(ClaudeSDKRateLimited, match="api_error_status=429"):
                await provider.generate_text("Summarize", caller="wiki")

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_result_preserves_body(
        self, claude_config: DaemonConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-rate-limit error result surfaces its real body, not the opaque subtype."""

        async def mock_query(prompt: str, options: object) -> object:
            yield MockResultMessage(
                result="model refused: prompt too long",
                is_error=True,
                subtype="success",
            )

        with mock_claude_sdk(mock_query):
            from gobby.llm.claude import ClaudeLLMProvider
            from gobby.llm.claude_errors import (
                ClaudeSDKProviderFailure,
                ClaudeSDKRateLimited,
            )

            provider = ClaudeLLMProvider(claude_config)

            with (
                caplog.at_level(logging.WARNING, logger="gobby.llm.claude"),
                pytest.raises(ClaudeSDKProviderFailure) as excinfo,
            ):
                await provider.generate_text("Summarize", caller="wiki")

        assert not isinstance(excinfo.value, ClaudeSDKRateLimited)
        assert excinfo.value.classification == "error_result"
        assert "model refused: prompt too long" in str(excinfo.value)
        assert "model refused: prompt too long" in caplog.text


# ─── describe_image tests ───────────────────────────────────────────────


class TestDescribeImage:
    """Tests for describe_image method."""

    @pytest.mark.asyncio
    async def test_describe_image_sdk_no_cli(self, claude_config: DaemonConfig) -> None:
        """Raises a structured provider error when CLI is not found."""
        with patch("gobby.llm.claude_cli.shutil.which", return_value=None):
            from gobby.llm.claude import ClaudeLLMProvider

            provider = ClaudeLLMProvider(claude_config)
            with pytest.raises(VisionProviderUnavailableError, match="Claude CLI not found"):
                await provider.describe_image("/path/to/image.png")

    @pytest.mark.asyncio
    async def test_describe_image_sdk_missing_file_raises_input_error(
        self, claude_config: DaemonConfig
    ) -> None:
        from gobby.llm.claude import ClaudeLLMProvider

        provider = ClaudeLLMProvider(claude_config)
        provider._sdk_client._verify_cli_path = AsyncMock(return_value="/bin/claude")

        with pytest.raises(VisionInputError, match="not found"):
            await provider.describe_image("/missing/image.png")

    @pytest.mark.asyncio
    async def test_describe_image_sdk_unreadable_file_raises_input_error(
        self, claude_config: DaemonConfig, tmp_path: Path
    ) -> None:
        from gobby.llm.claude import ClaudeLLMProvider

        image_path = tmp_path / "image.png"
        image_path.write_bytes(b"image")
        provider = ClaudeLLMProvider(claude_config)
        provider._sdk_client._verify_cli_path = AsyncMock(return_value="/bin/claude")

        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with pytest.raises(VisionInputError, match="Failed to read"):
                await provider.describe_image(str(image_path))

    @pytest.mark.asyncio
    async def test_describe_image_sdk_failure_raises_provider_error(
        self, claude_config: DaemonConfig, tmp_path: Path
    ) -> None:
        from gobby.llm.claude import ClaudeLLMProvider

        image_path = tmp_path / "image.png"
        image_path.write_bytes(b"image")
        provider = ClaudeLLMProvider(claude_config)
        provider._sdk_client._verify_cli_path = AsyncMock(return_value="/bin/claude")

        with patch(
            "gobby.llm.claude_sdk.execute_sdk_query",
            new=AsyncMock(side_effect=RuntimeError("SDK failed")),
        ):
            with pytest.raises(VisionProviderError, match="SDK failed") as exc_info:
                await provider.describe_image(str(image_path))

        assert isinstance(exc_info.value.__cause__, RuntimeError)

    @pytest.mark.asyncio
    async def test_describe_image_sdk_preserves_successful_output(
        self, claude_config: DaemonConfig, tmp_path: Path
    ) -> None:
        from gobby.llm.claude import ClaudeLLMProvider

        image_path = tmp_path / "image.png"
        image_path.write_bytes(b"image")
        provider = ClaudeLLMProvider(claude_config)
        provider._sdk_client._verify_cli_path = AsyncMock(return_value="/bin/claude")

        with patch(
            "gobby.llm.claude_sdk.execute_sdk_query",
            new=AsyncMock(return_value="A blue diagram"),
        ):
            result = await provider.describe_image(str(image_path))

        assert result == "A blue diagram"


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


class TestClassifyResultMessage:
    """Unit tests for Claude SDK error-result classification."""

    def test_usage_limit_body_marks_rate_limit_and_parses_reset(self) -> None:
        from gobby.llm.claude_errors import ClaudeSDKRateLimited, classify_result_message

        message = MockResultMessage(
            result="Claude AI usage limit reached|1700001000",
            is_error=True,
            subtype="success",
        )
        failure = classify_result_message(message, "generate_text", now=1_700_000_000.0)

        assert isinstance(failure, ClaudeSDKRateLimited)
        assert failure.reset_at == 1_700_001_000.0
        assert failure.retry_after == 1000.0

    def test_rate_limit_event_reset_is_preferred_over_body(self) -> None:
        from gobby.llm.claude_errors import ClaudeSDKRateLimited, classify_result_message

        message = MockResultMessage(
            result="Claude AI usage limit reached|9999",
            is_error=True,
            subtype="success",
        )
        info = MockRateLimitInfo(status="rejected", resets_at=5000, rate_limit_type="seven_day")
        failure = classify_result_message(
            message, "generate_text", now=1000.0, rate_limit_info=info
        )

        assert isinstance(failure, ClaudeSDKRateLimited)
        assert failure.reset_at == 5000.0
        assert failure.retry_after == 4000.0
        assert "window=seven_day" in str(failure)

    def test_past_reset_yields_no_retry_after(self) -> None:
        from gobby.llm.claude_errors import ClaudeSDKRateLimited, classify_result_message

        message = MockResultMessage(
            result="Claude AI usage limit reached|1699999000",
            is_error=True,
            subtype="success",
        )
        failure = classify_result_message(message, "generate_text", now=1_700_000_000.0)

        assert isinstance(failure, ClaudeSDKRateLimited)
        assert failure.retry_after is None

    def test_generic_error_result_is_not_rate_limited(self) -> None:
        from gobby.llm.claude_errors import (
            ClaudeSDKProviderFailure,
            ClaudeSDKRateLimited,
            classify_result_message,
        )

        message = MockResultMessage(
            result="context deadline exceeded",
            is_error=True,
            subtype="success",
        )
        failure = classify_result_message(message, "generate_json", now=1000.0)

        assert isinstance(failure, ClaudeSDKProviderFailure)
        assert not isinstance(failure, ClaudeSDKRateLimited)
        assert failure.classification == "error_result"
        assert failure.subtype == "success"
        assert "context deadline exceeded" in str(failure)

    def test_max_turns_subtype_is_budget_exhaustion(self) -> None:
        from gobby.llm.claude_errors import ClaudeSDKMaxTurns, classify_result_message

        message = MockResultMessage(result=None, is_error=True, subtype="error_max_turns")
        failure = classify_result_message(message, "generate_agentic")

        assert isinstance(failure, ClaudeSDKMaxTurns)
        assert failure.classification == "max_turns"
        assert "provider degraded" not in str(failure)
