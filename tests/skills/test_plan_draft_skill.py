"""Content-level tests for the bundled plan-draft methodology skill.

plan-draft is the single source of truth for how to structure a gobby plan
document. It is consumed from two places:
  - the interactive /gobby plan skill (Step 3 loads it via get_skill),
  - the autonomous planner.yaml agent (load_skill step before drafting).

These tests guard content drift: the canonical category list, the TDD-forbidden
patterns, and the verification checklist all have downstream consumers (the
expand-task pipeline and the plan-review skill's heuristics) that rely on what
this file says. Drift here silently breaks those consumers.
"""

from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/plan-draft/SKILL.md")


class TestPlanDraftFrontmatter:
    def test_skill_parses(self) -> None:
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.name == "plan-draft"
        assert parsed.description
        # The description must call out that this is drafting methodology so
        # semantic search can surface it when drafting.
        assert "plan" in parsed.description.lower()

    def test_audience_is_all(self) -> None:
        """plan-draft is consumed by both interactive and autonomous sessions."""
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.audience_config is not None
        assert parsed.audience_config.audience == "all"


class TestPlanDraftContent:
    """Assertions against the Markdown body.

    These are not style checks — each assertion corresponds to a contract with
    another part of the system. Adjust carefully.
    """

    @pytest.fixture(scope="class")
    def body(self) -> str:
        return SKILL_PATH.read_text()

    # --- canonical category list -------------------------------------------

    def test_lists_every_expansion_category(self, body: str) -> None:
        """`required` covers categories that expansion may emit as leaves."""
        required = {"code", "config", "docs", "refactor", "test"}
        for cat in required:
            assert f"`{cat}`" in body, f"Missing canonical category: {cat}"
        assert "discovery and" in body
        assert "design must already be resolved" in body
        assert "`manual`" in body
        assert "outside expansion manifests" in body

    def test_refactor_category_documented(self, body: str) -> None:
        """Refactor was added as a canonical category in #12038 and the skill
        must reflect that."""
        assert "`refactor`" in body

    def test_code_and_config_marked_tdd_eligible(self, body: str) -> None:
        """Only code and config are TDD-eligible expansion categories."""
        assert "`code` | yes" in body
        assert "`config` | conditional" in body
        assert "use `tdd: true` only for executable behavior" in body

    # --- TDD anti-patterns --------------------------------------------------

    def test_forbids_explicit_test_tasks(self, body: str) -> None:
        """Wrapper prefixes must be flagged as forbidden in drafts.

        Expansion now emits one leaf per manifest entry and skill-backed TDD
        metadata on required leaves; pre-inserted wrappers still break that.
        """
        for pattern in ("[TDD]", "[IMPL]", "[REF]"):
            assert pattern in body, f"TDD-forbidden pattern not called out: {pattern}"
        assert "Write tests for" in body
        assert "duplicate TDD-wrapper" in body
        assert 'additional_skills: ["test-driven-development"]' in body
        assert "label `tdd:required`" in body
        assert "standalone `category: test`" in body
        assert "parity regression suite" in body

    # --- phase heading syntax ----------------------------------------------

    def test_canonical_phase_syntax_documented(self, body: str) -> None:
        """The canonical form is `## P<N>: Name`; old Phase headings are skipped."""
        assert "## P<N>: Name" in body
        assert "## Phase 1: Setup" in body
        assert "silently fail" in body

    # --- self-contained-sections rule --------------------------------------

    def test_sections_must_be_self_contained(self, body: str) -> None:
        """Implementing agents see ONLY their own ### N.N section during
        expansion — the skill must tell the drafter this explicitly."""
        lowered = body.lower()
        assert "self-contained" in lowered
        # The implementing-agent-only-sees-this-section guarantee
        assert "only" in lowered

    # --- verification checklist --------------------------------------------

    def test_verification_checklist_covers_five_items(self, body: str) -> None:
        """The plan skill's Step 5 asks the drafter to run this checklist;
        every item corresponds to something downstream cares about."""
        assert "Verification" in body
        # Five numbered checks per the plan design
        # Don't pin exact numbering — just ensure each item is present.
        required_checks = (
            "explicit test tasks",  # 1
            "Dependency Tree",  # 2
            "Categor",  # 3
            "Phase Heading",  # 4
            "Self-Contained",  # 5
        )
        for check in required_checks:
            assert check in body, f"Verification checklist missing: {check}"
