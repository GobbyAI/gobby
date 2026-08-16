"""Tests for shared SDK utilities in gobby.llm.sdk_utils."""

import logging
import re
from types import SimpleNamespace

import pytest

from gobby.hooks.event_handlers._session_start.handoff import _bound_handoff_summary
from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    HANDOFF_SUMMARY_INJECT_BUDGET,
    allocate_section_budget,
    format_exception_group,
    head_with_breadcrumb,
    parse_server_name,
    sanitize_error,
    split_markdown_sections,
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

    def test_over_limit_uses_contributor_sizes_to_preserve_small_parts(self) -> None:
        large_context = "x" * (ADDITIONAL_CONTEXT_LIMIT + 200)
        metadata = "session metadata survives"
        text = f"{large_context}\n\n{metadata}"

        result = truncate_additional_context(
            text,
            contributor_sizes={
                "response.context": len(large_context),
                "metadata": len(metadata),
            },
        )

        assert len(result) == ADDITIONAL_CONTEXT_LIMIT
        assert metadata in result
        assert result.endswith("\n... [truncated]")

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
        assert len(result) <= 50

    def test_over_budget_cuts_on_paragraph_boundary(self) -> None:
        head = "first paragraph kept intact"
        text = f"{head}\n\n" + ("z" * 500)
        budget = len(head) + len("\n\nMORE") + 5
        result = head_with_breadcrumb(text, budget=budget, breadcrumb="MORE")
        assert result.startswith(head)
        assert len(result) <= budget
        # The trailing run must not survive the clean cut.
        assert "z" not in result.replace("MORE", "")

    def test_over_budget_falls_back_to_newline_boundary(self) -> None:
        # No blank-line break in the back half -> fall back to last newline.
        text = "alpha\nbeta\ngamma\n" + ("q" * 200)
        result = head_with_breadcrumb(text, budget=16, breadcrumb="MORE")
        assert result.endswith("MORE")
        assert len(result) <= 16
        assert "q" not in result

    def test_no_boundary_hard_cut_at_budget(self) -> None:
        text = "a" * 500  # no newline anywhere
        budget = 40
        result = head_with_breadcrumb(text, budget=budget, breadcrumb="MORE")
        assert result == ("a" * (budget - len("\n\nMORE"))) + "\n\nMORE"
        assert len(result) == budget

    def test_tiny_budget_prioritizes_breadcrumb(self) -> None:
        result = head_with_breadcrumb("x" * 500, budget=3, breadcrumb="MORE")
        assert result == "MOR"
        assert len(result) == 3


