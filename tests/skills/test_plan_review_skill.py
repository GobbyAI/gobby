"""Adversarial reference obligations and stage/taskless consumer contracts."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog

pytestmark = pytest.mark.unit
REFERENCES = Path("src/gobby/install/shared/skills/gobby/references/plan")
AGENTS = Path("src/gobby/install/shared/workflows/agents")


def _body(topic: str) -> str:
    return " ".join((REFERENCES / f"{topic}.md").read_text().split())


def test_plan_review_catalog_identity() -> None:
    assert load_capability_catalog().folded_skills["plan-review"] == (
        "gobby:references/plan/review.md"
    )


def test_plan_review_uses_complete_snapshot_and_three_lanes() -> None:
    body = _body("review")
    for term in (
        "get_plan_review_snapshot for immutable bytes",
        "does not reread the live artifact",
        "Complete any oversized-result retrieval",
        "requirements_traceability",
        "runtime_invariants",
        "repository_blast_radius delegated-verified",
        "provider-native internal subagents",
        "only that lane to sequential parent work",
        "never findings/verdicts/evidence writes",
        "verifies/deduplicates every candidate",
        "emitted_finding or dismissed disposition with reason",
        "cross-lane and adjacent-variant",
        "validate_plan_review_coverage",
        "Preserve its returned attestation verbatim",
    ):
        assert term in body


def test_plan_review_protocol_failure_omits_verdict() -> None:
    body = _body("review")
    assert "protocol_failure with exact tool error and draft findings, no verdict" in body
    assert "derive_plan_review_manifest even on rejection" in body
    assert "the unmodified derive_plan_review_manifest result, ok included" in body
    assert "that result without manifest_entries" in body


def test_plan_review_finding_and_verdict_vocabulary() -> None:
    body = _body("review")
    for term in (
        "stable finding_id/check_key",
        "evidence section_id",
        "severity blocking or nit",
        "principle or root_cause",
        "missing-requirement",
        "bad-sequencing",
        "unhandled-edge",
        "weak-testability",
        "traceability",
        "over-engineering",
        "gobby-format",
        "approved or needs_review",
        "blocking finding prevents approval",
        "real nonblank/nonunknown Plan ID outside code fences",
        "covers:unknown",
    ):
        assert term in body


def test_plan_review_resolves_symbol_targets_before_blast_radius() -> None:
    body = _body("review")
    assert "Resolve each exact file-qualified Target before usages or blast-radius" in body
    assert "successful symbol validation for indexed targets regardless of category" in body
    assert "[coverage](coverage.md)" in body


@pytest.mark.parametrize("name", ["plan-adversary", "plan-adversary-taskless"])
def test_review_prompts_have_direct_repository_and_task_access(name: str) -> None:
    body = (AGENTS / f"{name}.yaml").read_text()
    assert "gobby-tasks:get_task" in body
    assert "Read repository code and Gobby task evidence directly" in _body("review")
    assert "Never edit the plan" in _body("review")


def test_repair_class_section() -> None:
    body = _body("review")
    for term in (
        "traceability permits add_targets/add_acceptance",
        "bad-sequencing add_dependency",
        "weak-testability add_acceptance",
        "gobby-format all three",
        "Other categories remain prose",
        "Every referenced section must exist in evidence",
        "Only coordinator apply_plan_review_repairs writes accepted repairs after a finalized rejection",
        "fresh reviewer re-runs its check",
    ):
        assert term in body


def test_proportionality_requires_complete_simpler_solution() -> None:
    body = _body("review")
    assert "concrete removable mechanism" in body
    assert "complete simpler solution preserving all requirements" in body
    assert "ambition/size alone never qualifies" in body


def test_capped_review_processes_final_findings_before_human_handoff() -> None:
    body = _body("repair")
    assert "finish every vote, rejection checkpoint and accepted repair, base-validate" in body
    assert "no extra adversary round" in body
    assert "only with implementation approval" in body
