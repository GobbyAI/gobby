"""Tests for shared SDK utilities in gobby.llm.sdk_utils."""

from unittest.mock import patch

import pytest

from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    _STATS_RE,
    compress_context,
    format_exception_group,
    parse_server_name,
    sanitize_error,
    truncate_additional_context,
)

pytestmark = pytest.mark.unit


class TestSanitizeError:
    def test_passes_through_normal_errors(self) -> None:
        assert sanitize_error(RuntimeError("Connection failed")) == "Connection failed"

    def test_hides_model_mapping_errors(self) -> None:
        assert sanitize_error(RuntimeError("model isn't mapped yet")) == (
            "An internal error occurred. Please try again."
        )

    def test_hides_custom_llm_provider_errors(self) -> None:
        assert sanitize_error(RuntimeError("custom_llm_provider required")) == (
            "An internal error occurred. Please try again."
        )


class TestParseServerName:
    def test_extracts_server_from_mcp_tool(self) -> None:
        assert parse_server_name("mcp__gobby-tasks__create_task") == "gobby-tasks"

    def test_extracts_server_with_multiple_separators(self) -> None:
        assert parse_server_name("mcp__my-server__do__thing") == "my-server"

    def test_returns_builtin_for_non_mcp(self) -> None:
        assert parse_server_name("code_execution") == "builtin"

    def test_returns_builtin_for_empty_string(self) -> None:
        assert parse_server_name("") == "builtin"

    def test_handles_mcp_prefix_only(self) -> None:
        assert parse_server_name("mcp__") == ""


class TestFormatExceptionGroup:
    def test_formats_single_exception(self) -> None:
        eg = ExceptionGroup("errors", [RuntimeError("boom")])
        assert format_exception_group(eg) == "boom"

    def test_formats_multiple_exceptions(self) -> None:
        eg = ExceptionGroup("errors", [RuntimeError("e1"), ValueError("e2")])
        assert format_exception_group(eg) == "e1; e2"

    def test_sanitizes_internal_errors(self) -> None:
        eg = ExceptionGroup("errors", [RuntimeError("model isn't mapped yet")])
        assert format_exception_group(eg) == "An internal error occurred. Please try again."


class TestAdditionalContextLimit:
    def test_limit_value(self) -> None:
        assert ADDITIONAL_CONTEXT_LIMIT == 9_950


class TestTruncateAdditionalContext:
    def test_short_text_unchanged(self) -> None:
        assert truncate_additional_context("hello") == "hello"

    def test_exact_limit_unchanged(self) -> None:
        text = "x" * ADDITIONAL_CONTEXT_LIMIT
        assert truncate_additional_context(text) == text

    def test_over_limit_truncated(self) -> None:
        text = "x" * (ADDITIONAL_CONTEXT_LIMIT + 100)
        result = truncate_additional_context(text)
        assert len(result) == ADDITIONAL_CONTEXT_LIMIT

    def test_empty_string(self) -> None:
        assert truncate_additional_context("") == ""


class TestStatsRegex:
    """Test the regex used to parse gsqz --stats stderr output."""

    def test_parses_standard_output(self) -> None:
        line = "[gsqz] strategy=prose:standard original=5000 compressed=3000 savings=40.0%"
        m = _STATS_RE.search(line)
        assert m is not None
        assert m.group("strategy") == "prose:standard"
        assert m.group("original") == "5000"
        assert m.group("compressed") == "3000"
        assert m.group("savings") == "40.0"

    def test_no_match_on_garbage(self) -> None:
        assert _STATS_RE.search("some random stderr") is None


class TestCompressContext:
    """Tests for compress_context — gsqz input integration."""

    def test_disabled_returns_original(self) -> None:
        text = "x" * 1000
        result, stats = compress_context(text, enabled=False)
        assert result == text
        assert stats is None

    def test_short_text_skipped(self) -> None:
        text = "short"
        result, stats = compress_context(text, enabled=True)
        assert result == text
        assert stats is None

    def test_no_binary_returns_original(self) -> None:
        text = "x" * 1000
        with patch("gobby.llm.sdk_utils._resolve_gsqz_bin", return_value=None):
            result, stats = compress_context(text)
        assert result == text
        assert stats is None

    def test_subprocess_success(self) -> None:
        text = "The quick brown fox jumps over the lazy dog. " * 30
        compressed = "quick brown fox jumps over lazy dog. " * 30
        stderr = "[gsqz] strategy=prose:standard original=1350 compressed=1080 savings=20.0%"

        with patch("gobby.llm.sdk_utils._resolve_gsqz_bin", return_value="/usr/bin/gsqz"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = compressed
                mock_run.return_value.stderr = stderr
                result, stats = compress_context(text)

        assert result == compressed
        assert stats is not None
        assert stats["original_chars"] == 1350
        assert stats["compressed_chars"] == 1080
        assert stats["savings_pct"] == 20.0
        assert stats["strategy"] == "prose:standard"

        # Verify subprocess was called correctly
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["/usr/bin/gsqz", "input", "--level", "standard", "--stats"]
        assert args[1]["input"] == text

    def test_subprocess_failure_returns_original(self) -> None:
        text = "x" * 1000
        with patch("gobby.llm.sdk_utils._resolve_gsqz_bin", return_value="/usr/bin/gsqz"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stdout = ""
                mock_run.return_value.stderr = "error"
                result, stats = compress_context(text)

        assert result == text
        assert stats is None

    def test_subprocess_timeout_returns_original(self) -> None:
        import subprocess

        text = "x" * 1000
        with patch("gobby.llm.sdk_utils._resolve_gsqz_bin", return_value="/usr/bin/gsqz"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gsqz", 5)):
                result, stats = compress_context(text)

        assert result == text
        assert stats is None

    def test_empty_stdout_returns_original(self) -> None:
        text = "x" * 1000
        with patch("gobby.llm.sdk_utils._resolve_gsqz_bin", return_value="/usr/bin/gsqz"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = ""
                mock_run.return_value.stderr = ""
                result, stats = compress_context(text)

        assert result == text
        assert stats is None

    def test_explicit_gsqz_bin_override(self) -> None:
        text = "x" * 1000
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "compressed"
            mock_run.return_value.stderr = ""
            result, stats = compress_context(text, gsqz_bin="/custom/gsqz")

        args = mock_run.call_args
        assert args[0][0][0] == "/custom/gsqz"

    def test_invalid_level_falls_back_to_standard(self) -> None:
        text = "x" * 1000
        with patch("gobby.llm.sdk_utils._resolve_gsqz_bin", return_value="/usr/bin/gsqz"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "compressed"
                mock_run.return_value.stderr = ""
                compress_context(text, level="invalid")

        args = mock_run.call_args
        assert args[0][0] == ["/usr/bin/gsqz", "input", "--level", "standard", "--stats"]

    def test_no_stats_in_stderr(self) -> None:
        text = "x" * 1000
        with patch("gobby.llm.sdk_utils._resolve_gsqz_bin", return_value="/usr/bin/gsqz"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "compressed output"
                mock_run.return_value.stderr = "some warning"
                result, stats = compress_context(text)

        assert result == "compressed output"
        assert stats is None
