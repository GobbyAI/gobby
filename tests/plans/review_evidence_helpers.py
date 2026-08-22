"""Shared helpers for interactive plan-review evidence tests."""

from __future__ import annotations

from pathlib import Path

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.storage.agents import LocalAgentRunManager
from tests.review_coverage_helpers import coverage_attestation

ROUND_PROSE = "**Round 2** `kind: verification`\n\n- verdict: needs_review"


def bind_interactive_review(
    service: PlanReviewEvidenceService,
    project_id: str,
    session_id: str,
    plan_path: Path,
) -> str:
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    run = LocalAgentRunManager(service.db).create(
        parent_session_id=session_id,
        provider="codex",
        prompt="review",
    )
    service.bind_evidence_run(prepared.evidence_id, run.id)
    return prepared.evidence_id


def needs_review_result(evidence_id: str) -> dict[str, object]:
    return {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id,
            shadow_valid=False,
        ),
    }


def repair_reviewed_section(plan_path: Path) -> None:
    current = plan_path.read_bytes()
    repaired = current.replace(b"Behavior exists.", b"Repaired behavior exists.", 1)
    assert repaired != current
    plan_path.write_bytes(repaired)
