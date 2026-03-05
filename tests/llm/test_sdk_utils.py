"""Tests for shared SDK utilities in gobby.llm.sdk_utils."""

import pytest

from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    format_exception_group,
    parse_server_name,
    sanitize_error,
    truncate_additional_context,
)

pytestmark = pytest.mark.unit


class TestSanitizeError:
    def test_passes_through_normal_errors(self) -> None:
        assert sanitize_error(RuntimeError("Connection failed")) == "Connection failed"

    def test_hides_litellm_errors(self) -> None:
        assert sanitize_error(RuntimeError("litellm.BadRequestError: foo")) == (
            "An internal error occurred. Please try again."
        )

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
        eg = ExceptionGroup("errors", [RuntimeError("litellm.Error: bad")])
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
