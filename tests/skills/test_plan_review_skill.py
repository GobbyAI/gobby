"""Content-level tests for the bundled plan-review methodology skill.

plan-review is the single source of truth for how to adversarially review a
gobby plan document. It is consumed from two places:
  - the interactive /gobby plan skill's adversarial loop (Step 7),
  - the autonomous plan-adversary.yaml agent (load_skill step before reviewing).

These tests guard content drift: the review-rejection contract matches
taskless plan-adversary expectations, the round-scoped heading matches
what the interactive planner records in the plan changelog, and the
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
            "explicit test tasks",
            "file path",  # concrete target for code/config
            "refactor",  # canonical category (added in #12038)
            "P<N>: Name",  # canonical phase heading
            "implementation_domain",  # code routing contract
        ):
            assert check in body, f"Missing gobby-specific check: {check}"

    def test_plan_identity_precondition_blocks_unknown_covers(self, body: str) -> None:
        normalized = " ".join(body.split())
        assert "Plan Identity Precondition" in body
        assert "**Plan ID:** <id>" in body
        assert "outside fenced code" in body
        assert "literal `unknown`" in normalized
        assert "covers:unknown:" in body

    # --- escalation ---------------------------------------------------------

    def test_blocking_findings_use_review_rejection(self, body: str) -> None:
        """Routine taskless revision rounds should return needs_review."""
        assert "verdict: needs_review" in body
        assert "round_number" in body
        assert "## V1 Plan Changelog" in body

    def test_halt_condition_uses_needs_requirements_prefix(self, body: str) -> None:
        """Insufficient-context halt uses the same prefix the autonomous
        planner uses (matching contract)."""
        assert "needs_requirements:" in body

    def test_autonomous_exit_uses_end_agent_run_without_session_id(self, body: str) -> None:
        assert "end_agent_run" in body
        assert "session_id" not in body

    def test_autonomous_exit_returns_structured_review_result(self, body: str) -> None:
        assert "verdict: approved" in body
        assert "verdict: needs_review" in body

    def test_nits_do_not_block_approval(self, body: str) -> None:
        """Severity distinction: blocking vs nit. Nits alone approve."""
        lowered = body.lower()
        assert "blocking" in lowered
        assert "nit" in lowered

    def test_severity_matrix_and_ledger_delivery_surfaces(self, body: str) -> None:
        contract = Path("docs/contracts/plan-coverage.md").read_text(encoding="utf-8")
        taskless_agent = Path(
            "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml"
        ).read_text(encoding="utf-8")
        plan_skill = Path("src/gobby/install/shared/skills/plan/SKILL.md").read_text(
            encoding="utf-8"
        )

        for surface in (body, contract):
            normalized = " ".join(surface.lower().split())
            for severity in ("blocking", "major", "minor", "nit"):
                assert f"| {severity} |" in normalized
            assert "demonstrated violation of a required obligation" in normalized
            assert "material non-gating quality or operability risk" in normalized
            assert "localized hardening with bounded effect" in normalized
            assert "cosmetic" in normalized
            assert "boundary example" in normalized
            assert "quality ledger" in normalized

        assert "failure_trace" in taskless_agent
        assert "minimal_repair" in taskless_agent
        assert "plan-review skill" in taskless_agent.lower()
        assert "approval_result.quality_ledger" in plan_skill
        assert "routing_decisions" in plan_skill
        assert "manifest_entries" in plan_skill

    # --- output format ------------------------------------------------------

    def test_round_scoped_findings_header(self, body: str) -> None:
        """The interactive planner records the current display round."""
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

    def test_minimal_repair_scope_contract(self, body: str) -> None:
        assert "**minimal_repair**" in body
        assert "**repair_scope**" in body
        assert "`existing_sections`" in body
        assert "`new_deliverable`" in body
        assert "**new_deliverable_justification**" in body

    def test_three_lane_coverage_and_complexity_thresholds(self, body: str) -> None:
        normalized_body = " ".join(body.lower().split())
        for lane in (
            "requirements_traceability",
            "repository_blast_radius",
            "runtime_invariants",
        ):
            assert lane in body
        for threshold in ("8 deliverables", "24 acceptance", "12 distinct target", "4 sections"):
            assert threshold in body
        assert (
            "parallel fanout is limited to one read-only provider-native internal "
            "subagent per lane" in normalized_body
        )
        assert "run all three concurrently." in normalized_body
        assert "plan-review-researcher-taskless" not in body
        assert "`gobby-agents:spawn_agent` for lane research" in body
        assert "15 minutes" in body

    def test_snapshot_transport_pages_to_verified_exhaustion(self, body: str) -> None:
        normalized = " ".join(body.split())

        assert "start with `offset: 0`" in normalized
        assert "follow `next_offset` to exhaustion" in normalized
        assert "concatenate every `content` page" in normalized
        assert "verify the reconstructed bytes against `snapshot_hash`" in normalized
        assert "parse all records before lane review begins" in normalized
        assert "Pass only `routing_decisions` to `validate_plan_review_coverage`" in normalized

    def test_parent_dispositions_and_adjacent_variant_closure(self, body: str) -> None:
        for field in (
            "cross_lane_interactions",
            "adjacent_variant_sweeps",
            "causal_repair_sweeps",
            "candidate_dispositions",
            "record_bundle",
        ):
            assert field in body
        assert "emitted_finding" in body
        assert "dismissed" in body
        assert "cross-lane interaction" in body.lower()
        assert "adjacent-variant" in body.lower()
        assert "prior finding" in body.lower()

    def test_shadow_manifest_and_source_drift_contract(self, body: str) -> None:
        assert "derive_plan_review_manifest" in body
        assert "validate_plan_review_coverage" in body
        assert "coverage_attestation" in body
        assert '"verdict":"inconclusive"' in body
        assert '"reason_code":"source_drift"' in body
        assert "needs_human:unstable_review_source:<paths>" in body
