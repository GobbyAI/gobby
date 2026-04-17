"""Tests for phase subepic support in task expansion."""

import pytest

from gobby.mcp_proxy.tools.tasks._expansion import (
    _extract_phase_from_title,
    _extract_phase_titles,
    _get_subtask_phase,
)
from gobby.tasks.expansion_service import (
    _extract_phase_sections,
    _prefix_spec_ids,
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


class TestExtractPhaseSections:
    def test_splits_plan_into_ordered_sections(self) -> None:
        content = (
            "# Epic\n\n"
            "Intro paragraph.\n\n"
            "## Phase 0: Prep\n\n"
            "Prep body line 1.\n\n"
            "Prep body line 2.\n\n"
            "## Phase 1: Build\n\n"
            "Build body.\n"
        )
        sections = _extract_phase_sections(content)
        assert [s["number"] for s in sections] == [0, 1]
        assert sections[0]["title"] == "Prep"
        assert sections[1]["title"] == "Build"
        assert "Prep body line 1." in sections[0]["body"]
        assert "Prep body line 2." in sections[0]["body"]
        assert "Build" not in sections[0]["body"]
        assert "Build body." in sections[1]["body"]

    def test_last_section_runs_to_end_of_file(self) -> None:
        content = (
            "## Phase 1: First\n\n"
            "First body.\n\n"
            "## Phase 2: Second\n\n"
            "Second body with trailing content.\n"
            "More lines.\n"
        )
        sections = _extract_phase_sections(content)
        assert len(sections) == 2
        assert sections[-1]["body"].endswith("More lines.")

    def test_empty_content_yields_no_sections(self) -> None:
        assert _extract_phase_sections("") == []

    def test_content_with_no_phase_headings(self) -> None:
        content = "# Just a heading\n\nSome paragraph.\n"
        assert _extract_phase_sections(content) == []

    def test_accepts_em_dash_separator(self) -> None:
        content = "## Phase 0 \u2014 Prereqs\n\nBody.\n## Phase 1 \u2014 Build\n\nMore.\n"
        sections = _extract_phase_sections(content)
        assert [s["number"] for s in sections] == [0, 1]
        assert sections[0]["title"] == "Prereqs"
        assert sections[1]["title"] == "Build"


class TestPrefixSpecIds:
    @staticmethod
    def _sample_spec() -> dict[str, object]:
        return {
            "phases": [
                {"id": "phase-1", "title": "P", "task_ids": ["t-1", "t-2"]},
            ],
            "tasks": [
                {"id": "t-1", "phase_id": "phase-1", "title": "First"},
                {"id": "t-2", "phase_id": "phase-1", "title": "Second"},
            ],
            "dependencies": [{"task_id": "t-2", "depends_on": "t-1"}],
            "execution_groups": [
                {"id": "group-1", "mode": "parallel", "task_ids": ["t-1", "t-2"]},
            ],
        }

    def test_prefixes_all_id_fields(self) -> None:
        spec = self._sample_spec()
        result = _prefix_spec_ids(spec, prefix="phase-0-")
        assert result["phases"][0]["id"] == "phase-0-phase-1"
        assert result["phases"][0]["task_ids"] == ["phase-0-t-1", "phase-0-t-2"]
        assert [t["id"] for t in result["tasks"]] == ["phase-0-t-1", "phase-0-t-2"]
        assert {t["phase_id"] for t in result["tasks"]} == {"phase-0-phase-1"}
        dep = result["dependencies"][0]
        assert dep == {"task_id": "phase-0-t-2", "depends_on": "phase-0-t-1"}
        assert result["execution_groups"][0]["id"] == "phase-0-group-1"
        assert result["execution_groups"][0]["task_ids"] == ["phase-0-t-1", "phase-0-t-2"]

    def test_idempotent_on_already_prefixed_ids(self) -> None:
        spec = _prefix_spec_ids(self._sample_spec(), prefix="phase-0-")
        once_more = _prefix_spec_ids(spec, prefix="phase-0-")
        assert once_more == spec

    def test_distinct_prefixes_produce_disjoint_ids(self) -> None:
        a = _prefix_spec_ids(self._sample_spec(), prefix="phase-0-")
        b = _prefix_spec_ids(self._sample_spec(), prefix="phase-1-")
        a_ids = {t["id"] for t in a["tasks"]}
        b_ids = {t["id"] for t in b["tasks"]}
        assert a_ids.isdisjoint(b_ids)

    def test_drops_dependencies_missing_endpoints(self) -> None:
        spec = self._sample_spec()
        spec["dependencies"].append({"task_id": "", "depends_on": "t-1"})
        spec["dependencies"].append({"task_id": "t-2", "depends_on": ""})
        result = _prefix_spec_ids(spec, prefix="phase-0-")
        assert len(result["dependencies"]) == 1
        assert result["dependencies"][0]["task_id"] == "phase-0-t-2"
