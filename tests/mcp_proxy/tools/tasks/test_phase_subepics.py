"""Tests for phase subepic support in task expansion."""

import pytest

from gobby.mcp_proxy.tools.tasks._expansion import (
    _extract_phase_from_title,
    _extract_phase_titles,
    _get_subtask_phase,
)

pytestmark = pytest.mark.unit


class TestExtractPhaseFromTitle:
    def test_extracts_from_tdd_test_title(self) -> None:
        subtask = {"title": "[TEST] Phase 2: Write failing tests"}
        assert _extract_phase_from_title(subtask) == 2

    def test_extracts_from_tdd_ref_title(self) -> None:
        subtask = {"title": "[REF] Phase 3: Refactor with green tests"}
        assert _extract_phase_from_title(subtask) == 3

    def test_returns_none_for_plain_title(self) -> None:
        subtask = {"title": "Add user model"}
        assert _extract_phase_from_title(subtask) is None

    def test_returns_none_for_empty_title(self) -> None:
        subtask = {"title": ""}
        assert _extract_phase_from_title(subtask) is None

    def test_returns_none_for_missing_title(self) -> None:
        subtask = {}
        assert _extract_phase_from_title(subtask) is None


class TestGetSubtaskPhase:
    def test_prefers_description_over_title(self) -> None:
        """Phase from description (Plan Section) takes precedence."""
        subtask = {
            "title": "[TEST] Phase 2: Write failing tests",
            "description": "### Plan Section: 1.1\n\nDetails",
        }
        assert _get_subtask_phase(subtask) == 1

    def test_falls_back_to_title(self) -> None:
        """When no Plan Section in description, extract from title."""
        subtask = {
            "title": "[TEST] Phase 3: Write failing tests",
            "description": "Write tests for phase 3 tasks.",
        }
        assert _get_subtask_phase(subtask) == 3

    def test_returns_zero_for_unphased(self) -> None:
        subtask = {"title": "Fix a bug", "description": "Just fix it"}
        assert _get_subtask_phase(subtask) == 0

    def test_handles_missing_description(self) -> None:
        """Returns 0 when description key is absent."""
        subtask = {"title": "Fix a bug"}
        assert _get_subtask_phase(subtask) == 0


class TestExtractPhaseTitles:
    def test_extracts_multiple_phases(self) -> None:
        description = """# Multi-Provider Web Chat

## Phase 1: Wire SessionsTab to Chat Area

Some description.

## Phase 2: Chat Area Mode UX

More description.

## Phase 3: Resume Strategy Pattern

Even more."""
        titles = _extract_phase_titles(description)
        assert titles == {
            1: "Wire SessionsTab to Chat Area",
            2: "Chat Area Mode UX",
            3: "Resume Strategy Pattern",
        }

    def test_handles_no_phases(self) -> None:
        description = "# Simple Epic\n\nJust one task."
        assert _extract_phase_titles(description) == {}

    def test_strips_whitespace(self) -> None:
        description = "## Phase 1:   Spaced Title   \n"
        titles = _extract_phase_titles(description)
        assert titles[1] == "Spaced Title"

    def test_handles_phase_with_extra_content_on_line(self) -> None:
        description = "## Phase 5: Gemini Web Chat + Provider Picker + Personas\n"
        titles = _extract_phase_titles(description)
        assert titles[5] == "Gemini Web Chat + Provider Picker + Personas"

    def test_accepts_em_dash_separator(self) -> None:
        description = (
            "## Phase 0 \u2014 Prerequisites\n\n"
            "## Phase 1 \u2014 Sandbox test harness\n\n"
            "## Phase 2 \u2014 Rust `ghook` binary\n"
        )
        titles = _extract_phase_titles(description)
        assert titles == {
            0: "Prerequisites",
            1: "Sandbox test harness",
            2: "Rust `ghook` binary",
        }

    def test_accepts_en_dash_separator(self) -> None:
        description = "## Phase 2 \u2013 Implementation\n"
        titles = _extract_phase_titles(description)
        assert titles[2] == "Implementation"

    def test_accepts_ascii_hyphen_separator(self) -> None:
        description = "## Phase 3 - Cleanup\n"
        titles = _extract_phase_titles(description)
        assert titles[3] == "Cleanup"

    def test_mixed_separators_in_same_document(self) -> None:
        description = (
            "## Phase 1: Colon style\n\n"
            "## Phase 2 \u2014 Em-dash style\n\n"
            "## Phase 3 - Hyphen style\n"
        )
        titles = _extract_phase_titles(description)
        assert titles == {
            1: "Colon style",
            2: "Em-dash style",
            3: "Hyphen style",
        }

    def test_preserves_hyphens_inside_title(self) -> None:
        description = "## Phase 4: TDD - red/green/refactor\n"
        titles = _extract_phase_titles(description)
        assert titles[4] == "TDD - red/green/refactor"

    def test_ignores_heading_without_separator(self) -> None:
        description = "## Phase 5 NoSeparatorHere\n"
        assert _extract_phase_titles(description) == {}
