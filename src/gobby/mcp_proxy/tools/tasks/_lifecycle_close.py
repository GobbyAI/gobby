"""Checklist-based task closure with one evaluation and one commit phase."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    resolve_task_repo_path,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import CloseEvaluationFingerprint
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence as _derive_close_transcript_evidence,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    has_committable_edits as _has_committable_edits,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import (
    capture_attribution as _capture_attribution,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import (
    children_state as _children_state,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import commit_close as _commit_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    active_review_response,
    launch_close_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    submit_close_review as finalize_close_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import (
    CloseEvaluation,
    resolve_close_commit_shas,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import (
    SubmittedCloseReview,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import (
    evaluate_close_criteria as evaluate_criteria_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    active_validation_backoff,
    record_validation_infrastructure_failure,
    validate_commit_requirements,
    validate_parent_task,
)
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.mcp_proxy.tools.tasks._task_scope import evaluate_task_scope
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import Task, TaskNotFoundError
from gobby.tasks.acceptance_artifacts import (
    evaluate_acceptance_artifacts,
    render_acceptance_test_bodies,
)
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.close_verdict_memo import TaskCloseVerdictMemo
from gobby.tasks.commits import collect_commit_diff_text
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.epic_guards import evaluate_epic_guards
from gobby.tasks.generation_schemas import TASK_CLOSE_VALIDATION_SCHEMA
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence
from gobby.tasks.transcript_evidence import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
)
from gobby.tasks.validation import NO_WORK_CLOSE_REASONS

_DELIBERATE_CLOSE_SKIP = "Skipped for a justified deliberate close of an escalated task."


def _close_verdict_memo(
    ctx: RegistryContext,
    *,
    task: Task,
    caller_session_id: str | None,
    close_arguments: Mapping[str, Any],
) -> TaskCloseVerdictMemo | None:
    """Bind this task's verdict memo to the attempt's own identity.

    Returns ``None`` when the attempt has no resolvable session or no criteria
    to review against — both cases the review gate handles on its own, and
    neither is worth a memo row.
    """
    criteria = split_validation_criteria(task.validation_criteria or "")
    if caller_session_id is None or not criteria:
        return None
    return TaskCloseVerdictMemo(
        TaskCloseReviewStore(ctx.task_manager.db),
        task_id=task.id,
        task_ref=f"#{task.seq_num}" if task.seq_num else task.id,
        caller_session_id=caller_session_id,
        close_arguments=close_arguments,
        criteria=criteria,
    )


def _is_deliberate_close(task: Task, override_justification: str | None) -> bool:
    """Whether a human has explicitly decided this escalated task closes."""
    return task.is_escalated and bool((override_justification or "").strip())


def _apply_escalated_close_gate(
    evaluation: CloseEvaluation,
    override_justification: str | None,
) -> None:
    """Gate 14 for an escalated task: require justification, then skip review."""
    if not (override_justification or "").strip():
        evaluation.fail(
            14,
            "criteria_review",
            "task_escalated",
            "Escalated tasks require override_justification for deliberate closure.",
            action=(
                "Provide override_justification to close deliberately, "
                "or use de_escalate_task/reopen_task."
            ),
            extra={"escalated": True},
        )
        return
    evaluation.validation_reset_reason = "escalated_deliberate_close"
    evaluation.pass_gate(14, "criteria_review", _DELIBERATE_CLOSE_SKIP, skipped=True)


async def _evaluate_close(
    ctx: RegistryContext,
    *,
    task_id: str,
    reason: str,
    changes_summary: str | None,
    commit_sha: str | None,
    project_path: str | None,
    response_detail: Literal["concise", "diagnostic"],
    submitted_review: SubmittedCloseReview | None = None,
    closing_session_id: str | None = None,
    override_justification: str | None = None,
    scope_justification: str | None = None,
) -> CloseEvaluation:
    """Evaluate the checklist once without close or commit-link mutation."""
    evaluation = CloseEvaluation(task_id, response_detail=response_detail)
    try:
        resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
    except (TaskNotFoundError, ValueError) as exc:
        return evaluation.fail(1, "task_exists", "task_not_found", str(exc))
    task = ctx.task_manager.get_task(resolved_id)
    if task is None:
        return evaluation.fail(1, "task_exists", "task_not_found", f"Task {task_id} not found.")
    evaluation.task = task
    evaluation.task_id = resolved_id
    evaluation.pass_gate(1, "task_exists", "Task exists.")

    from gobby.utils.session_context import get_current_session_id

    session_ref = closing_session_id or get_current_session_id() or get_claimed_session_id(task)
    if not session_ref:
        return evaluation.fail(
            2,
            "session_context",
            "no_session_context",
            "close_task requires an active session or a task claimed by a registered session.",
            action="Claim the task from an active session, then retry close_task.",
        )
    try:
        resolved_session_id = ctx.resolve_session_id(session_ref)
    except ValueError as exc:
        return evaluation.fail(
            2,
            "session_context",
            "session_resolution_failed",
            f"Cannot resolve close session {session_ref!r}: {exc}",
        )
    evaluation.resolved_session_id = resolved_session_id
    evaluation.pass_gate(2, "session_context", "Close session resolved.")

    try:
        repo_path = resolve_task_repo_path(
            task_manager=ctx.task_manager,
            project_manager=ctx.project_manager,
            task=task,
            project_path=project_path,
        )
    except RepoPathValidationError as exc:
        return evaluation.fail(3, "repository_path", "invalid_project_path", str(exc))
    if repo_path is None:
        return evaluation.fail(
            3,
            "repository_path",
            "task_repo_path_unavailable",
            "close_task requires a registered repository path.",
        )
    evaluation.repo_path = repo_path
    evaluation.pass_gate(3, "repository_path", "Task repository resolved.")
    evaluation.edit_session_id = get_claimed_session_id(task) or resolved_session_id

    children, children_state = _children_state(ctx, resolved_id)
    has_children = bool(children)
    if has_children:
        parent_result = validate_parent_task(ctx, resolved_id, children=children)
        if not parent_result.can_close:
            return evaluation.fail(
                4,
                "children_closed",
                parent_result.error_type or "children_open",
                parent_result.message or "Close every child task first.",
                extra=parent_result.extra,
            )
    evaluation.is_epic = task.task_type == "epic"
    evaluation.skip_leaf_checks = has_children or evaluation.is_epic
    evaluation.pass_gate(4, "children_closed", "Every child task is closed.")
    if evaluation.skip_leaf_checks:
        evaluation.fingerprint = CloseEvaluationFingerprint.capture(
            task,
            children_state=children_state,
            attribution=None,
        )
        # Off the loop: this reaches normalize_commit_sha -> run_git_command ->
        # subprocess.run, which forks git and then blocks waiting for it (#20861).
        evaluation.commit_shas, _commit_error = await asyncio.to_thread(
            resolve_close_commit_shas,
            ctx.task_manager,
            task=task,
            task_id=resolved_id,
            claim_started_at=None,
            commit_sha=commit_sha,
            cwd=repo_path,
            project_name=ctx.get_current_project_name(),
        )
        for item, name in (
            (5, "criteria_present"),
            (6, "changes_summary_present"),
            (7, "linked_commits"),
            (8, "task_scope"),
            (9, "uncommitted_task_edits"),
            (10, "validation_commands"),
            (11, "acceptance_artifacts"),
            (12, "tdd_evidence"),
            (13, "epic_guards"),
        ):
            evaluation.pass_gate(
                item,
                name,
                "Skipped for an epic or structural parent.",
                skipped=True,
            )
        if task.is_escalated:
            _apply_escalated_close_gate(evaluation, override_justification)
            return evaluation
        evaluation.pass_gate(
            14,
            "criteria_review",
            "Skipped for an epic or structural parent.",
            skipped=True,
        )
        return evaluation

    if not (task.validation_criteria or "").strip():
        return evaluation.fail(
            5,
            "criteria_present",
            "missing_validation_criteria",
            "Leaf tasks require explicit validation criteria before closing.",
        )
    evaluation.pass_gate(5, "criteria_present", "Validation criteria are present.")
    if not (changes_summary or "").strip():
        return evaluation.fail(
            6,
            "changes_summary_present",
            "missing_changes_summary",
            "Leaf tasks require changes_summary describing what changed and why.",
        )
    evaluation.pass_gate(6, "changes_summary_present", "Changes summary is present.")

    try:
        attribution = await _capture_attribution(
            ctx,
            task=task,
            task_id=resolved_id,
            resolved_session_id=resolved_session_id,
            repo_path=repo_path,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return evaluation.fail(
            2,
            "session_context",
            "session_variable_lookup_failed",
            f"Cannot read task edit attribution from the owning session: {exc}",
        )
    evaluation.edit_session_id = attribution.owner_session_id
    if attribution.attributed and not attribution.raw_paths:
        return evaluation.fail(
            9,
            "uncommitted_task_edits",
            "task_edit_paths_unavailable",
            "The task records edits but no attributed file paths. Restore task edit state and retry.",
        )
    evaluation.edited_paths = set(attribution.edited_paths)
    evaluation.had_attributed_edits = attribution.had_attributed_edits
    evaluation.claim_started_at = attribution.claim_started_at
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task,
        children_state=children_state,
        attribution=attribution,
    )
    # Off the loop, for the same reason as the skip_leaf_checks branch above: a
    # close resolves several shas and each one forks git and waits for it (#20861).
    commit_shas, commit_error = await asyncio.to_thread(
        resolve_close_commit_shas,
        ctx.task_manager,
        task=task,
        task_id=resolved_id,
        claim_started_at=evaluation.claim_started_at,
        commit_sha=commit_sha,
        cwd=repo_path,
        project_name=ctx.get_current_project_name(),
    )
    evaluation.commit_shas = commit_shas
    if commit_error:
        return evaluation.fail(
            7,
            "linked_commits",
            str(commit_error["error"]),
            str(commit_error["message"]),
        )
    evaluation_task = replace(task, commits=commit_shas or None)
    if evaluation.had_attributed_edits:
        # Off the loop: normalize_commit_sha again, once per already-linked commit.
        commit_result = await asyncio.to_thread(
            validate_commit_requirements, evaluation_task, reason, repo_path
        )
        if not commit_result.can_close:
            commit_extra = dict(commit_result.extra)
            if evaluation.response_detail == "diagnostic":
                commit_extra["attributed_paths"] = sorted(evaluation.edited_paths)
            return evaluation.fail(
                7,
                "linked_commits",
                commit_result.error_type or "commit_validation_failed",
                commit_result.message or "Link a commit for the attributed task edits.",
                extra=commit_extra,
            )
    evaluation.pass_gate(
        7,
        "linked_commits",
        "Attributed edits have a linked commit."
        if evaluation.had_attributed_edits
        else "No attributed committable edits require a commit.",
        skipped=not evaluation.had_attributed_edits,
    )

    try:
        scope = await asyncio.to_thread(
            evaluate_task_scope,
            db=ctx.task_manager.db,
            task=task,
            commit_shas=commit_shas,
            attributed_paths=evaluation.edited_paths,
            repo_path=repo_path,
            scope_justification=scope_justification,
        )
    except RuntimeError as exc:
        return evaluation.fail(
            8,
            "task_scope",
            "task_scope_unavailable",
            f"Task scope cannot be evaluated: {exc}",
        )
    evaluation.scope_snapshot = scope.snapshot()
    evaluation.scope_justification = scope.scope_justification
    if not scope.accepted:
        return evaluation.fail(
            8,
            "task_scope",
            "task_scope_mismatch",
            scope.justification_error or "Task changes exceed the declared scope.",
            action=(
                "Pass a specific scope_justification between 20 and 1000 characters "
                "that explains why the listed paths belong in this task."
            ),
            details=scope.details(),
            extra=scope.details(),
        )
    evaluation.pass_gate(
        8,
        "task_scope",
        "Out-of-scope paths have a recorded justification."
        if scope.has_mismatch
        else "Delivered paths stay within the declared task scope.",
        details=scope.details(),
        skipped=not scope.declared_paths,
    )

    has_dirty_edits = bool(evaluation.edited_paths) and await asyncio.to_thread(
        _has_committable_edits, evaluation.edited_paths, repo_path
    )
    if has_dirty_edits:
        return evaluation.fail(
            9,
            "uncommitted_task_edits",
            "uncommitted_task_edits",
            "Task-attributed files still have uncommitted changes. Commit them and retry.",
        )
    evaluation.pass_gate(9, "uncommitted_task_edits", "No task-attributed files are dirty.")

    transcript = TranscriptEvidence()
    command_gate = replace(
        evaluate_validation_commands(
            task_category=task.category,
            evidence=TranscriptEvidence(),
            has_attributed_edits=evaluation.had_attributed_edits,
        ),
        item=10,
    )
    if command_gate.status != "skipped":
        backoff = active_validation_backoff(task, ctx)
        if backoff is not None:
            return evaluation.fail(
                10,
                "validation_commands",
                backoff.error_type or "validation_infrastructure_unavailable",
                backoff.message or "Validation infrastructure is unavailable.",
                extra=backoff.extra,
            )
        try:
            transcript = await _derive_close_transcript_evidence(
                ctx,
                task_id=resolved_id,
                owner_session_id=attribution.owner_session_id,
                closing_session_id=resolved_session_id,
                owner_window_start=evaluation.claim_started_at,
                task_edited_files=evaluation.edited_paths,
                repo_path=repo_path,
            )
        except TranscriptEvidenceUnavailable as exc:
            infra = record_validation_infrastructure_failure(
                task,
                ctx,
                resolved_id=resolved_id,
                message=(
                    f"Task-close transcript evidence is unavailable: {exc}. "
                    f"Attempted paths: {', '.join(exc.attempted_paths) or 'none'}."
                ),
                error_type="validation_evidence_unavailable",
            )
            return evaluation.fail(
                10,
                "validation_commands",
                infra.error_type or "validation_evidence_unavailable",
                infra.message or str(exc),
                extra=infra.extra,
            )
        evaluation.transcript_evidence = transcript.summary()
        command_gate = replace(
            evaluate_validation_commands(
                task_category=task.category,
                evidence=transcript,
                has_attributed_edits=evaluation.had_attributed_edits,
            ),
            item=10,
        )
    evaluation.gates.append(command_gate)
    if not command_gate.passed:
        evaluation.error = "validation_command_required"
        evaluation.message = command_gate.message
        evaluation.action = command_gate.message
        return evaluation

    try:
        diff_text = await asyncio.to_thread(
            collect_commit_diff_text,
            commit_shas,
            cwd=repo_path,
        )
    except RuntimeError as exc:
        infra = record_validation_infrastructure_failure(
            task,
            ctx,
            resolved_id=resolved_id,
            message=f"Validation diff is unavailable: {exc}",
            error_type="validation_diff_unavailable",
        )
        return evaluation.fail(
            14,
            "criteria_review",
            infra.error_type or "validation_diff_unavailable",
            infra.message or str(exc),
            extra=infra.extra,
        )

    acceptance_details: dict[str, object]
    tdd_details: dict[str, object]
    guard_details: dict[str, object]
    # Gate 13's own details keep the guard runner's stdout for diagnostics; the
    # facts handed to the criteria review must not, because they fingerprint
    # the review and a fresh pytest duration per attempt makes the memoized
    # verdict unreachable (#20866).
    guard_review_facts: dict[str, object]
    test_bodies = "Named acceptance tests: none."
    if reason in NO_WORK_CLOSE_REASONS:
        acceptance_details = {"findings": [], "test_references": [], "evidence_files": []}
        tdd_details = {"findings": [], "red_runs": [], "green_runs": []}
        guard_details = {"paths": [], "source_task_ids": []}
        guard_review_facts = dict(guard_details)
        for item, name in (
            (11, "acceptance_artifacts"),
            (12, "tdd_evidence"),
            (13, "epic_guards"),
        ):
            evaluation.pass_gate(
                item,
                name,
                "Skipped for a canonical no-work disposition.",
                skipped=True,
            )
    else:
        artifacts = await asyncio.to_thread(
            evaluate_acceptance_artifacts,
            criteria=task.validation_criteria or "",
            repo_path=repo_path,
            commit_shas=commit_shas,
        )
        acceptance_details = artifacts.details()
        if not artifacts.passed:
            return evaluation.fail(
                11,
                "acceptance_artifacts",
                "acceptance_artifacts_invalid",
                artifacts.findings[0],
                details=acceptance_details,
                extra={"acceptance_artifacts": acceptance_details},
            )
        evaluation.pass_gate(
            11,
            "acceptance_artifacts",
            "Named acceptance artifacts passed deterministic checks.",
            details=acceptance_details,
            skipped=not artifacts.tests and not artifacts.evidence_files,
        )
        test_bodies = render_acceptance_test_bodies(artifacts.tests)

        tdd = evaluate_tdd_evidence(artifacts.tests, transcript)
        tdd_details = tdd.details()
        # Gate 12 and gate 14 both ask whether the loop was followed rather than
        # whether the deliverable is sound, so a justified deliberate close waives
        # them together. The delivery gates above stay hard: a waived close still
        # proves the work is committed, in scope, clean, and validated.
        waive_tdd = not tdd.passed and _is_deliberate_close(task, override_justification)
        if not tdd.passed and not waive_tdd:
            return evaluation.fail(
                12,
                "tdd_evidence",
                "tdd_evidence_missing",
                tdd.findings[0],
                details=tdd_details,
                extra={"tdd_evidence": tdd_details},
            )
        evaluation.pass_gate(
            12,
            "tdd_evidence",
            _DELIBERATE_CLOSE_SKIP
            if waive_tdd
            else "Every named acceptance test has assertion-backed red and later green evidence.",
            details=tdd_details,
            skipped=tdd.skipped or waive_tdd,
        )

        guards = await evaluate_epic_guards(
            task_manager=ctx.task_manager,
            task=task,
            repo_path=repo_path,
        )
        guard_details = guards.details()
        guard_review_facts = guards.review_facts()
        if not guards.passed:
            return evaluation.fail(
                13,
                "epic_guards",
                guards.error_type or "epic_guard_failed",
                guards.message,
                details=guard_details,
                extra={"epic_guards": guard_details},
            )
        evaluation.pass_gate(
            13,
            "epic_guards",
            guards.message,
            details=guard_details,
            skipped=guards.skipped,
        )

    if task.is_escalated:
        _apply_escalated_close_gate(evaluation, override_justification)
        return evaluation

    task_validator = ctx.task_validator
    if task_validator is None:
        infra = record_validation_infrastructure_failure(
            task,
            ctx,
            resolved_id=resolved_id,
            message="The task-close criteria reviewer is not configured.",
        )
        return evaluation.fail(
            14,
            "criteria_review",
            "validation_provider_unavailable",
            infra.message or "The task-close criteria reviewer is not configured.",
            extra=infra.extra,
        )
    llm_result = await evaluate_criteria_review(
        task=evaluation_task,
        task_validator=task_validator,
        ctx=ctx,
        resolved_id=resolved_id,
        verdict_memo=_close_verdict_memo(
            ctx,
            task=evaluation_task,
            caller_session_id=evaluation.resolved_session_id,
            close_arguments={
                "reason": reason,
                "changes_summary": changes_summary,
                "commit_sha": commit_sha,
                "project_path": project_path,
                "override_justification": override_justification,
                "scope_justification": scope_justification,
            },
        ),
        changes_summary=changes_summary or "",
        diff_text=diff_text,
        checklist_facts={
            "commit_count": len(commit_shas),
            "commit_shas": commit_shas,
            "had_attributed_edits": evaluation.had_attributed_edits,
            "attributed_paths": sorted(evaluation.edited_paths),
            "claim_started_at": evaluation.claim_started_at,
            "validation_commands": command_gate.details,
            "acceptance_artifacts": acceptance_details,
            "tdd_evidence": tdd_details,
            "epic_guards": guard_review_facts,
        },
        validation_config=ctx.validation_config,
        reason=reason,
        description=task.description or "",
        test_bodies=test_bodies,
        submitted_review=submitted_review,
    )
    evaluation.validation_status = llm_result.validation_status
    evaluation.validation_feedback = llm_result.validation_feedback
    evaluation.validation_reset_reason = llm_result.reset_reason
    evaluation.verdict = llm_result.extra.get("verdict")
    if not llm_result.can_close:
        reasons = llm_result.extra.get("blocking_reasons")
        message = (
            str(reasons[0])
            if isinstance(reasons, list) and reasons
            else llm_result.message or "Criteria review did not pass."
        )
        return evaluation.fail(
            14,
            "criteria_review",
            llm_result.error_type or "validation_failed",
            message,
            extra=llm_result.extra,
        )
    evaluation.pass_gate(14, "criteria_review", "Task-close criteria review passed.")
    evaluation.extra.update(llm_result.extra)
    return evaluation


def register_close_task(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    """Register the checklist-based close_task tool."""

    async def close_task(
        task_id: str,
        reason: str = "completed",
        changes_summary: str | None = None,
        skip_validation: bool = False,
        override_justification: str | None = None,
        scope_justification: str | None = None,
        commit_sha: str | None = None,
        project_path: str | None = None,
        preview: bool = False,
        response_detail: Literal["concise", "diagnostic"] = "concise",
    ) -> dict[str, Any]:
        active = active_review_response(ctx, task_id)
        if active is not None:
            return active
        close_arguments = {
            "task_id": task_id,
            "reason": reason,
            "changes_summary": changes_summary,
            "skip_validation": skip_validation,
            "override_justification": override_justification,
            "scope_justification": scope_justification,
            "commit_sha": commit_sha,
            "project_path": project_path,
            "preview": preview,
            "response_detail": response_detail,
        }
        evaluation = await _evaluate_close(
            ctx,
            task_id=task_id,
            reason=reason,
            changes_summary=changes_summary,
            commit_sha=commit_sha,
            project_path=project_path,
            response_detail=response_detail,
            override_justification=override_justification,
            scope_justification=scope_justification,
        )
        if evaluation.error == "agentic_review_required":
            return await launch_close_review(
                ctx,
                evaluation=evaluation,
                close_arguments=close_arguments,
            )
        if not evaluation.ready:
            return evaluation.response(preview=preview)
        result = await _commit_close(
            ctx,
            evaluation,
            reason=reason,
            skip_validation=skip_validation,
            override_justification=override_justification,
            commit_sha=commit_sha,
        )
        result.update({"preview": preview, "can_close": result.get("closed") is True})
        return result

    async def submit_close_review(review_id: str, verdict: dict[str, object]) -> dict[str, Any]:
        return await finalize_close_review(
            ctx,
            review_id=review_id,
            verdict=verdict,
            evaluate_close=_evaluate_close,
            commit_close=_commit_close,
        )

    registry.register(
        name="close_task",
        description=(
            "Evaluate the ordered close checklist and close ready tasks in the same call. "
            "Leaf tasks require criteria, a changes summary, commits for attributed edits, "
            "a clean transcript-derived validation run, and one bounded criteria review "
            "unless a justified deliberate close exits escalation. "
            "Epics and other parents close when they have no open children; closing the "
            "last child auto-closes eligible ancestors. "
            "preview=true returns diagnostics when blocked and still closes when ready."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task reference (#N, path, or UUID)."},
                "reason": {"type": "string", "default": "completed"},
                "changes_summary": {"type": "string"},
                "skip_validation": {
                    "type": "boolean",
                    "default": False,
                    "description": "Organizational close audit flag; ignored for leaves.",
                },
                "override_justification": {
                    "type": "string",
                    "description": (
                        "Required to deliberately close an escalated task; persisted as "
                        "validation_override_reason and ignored for ordinary leaf closure."
                    ),
                },
                "scope_justification": {
                    "type": "string",
                    "minLength": 20,
                    "maxLength": 1000,
                    "description": (
                        "Required when linked or attributed paths exceed declared Targets or "
                        "manual/expansion affected-file annotations; persisted with close audit."
                    ),
                },
                "commit_sha": {"type": "string"},
                "project_path": {"type": "string"},
                "preview": {
                    "type": "boolean",
                    "default": False,
                    "description": "Close when ready; otherwise return first-failure diagnostics.",
                },
                "response_detail": {
                    "type": "string",
                    "enum": ["concise", "diagnostic"],
                    "default": "concise",
                },
            },
            "required": ["task_id"],
        },
        func=close_task,
    )
    registry.register(
        name="submit_close_review",
        description=(
            "Validator-only submission for a persisted oversized task-close review. "
            "The authenticated task-close-validator run reruns deterministic gates and "
            "atomically applies the current verdict."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "review_id": {"type": "string"},
                "verdict": TASK_CLOSE_VALIDATION_SCHEMA,
            },
            "required": ["review_id", "verdict"],
            "additionalProperties": False,
        },
        func=submit_close_review,
    )


__all__ = [
    "_commit_close",
    "_evaluate_close",
    "_has_committable_edits",
    "register_close_task",
]
