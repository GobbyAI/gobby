"""Tests for shared SDK utilities in gobby.llm.sdk_utils."""

import logging

import pytest

from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    HANDOFF_SUMMARY_INJECT_BUDGET,
    format_exception_group,
    head_with_breadcrumb,
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

    def test_over_limit_appends_marker(self) -> None:
        text = "x" * (ADDITIONAL_CONTEXT_LIMIT + 100)
        result = truncate_additional_context(text)
        assert result.endswith("\n... [truncated]")

    def test_over_limit_logs_warning_with_contributor_sizes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        text = "x" * (ADDITIONAL_CONTEXT_LIMIT + 100)
        logger = logging.getLogger("tests.additional_context")

        with caplog.at_level(logging.WARNING, logger=logger.name):
            truncate_additional_context(
                text,
                contributor_sizes={"skills": 6000, "metadata": 4050},
                logger=logger,
            )

        assert "additionalContext truncated" in caplog.text
        assert f"aggregate_len={len(text)}" in caplog.text
        assert "contributors={'skills': 6000, 'metadata': 4050}" in caplog.text

    def test_empty_string(self) -> None:
        assert truncate_additional_context("") == ""


class TestHandoffSummaryBudget:
    def test_budget_below_aggregate_limit(self) -> None:
        # The inline summary head must leave room for other contributors and the
        # breadcrumb under the SDK's aggregate ceiling.
        assert HANDOFF_SUMMARY_INJECT_BUDGET < ADDITIONAL_CONTEXT_LIMIT


class TestHeadWithBreadcrumb:
    def test_under_budget_returned_verbatim(self) -> None:
        text = "short summary\n\nwith paragraphs"
        assert head_with_breadcrumb(text, budget=100, breadcrumb="MORE") == text

    def test_at_budget_returned_verbatim(self) -> None:
        text = "x" * 100
        assert head_with_breadcrumb(text, budget=100, breadcrumb="MORE") == text

    def test_over_budget_appends_breadcrumb(self) -> None:
        text = "para one\n\n" + ("y" * 300)
        result = head_with_breadcrumb(text, budget=50, breadcrumb="CALL get_handoff_context")
        assert result.endswith("CALL get_handoff_context")

    def test_over_budget_cuts_on_paragraph_boundary(self) -> None:
        head = "first paragraph kept intact"
        text = f"{head}\n\n" + ("z" * 500)
        result = head_with_breadcrumb(text, budget=len(head) + 5, breadcrumb="MORE")
        assert result.startswith(head)
        # The trailing run must not survive the clean cut.
        assert "z" not in result.replace("MORE", "")

    def test_over_budget_falls_back_to_newline_boundary(self) -> None:
        # No blank-line break in the back half -> fall back to last newline.
        text = "alpha\nbeta\ngamma\n" + ("q" * 200)
        result = head_with_breadcrumb(text, budget=16, breadcrumb="MORE")
        assert result.endswith("MORE")
        assert "q" not in result

    def test_no_boundary_hard_cut_at_budget(self) -> None:
        text = "a" * 500  # no newline anywhere
        budget = 40
        result = head_with_breadcrumb(text, budget=budget, breadcrumb="MORE")
        assert result == ("a" * budget) + "\n\nMORE"
