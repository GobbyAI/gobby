"""Durable recorders for proof-backed review lessons."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.review_learning.round_diff import (
    PlanReviewLessonCandidate,
    classify_plan_review_rounds,
    select_plan_review_candidates,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._read import get_task

logger = logging.getLogger(__name__)


class ReviewLearningRecorder(Protocol):
    async def record(
        self,
        *,
        source_kind: str,
        source: str,
        source_review: str,
        decision: str,
        finding: dict[str, Any],
        evidence: dict[str, Any],
        session_id: str | None = None,
        repo: str | None = None,
        language: str | None = None,
        risk: str = "medium",
    ) -> dict[str, Any]: ...


async def mint_plan_review_lessons(
    task_id: str,
    stage: str,
    *,
    db: HubDatabase,
    review_learning_service: ReviewLearningRecorder,
    session_id: str | None = None,
) -> dict[str, object]:
    """Mint finalized plan-review lessons and checkpoint the approval row."""
    store = PlanReviewEvidenceStore(db)
    rows = store.list_for_task_stage(task_id=task_id, stage=stage)
    approval = _approval_checkpoint(rows)
    if approval is None:
        raise ReviewEvidenceError(
            "approval_checkpoint_missing",
            f"task {task_id} stage {stage} has no durable approval checkpoint",
        )
    if approval.lesson_mint_status in {"minted", "none"}:
        return plan_review_mint_result(approval)

    evidence_service = PlanReviewEvidenceService(db)
    task = get_task(db, task_id)
    if task.is_escalated:
        return _checkpoint(
            evidence_service,
            approval,
            status="none",
            lesson_ids=[],
            detail="task or stage was escalated or abandoned",
        )

    candidates = select_plan_review_candidates(
        classify_plan_review_rounds(rows, task_id=task_id, stage=stage),
        limit=5,
    )
    if not candidates:
        return _checkpoint(
            evidence_service,
            approval,
            status="none",
            lesson_ids=[],
            detail="no proof-backed blocking findings",
        )

    lesson_ids: list[str] = []
    try:
        for candidate in candidates:
            result = await review_learning_service.record(
                source_kind="plan_review",
                source="plan-review",
                source_review=_source_review(task_id, stage, candidate),
                decision="confirmed",
                finding=_lesson_finding(candidate),
                evidence=_lesson_evidence(task_id, stage, candidate),
                session_id=session_id,
                risk="high",
            )
            lesson_id = result.get("lesson_id")
            if isinstance(lesson_id, str) and lesson_id not in lesson_ids:
                lesson_ids.append(lesson_id)
    except Exception as error:
        logger.warning(
            "plan_review_lesson_mint_failed",
            extra={
                "task_id": task_id,
                "stage": stage,
                "evidence_id": approval.evidence_id,
            },
            exc_info=True,
        )
        return _checkpoint(
            evidence_service,
            approval,
            status="failed",
            lesson_ids=lesson_ids,
            detail=f"{type(error).__name__}: {error}",
        )
    return _checkpoint(
        evidence_service,
        approval,
        status="minted",
        lesson_ids=lesson_ids,
        detail=None,
    )


def _approval_checkpoint(
    rows: list[PlanReviewEvidence],
) -> PlanReviewEvidence | None:
    approvals = [
        row
        for row in rows
        if row.approval_result is not None
        and row.approved_at is not None
        and row.lesson_mint_status is not None
    ]
    if not approvals:
        return None
    return max(
        approvals,
        key=lambda row: (row.approved_at, row.created_at, row.evidence_id),
    )


def plan_review_mint_result(evidence: PlanReviewEvidence) -> dict[str, object]:
    """Return the stable wire result stored on an approval checkpoint."""
    detail = evidence.lesson_mint_detail or {}
    lesson_ids = detail.get("minted_lesson_ids", [])
    if not isinstance(lesson_ids, list):
        lesson_ids = []
    return {
        "lesson_mint_status": evidence.lesson_mint_status,
        "minted_lesson_ids": lesson_ids,
        "detail": detail.get("detail"),
        "evidence_id": evidence.evidence_id,
    }


def _checkpoint(
    service: PlanReviewEvidenceService,
    approval: PlanReviewEvidence,
    *,
    status: str,
    lesson_ids: list[str],
    detail: str | None,
) -> dict[str, object]:
    checkpoint_detail: dict[str, object] = {
        "minted_lesson_ids": lesson_ids,
        "detail": detail,
    }
    updated = service.checkpoint_plan_review_lesson_mint(
        approval.evidence_id,
        status=status,
        detail=checkpoint_detail,
    )
    return plan_review_mint_result(updated)


def _source_review(
    task_id: str,
    stage: str,
    candidate: PlanReviewLessonCandidate,
) -> str:
    finding_id = str(candidate.finding["finding_id"])
    return (
        f"plan-review:{task_id}:{stage}:round:{candidate.round_number}:"
        f"evidence:{candidate.evidence_id}:finding:{finding_id}"
    )


def _lesson_finding(candidate: PlanReviewLessonCandidate) -> dict[str, Any]:
    finding = dict(candidate.finding)
    finding_id = str(finding["finding_id"])
    category = str(finding["category"])
    check_key = str(finding["check_key"])
    finding.update(
        {
            "title": str(finding["description"]),
            "message": str(finding["description"]),
            "lesson_type": candidate.lesson_type,
            "pattern_id": (f"plan-review:{candidate.lesson_type}:{category}:{check_key}"),
            "finding_fingerprint": (
                f"plan-review:{candidate.lesson_type}:{candidate.evidence_id}:{finding_id}"
            ),
            "rule_id": f"plan-review:{category}",
            "guardrail_target": "checklist",
        }
    )
    return finding


def _lesson_evidence(
    task_id: str,
    stage: str,
    candidate: PlanReviewLessonCandidate,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "stage": stage,
        "evidence_id": candidate.evidence_id,
        "round_number": candidate.round_number,
        "classification": candidate.lesson_type,
        "proof": candidate.proof,
    }
