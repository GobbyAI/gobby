"""Post-commit plan-review mint tail for the stage approval tool."""

from __future__ import annotations

import asyncio
import logging

import psycopg

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.review_learning.recorders import (
    mint_plan_review_lessons,
    plan_review_mint_result,
)

logger = logging.getLogger(__name__)


def complete_plan_review_mint(
    ctx: RegistryContext,
    *,
    task_id: str,
    stage: str,
    evidence_id: str,
    session_id: str,
    replay: bool,
) -> dict[str, object]:
    """Await the fail-open mint tail or return a replayed durable result."""
    if replay:
        evidence_service = PlanReviewEvidenceService(ctx.task_manager.db)
        return plan_review_mint_result(evidence_service.get_evidence(evidence_id))
    recorder = ctx.review_learning_service
    if recorder is None:
        return _checkpoint_failure(
            PlanReviewEvidenceService(ctx.task_manager.db),
            evidence_id,
            "Review-learning service is unavailable",
        )
    try:
        return asyncio.run(
            mint_plan_review_lessons(
                task_id,
                stage,
                db=ctx.task_manager.db,
                review_learning_service=recorder,
                session_id=session_id,
            )
        )
    except Exception as error:
        logger.warning(
            "plan_review_approval_mint_tail_failed",
            extra={"task_id": task_id, "stage": stage, "evidence_id": evidence_id},
            exc_info=True,
        )
        return _checkpoint_failure(
            PlanReviewEvidenceService(ctx.task_manager.db),
            evidence_id,
            f"{type(error).__name__}: {error}",
        )


def _checkpoint_failure(
    service: PlanReviewEvidenceService,
    evidence_id: str,
    detail: str,
) -> dict[str, object]:
    try:
        evidence = service.checkpoint_plan_review_lesson_mint(
            evidence_id,
            status="failed",
            detail={"minted_lesson_ids": [], "detail": detail},
        )
    except (ReviewEvidenceError, psycopg.Error) as error:
        logger.warning(
            "plan_review_lesson_mint_checkpoint_failed",
            extra={"evidence_id": evidence_id},
            exc_info=True,
        )
        return {
            "lesson_mint_status": "failed",
            "minted_lesson_ids": [],
            "detail": f"{detail}; checkpoint failed: {type(error).__name__}: {error}",
            "evidence_id": evidence_id,
        }
    return plan_review_mint_result(evidence)
