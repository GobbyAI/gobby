"""Replay-safe effects that follow a committed staged plan-review verdict."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from gobby.build.coordinator import summary_allows_cross_project_coordinator
from gobby.build.dispatch_tick import schedule_dispatcher_tick_for_project
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import PlanReviewEvidence
from gobby.plans.review_telemetry import deterministic_review_message_id
from gobby.review_learning.recorders import (
    ReviewLearningRecorder,
    mint_plan_review_lessons,
)
from gobby.storage.agents import AgentRun
from gobby.storage.build_history import BuildHistoryStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.commits import auto_link_commits
from gobby.utils.datetime import datetime_to_required_iso
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import remove_claimed_task

logger = logging.getLogger(__name__)


def apply_staged_verdict_effects(
    db: HubDatabase,
    *,
    evidence: PlanReviewEvidence,
    run: AgentRun,
    result: dict[str, object],
) -> None:
    """Apply verdict-dependent effects after stage and evidence commit."""
    task_id = evidence.task_id
    if task_id is None:
        return
    task_manager = LocalTaskManager(db)
    task = task_manager.get_task(task_id)
    verdict = _verdict_message(task.seq_num, task_id, evidence.round_number, result)

    _auto_link_session_commits(
        db,
        task_manager=task_manager,
        task_id=task_id,
        project_id=task.project_id,
        session_id=run.child_session_id,
    )
    _clear_claim_and_record_verdict(
        db,
        task_id=task_id,
        session_id=run.child_session_id,
        verdict=verdict,
    )
    _link_session_task(
        db,
        task_id=task_id,
        session_id=run.child_session_id,
        result=result,
    )
    _relay_signoff(
        db,
        evidence=evidence,
        run=run,
        project_id=task.project_id,
        task_seq_num=task.seq_num,
        verdict=verdict,
        result=result,
    )
    _mint_approval_lessons(
        db,
        evidence=evidence,
        session_id=run.child_session_id,
    )
    schedule_dispatcher_tick_for_project(
        db,
        project_id=task.project_id,
        reason=_effect_action(result),
    )


def _auto_link_session_commits(
    db: HubDatabase,
    *,
    task_manager: LocalTaskManager,
    task_id: str,
    project_id: str,
    session_id: str | None,
) -> None:
    if session_id is None:
        return
    try:
        session = SessionManager(db).get(session_id)
        project = LocalProjectManager(db).get(project_id)
        if session is None or project is None or not project.repo_path:
            return
        auto_link_commits(
            task_manager=task_manager,
            task_id=task_id,
            since=datetime_to_required_iso(session.created_at),
            cwd=project.repo_path,
            project_id=project_id,
        )
    except (OSError, RuntimeError, ValueError):
        logger.debug("Plan-review commit auto-link failed", exc_info=True)


def _clear_claim_and_record_verdict(
    db: HubDatabase,
    *,
    task_id: str,
    session_id: str | None,
    verdict: str,
) -> None:
    if session_id is None:
        return
    variables = SessionVariableManager(db)
    try:
        current = variables.get_variables(session_id)
        variables.merge_variables(session_id, remove_claimed_task(current, task_id))
        variables.set_variable(session_id, "adversary_verdict", verdict)
    except (KeyError, RuntimeError, ValueError):
        logger.debug("Plan-review session variable update failed", exc_info=True)


def _link_session_task(
    db: HubDatabase,
    *,
    task_id: str,
    session_id: str | None,
    result: dict[str, object],
) -> None:
    if session_id is None:
        return
    relation = "review_approved" if result.get("verdict") == "approved" else "review_rejected"
    try:
        SessionTaskManager(db).link_task(session_id, task_id, relation)
    except (RuntimeError, ValueError):
        logger.debug("Plan-review session-task link failed", exc_info=True)


def _relay_signoff(
    db: HubDatabase,
    *,
    evidence: PlanReviewEvidence,
    run: AgentRun,
    project_id: str,
    task_seq_num: int | None,
    verdict: str,
    result: dict[str, object],
) -> None:
    task_id = evidence.task_id
    if task_id is None or run.child_session_id is None:
        return
    build_run = BuildHistoryStorage(db).latest_coordinated_run_for_task(project_id, task_id)
    if build_run is None or not build_run.summary:
        return
    target = build_run.summary.get("coordinator_session_id")
    if not isinstance(target, str) or not target:
        return
    coordinator = SessionManager(db).get(target)
    if coordinator is None:
        return
    coordinator_project_id = str(coordinator.project_id) if coordinator.project_id else None
    if coordinator_project_id != project_id and not summary_allows_cross_project_coordinator(
        build_run.summary,
        coordinator_project_id=coordinator_project_id,
        build_project_id=project_id,
    ):
        return
    action = _effect_action(result)
    metadata = {
        "task_id": task_id,
        "task_ref": f"#{task_seq_num}" if task_seq_num else None,
        "stage_name": evidence.stage,
        "action": action,
        "signoff_message": verdict,
        "build_run_id": build_run.id,
        "root_task_id": build_run.root_task_id,
        "from_session_id": run.child_session_id,
    }
    message_id = deterministic_review_message_id(
        evidence_id=evidence.evidence_id,
        run_id=run.id,
        effect_kind="signoff_relay",
        target_session_id=target,
    )
    InterSessionMessageManager(db).create_message(
        from_session=run.child_session_id,
        to_session=target,
        content=verdict,
        priority="high",
        message_type="message",
        metadata_json=json.dumps(metadata, default=str, sort_keys=True),
        message_id=message_id,
    )


def _mint_approval_lessons(
    db: HubDatabase,
    *,
    evidence: PlanReviewEvidence,
    session_id: str | None,
) -> None:
    if evidence.approval_result is None:
        return
    current = PlanReviewEvidenceService(db).get_evidence(evidence.evidence_id)
    if current.lesson_mint_status in {"minted", "none"}:
        return
    recorder = _review_learning_recorder(db) or _UnavailableReviewLearningRecorder()
    _run_coroutine_blocking(
        mint_plan_review_lessons(
            evidence.task_id or "",
            evidence.stage or "planning",
            db=db,
            review_learning_service=recorder,
            session_id=session_id,
        )
    )


def _review_learning_recorder(db: HubDatabase) -> ReviewLearningRecorder | None:
    from gobby.app_context import get_app_context

    app = get_app_context()
    if app is None or app.database is not db or app.memory_manager is None:
        return None
    from gobby.review_learning.service import ReviewLearningService

    return ReviewLearningService(
        memory_manager=app.memory_manager,
        task_manager=app.task_manager,
    )


class _UnavailableReviewLearningRecorder:
    async def record(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Review-learning service is unavailable")


def _run_coroutine_blocking[T](coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-mint") as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _effect_action(result: dict[str, object]) -> str:
    return "approve_review" if result.get("verdict") == "approved" else "reject_review"


def _verdict_message(
    task_seq_num: int | None,
    task_id: str,
    round_number: int,
    result: dict[str, object],
) -> str:
    task_ref = f"#{task_seq_num}" if task_seq_num else task_id
    if result.get("verdict") == "approved":
        return f"Approved {task_ref}"
    return f"Rejected {task_ref} round {round_number}"
