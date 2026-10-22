"""Shared helpers for interactive plan-review evidence tests."""

from __future__ import annotations

import uuid
from pathlib import Path

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.storage.sessions import SessionManager
from tests.review_coverage_helpers import coverage_attestation

ROUND_PROSE = "**Round 2** `kind: verification`\n\n- verdict: needs_review"


def bind_review_seats(
    service: PlanReviewEvidenceService,
    evidence_id: str,
    session_id: str,
) -> tuple[str, str]:
    sessions = SessionManager(service.db)
    owner = sessions.get(session_id)
    assert owner is not None
    evidence = service.get_evidence(evidence_id)
    writer = sessions.register(
        external_id=f"review-writer-{uuid.uuid4()}",
        machine_id=owner.machine_id,
        source="codex",
        project_id=evidence.project_id,
    )
    coordinator = sessions.register(
        external_id=f"review-coordinator-{uuid.uuid4()}",
        machine_id=owner.machine_id,
        source="codex",
        project_id=evidence.project_id,
    )
    service.bind_static_review_seats(
        evidence_id,
        writer_session_id=writer.id,
        coordinator_session_id=coordinator.id,
        caller_session_id=session_id,
    )
    return writer.id, coordinator.id


def bind_interactive_review(
    service: PlanReviewEvidenceService,
    project_id: str,
    session_id: str,
    plan_path: Path,
) -> tuple[str, str, str]:
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=2,
        session_id=session_id,
    )
    writer_id, coordinator_id = bind_review_seats(service, prepared.evidence_id, session_id)
    return prepared.evidence_id, writer_id, coordinator_id


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
