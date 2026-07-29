"""Task review transition helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping

from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
)
from gobby.plans.review_findings import (
    render_rejection_section,
    validate_plan_review_findings,
)
from gobby.plans.review_telemetry import persist_delivered_round_result
from gobby.storage.hub.protocol import (
    HubDatabase,
    StageReviewApprovalMutation,
    StageReviewRejectionMutation,
)
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import UNSET, MaybeUnset, Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._review_round_result import (
    build_approved_round_result,
    build_rejected_round_result,
)
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._stage_types import NoCurrentStageError
from gobby.storage.tasks._updates import update_task


def _stage_states(db: HubDatabase) -> StageStatesManager:
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def submit_for_review(
    db: HubDatabase,
    task_id: str,
    stage_name: str | None = None,
    *,
    review_notes: str | None = None,
    by_session_id: str | None = None,
    repair_submission: Mapping[str, object] | None = None,
    dispatch_run_id: str | None = None,
) -> Task:
    """Submit a stage for review and release ownership."""
    task = get_task(db, task_id)
    stages = _stage_states(db)
    if stage_name is None:
        current = stages.current_stage(task_id)
        if current is None:
            raise NoCurrentStageError(task_id)
        stage_name = current.stage_name
    stages.submit_for_review(
        task_id,
        stage_name,
        by_session_id=by_session_id,
        notes=review_notes,
        repair_submission=repair_submission,
        dispatch_run_id=dispatch_run_id,
    )
    description: MaybeUnset[str | None] = UNSET
    if review_notes:
        description = (task.description or "") + f"\n\n[Review Notes]\n{review_notes}"
    update_task(
        db,
        task_id,
        description=description,
        claimed_by_session_id=None,
        remove_labels=("planning-current-verdict:rejected",),
    )
    return get_task(db, task_id)


def approve_review(
    db: HubDatabase,
    task_id: str,
    stage_name: str | None = None,
    *,
    approval_notes: str | None = None,
    round_number: int | None = None,
    findings: list[dict[str, object]] | None = None,
    manifest_entries: list[dict[str, object]] | None = None,
    routing_decisions: dict[str, object] | None = None,
    coverage_attestation: dict[str, object] | None = None,
    convergence_telemetry: dict[str, object] | None = None,
    evidence_id: str | None = None,
    by_session_id: str | None = None,
    dispatch_run_id: str | None = None,
) -> Task:
    """Approve review on a stage and release ownership."""
    task = get_task(db, task_id)
    stages = _stage_states(db)
    if stage_name is None:
        current = stages.current_stage(task_id)
        if current is None:
            raise NoCurrentStageError(task_id)
        stage_name = current.stage_name
    if stage_name == "planning":
        return _approve_plan_review(
            db,
            task=task,
            stage_name=stage_name,
            approval_notes=approval_notes,
            round_number=round_number,
            findings=findings,
            manifest_entries=manifest_entries,
            routing_decisions=routing_decisions,
            coverage_attestation=coverage_attestation,
            convergence_telemetry=convergence_telemetry,
            evidence_id=evidence_id,
            by_session_id=by_session_id,
            dispatch_run_id=dispatch_run_id,
        )
    stages.approve_review(
        task_id,
        stage_name,
        by_session_id=by_session_id,
        notes=approval_notes,
        dispatch_run_id=dispatch_run_id,
    )
    description: MaybeUnset[str | None] = UNSET
    if approval_notes:
        description = (task.description or "") + f"\n\n[Approval Notes]\n{approval_notes}"

    update_task(
        db,
        task_id,
        description=description,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def _approve_plan_review(
    db: HubDatabase,
    *,
    task: Task,
    stage_name: str,
    approval_notes: str | None,
    round_number: int | None,
    findings: list[dict[str, object]] | None,
    manifest_entries: list[dict[str, object]] | None,
    routing_decisions: dict[str, object] | None,
    coverage_attestation: dict[str, object] | None,
    convergence_telemetry: dict[str, object] | None,
    evidence_id: str | None,
    by_session_id: str | None,
    dispatch_run_id: str | None,
) -> Task:
    if round_number is None or round_number < 1:
        raise ReviewEvidenceError(
            "missing_round_number",
            "planning-stage approval requires a positive round_number",
        )
    if not evidence_id:
        raise ReviewEvidenceError(
            "missing_evidence_id",
            "planning-stage approval requires evidence_id",
        )
    if findings is None:
        raise ReviewEvidenceError(
            "missing_findings",
            "planning-stage approval requires typed findings",
        )
    if not manifest_entries:
        raise ReviewEvidenceError(
            "missing_manifest_entries",
            "planning-stage approval requires typed manifest_entries",
        )
    if routing_decisions is None:
        raise ReviewEvidenceError(
            "missing_routing_decisions",
            "planning-stage approval requires routing_decisions",
        )
    if coverage_attestation is None:
        raise ReviewEvidenceError(
            "missing_coverage_attestation",
            "planning-stage approval requires coverage_attestation",
        )
    if convergence_telemetry is None:
        raise ReviewEvidenceError(
            "missing_convergence_telemetry",
            "planning-stage approval requires convergence_telemetry",
        )
    artifacts = TaskArtifactManager(db).get_artifacts(task.id)
    if not artifacts.plan_file_path:
        raise ReviewEvidenceError(
            "plan_path_missing",
            "planning-stage approval requires a plan artifact",
        )
    service = PlanReviewEvidenceService(db)
    evidence = service.authorize_current_attempt(
        evidence_id,
        project_id=task.project_id,
        plan_path=artifacts.plan_file_path,
        round_number=round_number,
        task_id=task.id,
        stage=stage_name,
        run_id=dispatch_run_id,
        allow_approval_replay=True,
    )
    validated_findings = validate_plan_review_findings(findings, evidence=evidence)
    round_result = build_approved_round_result(
        findings=validated_findings,
        manifest_entries=manifest_entries,
        routing_decisions=routing_decisions,
        coverage_attestation=coverage_attestation,
        convergence_telemetry=convergence_telemetry,
    )
    if convergence_telemetry.get("state") == "delivered":
        persist_delivered_round_result(
            db,
            run_id=dispatch_run_id or "",
            round_result=round_result,
        )
        return get_task(db, task.id)
    replay = _recorded_approval_replay(
        db,
        task.id,
        evidence=evidence,
        round_result=round_result,
    )
    with db.transaction_immediate(StageReviewApprovalMutation(task_id=task.id)) as transaction:
        if replay is None:
            service.apply_plan_review_manifest(
                evidence_id,
                plan_path=artifacts.plan_file_path,
                round_result=round_result,
                run_id=dispatch_run_id,
            )
            evidence = service.authorize_current_attempt(
                evidence_id,
                project_id=task.project_id,
                plan_path=artifacts.plan_file_path,
                round_number=round_number,
                task_id=task.id,
                stage=stage_name,
                run_id=dispatch_run_id,
                allow_approval_replay=True,
            )
            replay = _recorded_approval_replay(
                db,
                task.id,
                evidence=evidence,
                round_result=round_result,
            )
            if replay is None:
                if (
                    evidence.manifest_state != "applied"
                    or evidence.manifest_payload != round_result
                ):
                    raise ReviewEvidenceError(
                        "manifest_apply_incomplete",
                        "approval manifest must be durably applied before the approval commit",
                    )
                quality_ledger = service.derive_quality_ledger_for_evidence(
                    evidence_id,
                    round_result,
                    transaction=transaction,
                )
                service.finalize_plan_review_evidence(
                    evidence_id,
                    round_result,
                    _derived_quality_ledger=quality_ledger,
                )

        stages = _stage_states(db)
        current_stage = stages.get(task.id, stage_name)
        if current_stage is None or current_stage.state != "review_approved":
            stages.approve_review(
                task.id,
                stage_name,
                by_session_id=by_session_id,
                notes=approval_notes,
                dispatch_run_id=dispatch_run_id,
                preheld_mutex_run_id=dispatch_run_id,
            )
        description: MaybeUnset[str | None] = UNSET
        if approval_notes:
            description = (task.description or "") + f"\n\n[Approval Notes]\n{approval_notes}"
        update_task(
            db,
            task.id,
            description=description,
            claimed_by_session_id=None,
        )
    return get_task(db, task.id)


def _recorded_approval_replay(
    db: HubDatabase,
    task_id: str,
    *,
    evidence: PlanReviewEvidence,
    round_result: dict[str, object],
) -> Task | None:
    if evidence.finalized_at is None:
        return None
    approval_result = dict(evidence.approval_result or {})
    delivered_ledger = approval_result.pop("quality_ledger", None) or []
    if approval_result != round_result or delivered_ledger != (evidence.quality_ledger or []):
        raise ReviewEvidenceError(
            "approval_result_conflict",
            "approval retry conflicts with the durable approval result",
        )
    return get_task(db, task_id)


def reject_review(
    db: HubDatabase,
    task_id: str,
    stage_name: str | None = None,
    *,
    rejection_notes: str | None = None,
    round_number: int | None = None,
    findings: list[dict[str, object]] | None = None,
    coverage_attestation: dict[str, object] | None = None,
    convergence_telemetry: dict[str, object] | None = None,
    evidence_id: str | None = None,
    plan_hash: str | None = None,
    cited_subtasks: list[str] | None = None,
    by_session_id: str | None = None,
    dispatch_run_id: str | None = None,
) -> Task:
    """Reject review on a stage and release ownership."""
    task = get_task(db, task_id)
    normalized_round = None
    if round_number is not None:
        # Tools/routes may pass an int-like value; normalize once before validation.
        normalized_round = int(round_number)
        if normalized_round < 1:
            raise ValueError("round must be >= 1 when provided")

    stages = _stage_states(db)
    if stage_name is None:
        current = stages.current_stage(task_id)
        if current is None:
            raise NoCurrentStageError(task_id)
        stage_name = current.stage_name
    if findings is not None:
        return _reject_review_with_findings(
            db,
            task=task,
            stage_name=stage_name,
            round_number=normalized_round,
            findings=findings,
            coverage_attestation=coverage_attestation,
            convergence_telemetry=convergence_telemetry,
            evidence_id=evidence_id,
            by_session_id=by_session_id,
            dispatch_run_id=dispatch_run_id,
        )
    notes = rejection_notes
    if plan_hash:
        notes = f"{notes or ''}\n\nplan_hash: {plan_hash}".strip()
    if cited_subtasks:
        notes = f"{notes or ''}\n\ncited_subtasks: {', '.join(cited_subtasks)}".strip()
    stages.reject_review(
        task_id,
        stage_name,
        reason=rejection_notes or "review_rejected",
        by_session_id=by_session_id,
        notes=notes,
        dispatch_run_id=dispatch_run_id,
    )

    description: MaybeUnset[str | None] = UNSET
    if rejection_notes:
        heading = (
            f"## Adversary Findings — Round {normalized_round}"
            if normalized_round is not None
            else "## Review Rejection"
        )
        section = f"{heading}\n\n{rejection_notes}"
        existing = task.description or ""
        # Re-running the same round must replace the prior section, not stack.
        # Only attempt the in-place replacement for round-scoped headings; the
        # generic "## Review Rejection" heading is used for one-off rejections
        # without a round number and is allowed to stack.
        if normalized_round is not None:
            description = _replace_round_section(
                existing,
                round_number=normalized_round,
                section=section,
            )
        else:
            description = f"{existing}\n\n{section}" if existing else section

    update_task(
        db,
        task_id,
        description=description,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def _reject_review_with_findings(
    db: HubDatabase,
    *,
    task: Task,
    stage_name: str,
    round_number: int | None,
    findings: list[dict[str, object]],
    coverage_attestation: dict[str, object] | None,
    convergence_telemetry: dict[str, object] | None,
    evidence_id: str | None,
    by_session_id: str | None,
    dispatch_run_id: str | None,
) -> Task:
    if stage_name != "planning":
        raise ReviewEvidenceError(
            "unsupported_review_stage",
            "structured findings are supported only for planning-stage review",
        )
    if round_number is None:
        raise ReviewEvidenceError(
            "missing_round_number",
            "structured findings require round_number",
        )
    if not evidence_id:
        raise ReviewEvidenceError(
            "missing_evidence_id",
            "structured findings require evidence_id",
        )
    if coverage_attestation is None:
        raise ReviewEvidenceError(
            "missing_coverage_attestation",
            "structured findings require coverage_attestation",
        )
    if convergence_telemetry is None:
        raise ReviewEvidenceError(
            "missing_convergence_telemetry",
            "structured findings require convergence_telemetry",
        )
    artifacts = TaskArtifactManager(db).get_artifacts(task.id)
    if not artifacts.plan_file_path:
        raise ReviewEvidenceError(
            "plan_path_missing",
            "planning-stage structured findings require a plan artifact",
        )

    service = PlanReviewEvidenceService(db)
    evidence = service.authorize_current_attempt(
        evidence_id,
        project_id=task.project_id,
        plan_path=artifacts.plan_file_path,
        round_number=round_number,
        task_id=task.id,
        stage=stage_name,
        run_id=dispatch_run_id,
        allow_rejection_replay=True,
    )
    validated_findings = validate_plan_review_findings(
        findings,
        evidence=evidence,
    )
    round_result = build_rejected_round_result(
        findings=validated_findings,
        coverage_attestation=coverage_attestation,
        convergence_telemetry=convergence_telemetry,
    )
    if convergence_telemetry.get("state") == "delivered":
        persist_delivered_round_result(
            db,
            run_id=dispatch_run_id or "",
            round_result=round_result,
        )
        return get_task(db, task.id)
    replay = _recorded_rejection_replay(
        db,
        task.id,
        evidence=evidence,
        round_result=round_result,
    )
    if replay is None:
        with db.transaction_immediate(StageReviewRejectionMutation(task_id=task.id)):
            evidence = service.authorize_current_attempt(
                evidence_id,
                project_id=task.project_id,
                plan_path=artifacts.plan_file_path,
                round_number=round_number,
                task_id=task.id,
                stage=stage_name,
                run_id=dispatch_run_id,
                allow_rejection_replay=True,
            )
            replay = _recorded_rejection_replay(
                db,
                task.id,
                evidence=evidence,
                round_result=round_result,
            )
            if replay is None:
                service.finalize_plan_review_evidence(evidence_id, round_result)

    current_task = get_task(db, task.id)
    section = render_rejection_section(
        round_number=round_number,
        findings=validated_findings,
        evidence=evidence,
    )
    description = _replace_round_section(
        current_task.description or "",
        round_number=round_number,
        section=section,
    )
    stages = _stage_states(db)
    current_stage = stages.get(task.id, stage_name)
    if current_stage is None or current_stage.state != "ready":
        stages.reject_review(
            task.id,
            stage_name,
            reason="review_rejected",
            by_session_id=by_session_id,
            notes=section,
            dispatch_run_id=dispatch_run_id,
        )
    update_task(
        db,
        task.id,
        description=description,
        claimed_by_session_id=None,
    )
    return get_task(db, task.id)


def _recorded_rejection_replay(
    db: HubDatabase,
    task_id: str,
    *,
    evidence: PlanReviewEvidence,
    round_result: dict[str, object],
) -> Task | None:
    if evidence.finalized_at is None:
        return None
    if evidence.round_result != round_result:
        raise ReviewEvidenceError(
            "evidence_replay",
            "finalized rejection evidence cannot be reused with changed findings",
        )
    return get_task(db, task_id)


def _replace_round_section(
    description: str,
    *,
    round_number: int,
    section: str,
) -> str:
    heading = f"## Adversary Findings — Round {round_number}"
    pattern = re.compile(
        rf"^{re.escape(heading)}$.*?(?=^## |\Z)",
        re.DOTALL | re.MULTILINE,
    )
    if pattern.search(description):
        return pattern.sub(section.rstrip() + "\n\n", description).rstrip() or section
    return f"{description}\n\n{section}" if description else section