class TestMarkdownSectionBudget:
    PRIORITIES = {
        "next steps": 10,
        "current state": 20,
        "unresolved errors": 30,
        "key technical decisions": 40,
        "problems encountered": 50,
        "what didn't work": 55,
        "files changed": 70,
        "what was accomplished": 80,
    }

    def test_split_sections_preserves_preamble_and_casefolds_titles(self) -> None:
        text = "Preamble.\n\n## Current STATE\nReady.\n## Files Changed\n- a.py\n"

        sections = split_markdown_sections(text)

        assert [section.title for section in sections] == ["", "current state", "files changed"]
        assert [section.order for section in sections] == [0, 1, 2]
        assert "".join(section.text for section in sections) == text

    def test_headingless_handoff_matches_previous_head_cut_bytes(self) -> None:
        summary = "# Summary\n\n" + ("paragraph contents.\n\n" * 600)
        parent = SimpleNamespace(seq_num=42)
        result = _bound_handoff_summary(summary, parent)
        breadcrumb_start = result.index("> ⚠️")
        breadcrumb = result[breadcrumb_start:]

        assert result == head_with_breadcrumb(
            summary,
            budget=HANDOFF_SUMMARY_INJECT_BUDGET,
            breadcrumb=breadcrumb,
        )

    def test_mandatory_sections_survive_combined_overflow(self) -> None:
        summary = "## Next Steps\n" + ("N" * 8_000) + "\n## Current State\n" + ("C" * 8_000)

        result = allocate_section_budget(
            split_markdown_sections(summary),
            self.PRIORITIES,
            1_000,
        )

        assert len(result.text) <= 1_000
        assert "## Next Steps" in result.text
        assert "## Current State" in result.text
        assert result.text.count("[section trimmed]") == 2
        assert result.omitted_titles == ("Next Steps", "Current State")

    def test_single_mandatory_section_gets_full_guarantee(self) -> None:
        next_steps = ("K" * 500) + "\n"
        summary = "## What Was Accomplished\n" + ("W" * 8_000) + "\n## Next Steps\n" + next_steps

        result = allocate_section_budget(
            split_markdown_sections(summary),
            self.PRIORITIES,
            700,
        )

        assert len(result.text) <= 700
        assert f"## Next Steps\n{next_steps}" in result.text
        assert "## What Was Accomplished" not in result.text

    def test_skewed_mandatory_split_does_not_starve_shorter_body(self) -> None:
        summary = "## Next Steps\n" + ("N" * 19_800) + "\n## Current State\n" + ("C" * 200)

        result = allocate_section_budget(
            split_markdown_sections(summary),
            self.PRIORITIES,
            1_000,
        )

        assert "C" * 200 in result.text
        assert "## Next Steps" in result.text
        assert "## Current State" in result.text
        assert len(result.text) <= 1_000

    @pytest.mark.parametrize("fence", ["```", "~~~"])
    def test_fenced_pseudo_headings_do_not_create_sections(self, fence: str) -> None:
        fenced_headings = "\n".join(f"## Pseudo {index}" for index in range(40))
        summary = (
            f"## Notes\n{fence}markdown\n"
            "## Next Steps\n"
            f"{fenced_headings}\n"
            f"{fence}\n"
            "## Current State\nReal state.\n"
        )

        sections = split_markdown_sections(summary)

        assert [section.title for section in sections] == ["", "notes", "current state"]
        assert "## Next Steps" in sections[1].body
        assert fenced_headings in sections[1].body

    def test_first_duplicate_mandatory_heading_owns_guarantee(self) -> None:
        summary = "## Next Steps\n" + ("FIRST" * 1_000) + "\n## Next Steps\n" + ("SECOND" * 1_000)

        result = allocate_section_budget(
            split_markdown_sections(summary),
            self.PRIORITIES,
            700,
        )

        assert "FIRST" in result.text
        assert "SECOND" not in result.text
        assert result.text.count("## Next Steps") == 1

    def test_many_real_sections_keep_mandatory_sections_near_tail(self) -> None:
        optional = "".join(f"## Section {index}\n" + ("x" * 300) + "\n" for index in range(31))
        summary = (
            optional
            + "## Next Steps\nDo the next exact thing.\n"
            + "## Current State\nThe current state is known.\n"
        )

        result = _bound_handoff_summary(summary, SimpleNamespace(seq_num=42))

        assert len(result) <= HANDOFF_SUMMARY_INJECT_BUDGET
        assert "## Next Steps\nDo the next exact thing.\n" in result
        assert "## Current State\nThe current state is known." in result

    def test_pathological_titles_keep_bounded_handoff_breadcrumb(self) -> None:
        long_sections = "".join(f"## {str(index) * 5_000}\nbody\n" for index in range(20))
        summary = (
            long_sections
            + "## Next Steps\nMandatory next step.\n"
            + "## Current State\nMandatory current state.\n"
        )

        result = _bound_handoff_summary(summary, SimpleNamespace(seq_num=42))

        assert len(result) <= HANDOFF_SUMMARY_INJECT_BUDGET
        assert "## Next Steps\nMandatory next step.\n" in result
        assert "## Current State\nMandatory current state." in result
        omission_line = next(line for line in result.splitlines() if line.startswith("Omitted"))
        assert len(omission_line) < 600
        assert re.search(r"\+\d+ more", omission_line)

    def test_mandatory_only_handoff_budgets_actual_breadcrumb(self) -> None:
        summary = "## Next Steps\n" + ("N" * 8_000) + "\n## Current State\n" + ("C" * 8_000)

        result = _bound_handoff_summary(summary, SimpleNamespace(seq_num=42))

        assert len(result) <= HANDOFF_SUMMARY_INJECT_BUDGET
        assert "## Next Steps" in result
        assert "## Current State" in result
        assert "Omitted sections: Next Steps, Current State" in result

    def test_oversized_next_steps_alone_keeps_heading_and_marker(self) -> None:
        summary = "## Next Steps\n" + ("N" * 10_000)

        result = _bound_handoff_summary(summary, SimpleNamespace(seq_num=42))

        assert len(result) <= HANDOFF_SUMMARY_INJECT_BUDGET
        assert result.startswith("## Next Steps\n")
        assert "[section trimmed]" in result
        assert "Omitted sections: Next Steps" in result
