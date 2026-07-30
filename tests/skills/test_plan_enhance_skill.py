"""Content-level tests for the bundled plan-enhance methodology skill.

plan-enhance is the single source of truth for how to constructively enhance a
gobby plan document — the Better/Bigger counterweight to plan-review. It is
consumed from two places:
  - the interactive /gobby plan skill's enhancement phase (Step 4.5), via the
    spawned plan-enhancer-taskless agent,
  - the autonomous plan-enhancer.yaml lifecycle agent (load_skill step before
    suggesting), which records suggestions via record_plan_enhancement.

These tests guard content drift: the enhancer is advisory-only (never edits the
plan, never gates), it loads the shared proportionality test so it never
suggests a Rube Goldberg, it honestly reports `converged` rather than inventing
work, and its output schema carries the exact ranked fields the planner and
coordinator parse, with severity pinned to `opportunity` (never blocking).
"""

from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/plan-enhance/SKILL.md")


class TestPlanEnhanceFrontmatter:
    def test_skill_parses(self) -> None:
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.name == "plan-enhance"
        assert parsed.description
        # Discoverable by enhancement-related terms
        lowered = parsed.description.lower()
        assert "enhance" in lowered or "improvement" in lowered

    def test_audience_is_all(self) -> None:
        """Used by both interactive coordinator and spawned plan-enhancer agent."""
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.audience_config is not None
        assert parsed.audience_config.audience == "all"


class TestPlanEnhanceContent:
    @pytest.fixture
    def body(self) -> str:
        return SKILL_PATH.read_text(encoding="utf-8")

    def test_two_lenses_better_and_bigger(self, body: str) -> None:
        """The constructive pass walks Better (in-scope polish) and Bigger (justified net-new)."""
        assert "Better" in body
        assert "Bigger" in body
        assert "better | bigger" in body or "better|bigger" in body

    def test_constructive_attitude_not_a_critic(self, body: str) -> None:
        lowered = body.lower()
        assert "constructive" in lowered
        assert "opportunity-seeking" in lowered

    def test_references_proportionality_justification_test(self, body: str) -> None:
        """No gold-plating: every suggestion passes the shared proportionality test first."""
        assert "proportionality" in body
        assert "justification test" in body.lower()
        assert "gold-plating" in body.lower()

    def test_never_edits_plan_or_gates(self, body: str) -> None:
        lowered = body.lower()
        assert "never edit the plan file" in lowered
        # No approve/reject/manifest authority — that belongs to the adversary.
        assert "never approve" in lowered
        assert "manifest" in lowered

    def test_no_quota_reports_honestly(self, body: str) -> None:
        """A tight plan converges with zero suggestions instead of inventing work."""
        lowered = body.lower()
        assert "do not manufacture suggestions" in lowered
        assert "converged: true" in lowered
        assert "zero" in lowered

    def test_ranked_by_impact_vs_effort(self, body: str) -> None:
        lowered = body.lower()
        assert "impact" in lowered and "effort" in lowered

    def test_output_schema_fields(self, body: str) -> None:
        """The advisory suggestion schema the planner/coordinator parses."""
        for field in (
            "converged",
            "lens",
            "category",
            "location",
            "description",
            "suggested_enhancement",
            "impact",
            "effort",
            "risk",
            "severity",
        ):
            assert field in body, f"Output schema missing field: {field}"

    def test_category_enum(self, body: str) -> None:
        for category in ("scope", "testability", "reuse", "sequencing", "clarity"):
            assert category in body, f"Category enum missing: {category}"

    def test_skill_distinguishes_correctness_defects_from_conformance_gaps(self, body: str) -> None:
        normalized = " ".join(body.lower().split())
        assert "a settled contract is existing scope" in normalized
        assert "a **correctness defect** is a wrong claim inside the plan" in normalized
        assert "but do not propose the fix" in normalized
        assert (
            "a **conformance gap** is plan silence about a mechanism a cited contract" in normalized
        )
        assert "propose the fix as" in normalized
        assert "`lens: better`, `category: clarity`" in normalized
        assert "never classify that gap as" in normalized
        assert "`lens: bigger` or `category: scope`" in normalized

    def test_skill_includes_worked_e4_conformance_example(self, body: str) -> None:
        normalized = " ".join(body.lower().split())
        assert "**worked e4 example:**" in normalized
        assert "one token consumed by both css media queries and `useismobile`" in normalized
        assert "separate hard-coded thresholds satisfy the cited contract" in normalized

    def test_skill_instructs_suggestions_to_preserve_mandated_mechanisms(self, body: str) -> None:
        lowered = body.lower()
        normalized = " ".join(lowered.split())
        assert "preserve mandated mechanisms" in lowered
        assert "name that mechanism in `suggested_enhancement`" in lowered
        assert "carry its exact implementation constraint" in lowered
        assert "including any exact mechanism mandated" in normalized

    def test_severity_is_always_opportunity_never_blocking(self, body: str) -> None:
        """The enhancer has no blocking severity — blocking belongs to the adversary."""
        assert "opportunity" in body
        lowered = body.lower()
        assert "never" in lowered and "blocking" in lowered

    def test_autonomous_exit_records_enhancement_then_ends_run(self, body: str) -> None:
        assert "record_plan_enhancement" in body
        assert "end_agent_run" in body

    def test_interactive_exit_sends_message(self, body: str) -> None:
        assert "send_message" in body
