"""Content-level tests for the bundled plan-review methodology skill.

plan-review is the single source of truth for how to adversarially review a
gobby plan document. It is consumed from two places:
  - the interactive /gobby plan skill's adversarial loop (Step 7),
  - the autonomous plan-adversary.yaml agent (load_skill step before reviewing).

These tests guard content drift: the review-rejection contract matches
plan-adversary.yaml/stage-native planning expectations, the round-scoped heading matches
what the interactive planner extracts from the task description, and the
attitude-vs-quota guidance is deliberately the opposite of BMAD's "at least
10 findings" instruction so adversary approval is a valid outcome on clean
plans.
"""

from pathlib import Path

import pytest

from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/plan-review/SKILL.md")


class TestPlanReviewFrontmatter:
    def test_skill_parses(self) -> None:
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.name == "plan-review"
        assert parsed.description
        # Discoverable by review-related terms
        lowered = parsed.description.lower()
        assert "review" in lowered or "critique" in lowered

    def test_audience_is_all(self) -> None:
        """Used by both interactive session and spawned plan-adversary agent."""
        parsed = parse_skill_file(SKILL_PATH)
        assert parsed.audience_config is not None
        assert parsed.audience_config.audience == "all"


class TestPlanReviewContent:
    @pytest.fixture(scope="class")
    def body(self) -> str:
        return SKILL_PATH.read_text(encoding="utf-8")

    # --- attitude -----------------------------------------------------------

    def test_no_finding_quota(self, body: str) -> None:
        """Explicitly rejects BMAD's 'at least N findings' guidance — a clean
        plan is a valid outcome. Adversary should not manufacture findings."""
        lowered = body.lower()
        assert "quota" in lowered
        # Must contain language that rejects manufactured findings.
        assert "manufactur" in lowered or "do not" in lowered

    def test_re_check_before_approval(self, body: str) -> None:
        """If the first pass finds nothing, the skill must instruct a second
        pass before approving — and only then approve cleanly."""
        # Methodical re-check language
        assert "second pass" in body.lower() or "re-check" in body.lower()
        assert "approve" in body.lower()

    # --- method -------------------------------------------------------------

    def test_walks_branches_and_boundaries(self, body: str) -> None:
        """Mechanical-walk language — derived from bmad-review-edge-case-hunter."""
        lowered = body.lower()
        assert "branch" in lowered
        assert "boundary" in lowered or "boundaries" in lowered

    def test_enumerates_failure_modes(self, body: str) -> None:
        """Method section must prompt for failure-mode enumeration so the
        drafter gets concrete holes back."""
        lowered = body.lower()
        assert "failure" in lowered
        # Common edge classes we want surfaced
        assert any(w in lowered for w in ("timeout", "race", "null", "empty"))

    # --- traceability -------------------------------------------------------

    def test_traceability_against_parent_task_not_literal_requirements_header(
        self, body: str
    ) -> None:
        """Per plan design: the canonical source is the parent task description
        plus docs it references — NOT a literal ## Requirements heading. The
        skill must make this explicit so the adversary doesn't halt on plans
        without that exact heading."""
        lowered = body.lower()
        assert "parent task" in lowered
        # The explicit non-requirement of a literal header
        assert "literal" in lowered or "do not require" in lowered

    # --- gobby-specific checks ---------------------------------------------

    def test_gobby_format_checks_listed(self, body: str) -> None:
        """These are contract-level and must all be flagged as blocking.
        Silent drift here breaks the expand-task pipeline on accepted plans."""
        for check in (
            "explicit test tasks",  # TDD sandwich model
            "file path",  # concrete target for code/config
            "refactor",  # canonical category (added in #12038)
            "Phase N: Name",  # canonical phase heading
        ):
            assert check in body, f"Missing gobby-specific check: {check}"

    # --- escalation ---------------------------------------------------------

    def test_blocking_findings_use_review_rejection(self, body: str) -> None:
        """Routine revision rounds should use reject_review and
        return the planning task to open."""
        assert "reject_review" in body
        assert "round_number" in body
        assert "returns the anchor to `open`" in body

    def test_halt_condition_uses_needs_requirements_prefix(self, body: str) -> None:
        """Insufficient-context halt uses the same prefix the autonomous
        planner uses (matching contract)."""
        assert "needs_requirements:" in body

    def test_autonomous_exit_uses_end_agent_run_without_session_id(self, body: str) -> None:
        assert "end_agent_run" in body
        assert "session_id" not in body

    def test_autonomous_exit_allows_review_rejection_before_end_agent_run(self, body: str) -> None:
        assert "reject_review" in body

    def test_nits_do_not_block_approval(self, body: str) -> None:
        """Severity distinction: blocking vs nit. Nits alone approve."""
        lowered = body.lower()
        assert "blocking" in lowered
        assert "nit" in lowered

    # --- output format ------------------------------------------------------

    def test_round_scoped_findings_header(self, body: str) -> None:
        """The interactive planner extracts the section matching the CURRENT
        display round. The heading must be `## Adversary Findings — Round N`
        (with em-dash)."""
        assert "## Adversary Findings" in body
        assert "Round N" in body
        assert "em-dash" in body.lower() or "—" in body

    def test_preserves_prior_rounds(self, body: str) -> None:
        """Must not overwrite prior rounds' findings — audit trail."""
        lowered = body.lower()
        assert "overwrite" in lowered or "prior" in lowered or "append" in lowered

    def test_severity_and_category_fields(self, body: str) -> None:
        """The finding schema the interactive planner parses."""
        for field in ("severity", "category", "location", "description", "suggested fix"):
            assert field in body.lower(), f"Finding schema missing: {field}"
