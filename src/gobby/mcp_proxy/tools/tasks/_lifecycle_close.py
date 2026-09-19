"""Checklist-based task closure with one evaluation and one commit phase."""

from __future__ import annotations

import logging
from dataclasses import replace
from time import perf_counter
from typing import Literal

import gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization as close_finalization
from gobby.mcp_proxy.tools.task_repo_paths import (
    CloseWorktreeRoot,
    RepoPathValidationError,
    resolve_close_worktree_root_async,
    resolve_task_repo_path,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    CloseEvaluationFingerprint,
    format_git_since,
    task_edit_languages,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    closes_as_structural_parent as _closes_as_structural_parent,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence as _derive_close_transcript_evidence,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import (
    capture_attribution as _capture_attribution,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import (
    children_state as _children_state,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_finalization import commit_close as _commit_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import (
    CloseEvaluation,
    resolve_close_commit_shas,
    unlinked_tagged_commits,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import (
    SubmittedCloseReview,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import (
    evaluate_close_criteria as evaluate_criteria_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    active_validation_backoff,
    apply_task_cleanliness_gate,
    record_validation_infrastructure_failure,
    validate_commit_requirements,
    validate_parent_task,
)
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.mcp_proxy.tools.tasks._task_scope import (
    collect_commit_paths_async as collect_commit_paths,
)
from gobby.mcp_proxy.tools.tasks._task_scope import (
    evaluate_task_scope_async as evaluate_task_scope,
)
from gobby.sessions.machine_scope import RemoteSessionOwnershipError
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.tasks import Task, TaskNotFoundError
from gobby.tasks.acceptance_artifacts import (
    evaluate_acceptance_artifacts_async as evaluate_acceptance_artifacts,
)
from gobby.tasks.acceptance_artifacts import (
    extract_artifact_references,
    render_acceptance_test_bodies,
)
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.commits import collect_commit_diff_text_async as collect_commit_diff_text
from gobby.tasks.criteria_contract import operational_actions_from_command
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.tasks.tdd_evidence import evaluate_tdd_evidence, task_requires_tdd
from gobby.tasks.transcript_evidence_models import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
)
from gobby.tasks.validation import NO_WORK_CLOSE_REASONS

_DELIBERATE_CLOSE_SKIP = "Skipped for a justified deliberate close of an escalated task."
logger = logging.getLogger(__name__)


def _acceptance_root_diagnostic(
    finding: str,
    *,
    criteria: str,
    resolved_references: set[str],
    repo_path: str,
    close_root: CloseWorktreeRoot,
    project_path: str | None,
) -> str:
    """Name the evaluated root when a named test did not resolve outside the task worktree."""
    unresolved = [
        reference
        for reference in extract_artifact_references(criteria, "test")
        if reference not in resolved_references
    ]
    # With nothing registered there is no worktree or clone path to pass, so the
    # suggestion names a remedy the caller cannot supply and sends them hunting
    # for a worktree that does not exist (#21237).
    if (
        not unresolved
        or project_path is not None
        or close_root.applies
        or close_root.worktree_path is None
    ):
        return finding
    return (
        f"{finding} Named test {', '.join(unresolved)} did not resolve in {repo_path}; "
        f"{close_root.skip_reason}. Pass project_path=<registered worktree or clone path> "
        "to evaluate the task branch there."
    )


def _is_deliberate_close(task: Task, override_justification: str | None) -> bool:
    """Whether a human has explicitly decided this escalated task closes."""
    return task.is_escalated and bool((override_justification or "").strip())


def _apply_escalated_close_gate(
    evaluation: CloseEvaluation,
    override_justification: str | None,
) -> None:
    """Gate 13 for an escalated task: require justification, then skip review."""
    if not (override_justification or "").strip():
        evaluation.fail(
            13,
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
    evaluation.pass_gate(13, "criteria_review", _DELIBERATE_CLOSE_SKIP, skipped=True)


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
        return evaluation.fail(1, "task_exists", "task_not_found", str(exc)).block_remaining()
    task = ctx.task_manager.get_task(resolved_id)
    if task is None:
        return evaluation.fail(
            1, "task_exists", "task_not_found", f"Task {task_id} not found."
        ).block_remaining()
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
        ).block_remaining()
    try:
        resolved_session_id = ctx.resolve_session_id(session_ref)
    except ValueError as exc:
        return evaluation.fail(
            2,
            "session_context",
            "session_resolution_failed",
            f"Cannot resolve close session {session_ref!r}: {exc}",
        ).block_remaining()
    evaluation.resolved_session_id = resolved_session_id
    evaluation.pass_gate(2, "session_context", "Close session resolved.")

    close_session = ctx.session_manager.get(resolved_session_id)
    if close_session is None or not close_session.machine_id:
        return evaluation.fail(
            3,
            "repository_path",
            "session_machine_missing",
            "close_task requires the resolved session to have a machine_id.",
        ).block_remaining()
    try:
        repo_path = resolve_task_repo_path(
            task_manager=ctx.task_manager,
            project_manager=ctx.project_manager,
            task=task,
            project_path=project_path,
            machine_id=close_session.machine_id,
        )
    except CheckoutNotFoundError:
        return evaluation.fail(
            3,
            "repository_path",
            "task_repo_path_unavailable",
            "close_task requires a registered repository path.",
        ).block_remaining()
    except RepoPathValidationError as exc:
        return evaluation.fail(
            3, "repository_path", "invalid_project_path", str(exc)
        ).block_remaining()
    except ValueError as exc:
        return evaluation.fail(
            3, "repository_path", "invalid_project_path", str(exc)
        ).block_remaining()
    if repo_path is None:
        return evaluation.fail(
            3,
            "repository_path",
            "task_repo_path_unavailable",
            "close_task requires a registered repository path.",
        ).block_remaining()
    if project_path is None:
        close_root = await resolve_close_worktree_root_async(
            task_manager=ctx.task_manager,
            task=task,
            commit_shas=[*(task.commits or []), *([commit_sha] if commit_sha else [])],
        )
    else:
        close_root = CloseWorktreeRoot(None, None, "project_path was supplied")
    if close_root.repo_path is not None:
        repo_path = close_root.repo_path
    evaluation.repo_path = repo_path
    evaluation.pass_gate(
        3,
        "repository_path",
        f"Task repository resolved to the registered worktree {repo_path}."
        if close_root.applies
        else "Task repository resolved.",
    )
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
            ).block_remaining()
    evaluation.is_epic = task.task_type == "epic"
    evaluation.skip_leaf_checks = _closes_as_structural_parent(task, has_children=has_children)
    evaluation.pass_gate(4, "children_closed", "Every child task is closed.")
    if evaluation.skip_leaf_checks:
        evaluation.fingerprint = CloseEvaluationFingerprint.capture(
            task,
            children_state=children_state,
            attribution=None,
        )
        evaluation.commit_shas, _commit_error = await resolve_close_commit_shas(
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
            13,
            "criteria_review",
            "Skipped for an epic or structural parent.",
            skipped=True,
        )
        return evaluation

    # Gates 5 and 6 read nothing but the task row and the call arguments, so a failure
    # in either leaves every later gate evaluable and both are reported together.
    if (task.validation_criteria or "").strip():
        evaluation.pass_gate(5, "criteria_present", "Validation criteria are present.")
    else:
        evaluation.collect_failure(
            5,
            "criteria_present",
            "missing_validation_criteria",
            "Leaf tasks require explicit validation criteria before closing.",
        )
    if (changes_summary or "").strip():
        evaluation.pass_gate(6, "changes_summary_present", "Changes summary is present.")
    else:
        evaluation.collect_failure(
            6,
            "changes_summary_present",
            "missing_changes_summary",
            "Leaf tasks require changes_summary describing what changed and why.",
        )

    claim_started_at = close_finalization.claim_window_start(
        ctx,
        task=task,
        resolved_id=resolved_id,
    )
    commit_shas, commit_error = await resolve_close_commit_shas(
        ctx.task_manager,
        task=task,
        task_id=resolved_id,
        claim_started_at=claim_started_at,
        commit_sha=commit_sha,
        cwd=repo_path,
        project_name=ctx.get_current_project_name(),
    )
    evaluation.commit_shas = commit_shas
    # An unresolved commit set is a prerequisite failure: every later gate judges the
    # delivered change against it, so none of them can be evaluated without it.
    if commit_error:
        return evaluation.fail(
            7,
            "linked_commits",
            str(commit_error["error"]),
            str(commit_error["message"]),
        ).block_remaining()
    # Scan for tagged commits the review would never see: in #21451 five review
    # failures and an escalation stood in for this one git scan.
    (unlinked_on_head, tagged_elsewhere), tagged_error = await unlinked_tagged_commits(
        ctx.task_manager,
        task=task,
        task_id=resolved_id,
        commit_shas=commit_shas,
        cwd=repo_path,
        project_name=ctx.get_current_project_name(),
    )
    if tagged_error:
        return evaluation.fail(
            7, "linked_commits", str(tagged_error["error"]), str(tagged_error["message"])
        ).block_remaining()
    tagged_details = (
        {"other_ref_tagged_commit_shas": tagged_elsewhere} if tagged_elsewhere else None
    )
    if tagged_details:
        evaluation.extra.update(tagged_details)
    commits_linked = True
    if unlinked_on_head:
        commits_linked = False
        evaluation.collect_failure(
            7,
            "linked_commits",
            "unlinked_tagged_commits",
            f"Commits tagged for this task are not linked: {', '.join(unlinked_on_head)}. "
            "The criteria review would judge an incomplete diff.",
            action=(
                "Link each listed commit with link_commit(task_id, commit_sha), or run "
                f"auto_link_commits(task_id, since={format_git_since(task.created_at)!r}) "
                "and unlink_commit any linked commit that does not belong; then retry "
                "close_task."
            ),
            details=tagged_details,
            extra={"unlinked_tagged_commit_shas": unlinked_on_head},
        )

    try:
        attribution = await _capture_attribution(
            ctx,
            task=task,
            task_id=resolved_id,
            resolved_session_id=resolved_session_id,
            repo_path=repo_path,
            prospective_commit_shas=tuple(commit_shas),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return evaluation.fail(
            2,
            "session_context",
            "session_variable_lookup_failed",
            f"Cannot read task edit attribution from the owning session: {exc}",
        ).block_remaining()
    evaluation.edit_session_id = attribution.owner_session_id
    if attribution.attributed and not attribution.raw_paths:
        return evaluation.fail(
            9,
            "uncommitted_task_edits",
            "task_edit_paths_unavailable",
            "The task records edits but no attributed file paths. Restore task edit state and retry.",
        ).block_remaining()
    evaluation.edited_paths = set(attribution.edited_paths)
    evaluation.had_attributed_edits = attribution.had_attributed_edits
    evaluation.claim_started_at = attribution.claim_started_at
    evaluation.fingerprint = CloseEvaluationFingerprint.capture(
        task,
        children_state=children_state,
        attribution=attribution,
    )
    evaluation_task = replace(task, commits=commit_shas or None)
    if evaluation.had_attributed_edits:
        commit_result = await validate_commit_requirements(evaluation_task, reason, repo_path)
        if not commit_result.can_close:
            commit_extra = dict(commit_result.extra)
            if evaluation.response_detail == "diagnostic":
                commit_extra["attributed_paths"] = sorted(evaluation.edited_paths)
            commits_linked = False
            evaluation.collect_failure(
                7,
                "linked_commits",
                commit_result.error_type or "commit_validation_failed",
                commit_result.message or "Link a commit for the attributed task edits.",
                extra=commit_extra,
            )
    if commits_linked:
        evaluation.pass_gate(
            7,
            "linked_commits",
            "Attributed edits have a linked commit."
            if evaluation.had_attributed_edits
            else "No attributed committable edits require a commit.",
            details=tagged_details,
            skipped=not evaluation.had_attributed_edits,
        )

    try:
        scope = await evaluate_task_scope(
            db=ctx.task_manager.db,
            task=task,
            commit_shas=commit_shas,
            attributed_paths=evaluation.edited_paths,
            repo_path=repo_path,
            scope_justification=scope_justification,
        )
    except RuntimeError as exc:
        evaluation.collect_failure(
            8,
            "task_scope",
            "task_scope_unavailable",
            f"Task scope cannot be evaluated: {exc}",
        )
        scope = None
    if scope is not None:
        evaluation.scope_snapshot = scope.snapshot()
        evaluation.scope_justification = scope.scope_justification
        if scope.advisory_scope_drift:
            evaluation.extra["advisory_scope_drift"] = list(scope.advisory_scope_drift)
        if not scope.accepted:
            evaluation.collect_failure(
                8,
                "task_scope",
                "task_scope_mismatch",
                scope.justification_error or "Task changes exceed the declared scope.",
                action=(
                    "Pass a specific scope_justification between 20 and 1000 characters "
                    "that explains why the listed paths belong in this task."
                ),
                details=scope.details(),
                # Concise responses carry no checklist, so the scope inventory rides
                # here; a diagnostic response drops this copy because the gate
                # details already carry the identical section.
                extra=scope.details(),
            )
        else:
            evaluation.pass_gate(
                8,
                "task_scope",
                "Out-of-scope paths have a recorded justification."
                if scope.has_mismatch
                else "Delivered paths stay within the declared task scope.",
                details=scope.details(),
                skipped=not scope.declared_paths,
            )

    await apply_task_cleanliness_gate(
        ctx,
        evaluation,
        edited_paths=attribution.clean_proof_paths,
        owner_session_id=attribution.owner_session_id,
        project_id=task.project_id,
        repo_path=repo_path,
    )

    try:
        committed_paths = await collect_commit_paths(commit_shas, repo_path)
    except RuntimeError as exc:
        return evaluation.fail(
            10,
            "validation_commands",
            "validation_paths_unavailable",
            f"Cannot determine changed paths for validation requirements: {exc}",
        ).block_remaining()
    validation_paths = evaluation.edited_paths | committed_paths
    transcript = TranscriptEvidence()
    command_gate = replace(
        evaluate_validation_commands(
            task_category=task.category,
            evidence=TranscriptEvidence(),
            has_attributed_edits=evaluation.had_attributed_edits,
            changed_paths=validation_paths,
        ),
        item=10,
    )
    commands_required = command_gate.status != "skipped"
    if commands_required:
        backoff = active_validation_backoff(task, ctx)
        if backoff is not None:
            return evaluation.fail(
                10,
                "validation_commands",
                backoff.error_type or "validation_infrastructure_unavailable",
                backoff.message or "Validation infrastructure is unavailable.",
                extra=backoff.extra,
            ).block_remaining()
    if commands_required or (
        task.validation_criteria and not task.is_escalated and reason not in NO_WORK_CLOSE_REASONS
    ):
        try:
            transcript = await _derive_close_transcript_evidence(
                ctx,
                task_id=resolved_id,
                owner_session_id=attribution.owner_session_id,
                closing_session_id=resolved_session_id,
                owner_window_start=evaluation.claim_started_at,
                task_edited_files=evaluation.edited_paths,
                repo_path=repo_path,
                require_task_link=not evaluation.had_attributed_edits,
            )
        except (TranscriptEvidenceUnavailable, RemoteSessionOwnershipError) as exc:
            if commands_required and isinstance(exc, RemoteSessionOwnershipError):
                raise
            attempted_paths = (
                exc.attempted_paths if isinstance(exc, TranscriptEvidenceUnavailable) else ()
            )
            message = (
                f"Task-close transcript evidence is unavailable: {exc}. "
                f"Attempted paths: {', '.join(attempted_paths) or 'none'}."
            )
            if commands_required:
                infra = record_validation_infrastructure_failure(
                    task,
                    ctx,
                    resolved_id=resolved_id,
                    message=message,
                    error_type="validation_evidence_unavailable",
                )
                return evaluation.fail(
                    10,
                    "validation_commands",
                    infra.error_type or "validation_evidence_unavailable",
                    infra.message or str(exc),
                    extra=infra.extra,
                ).block_remaining()
            transcript = TranscriptEvidence(
                attempted_paths=tuple(attempted_paths),
                degraded_capabilities=(message,),
            )
        transcript = replace(
            transcript,
            edit_languages=task_edit_languages(ctx, task.project_id, transcript.edits),
        )
        evaluation.transcript_evidence = transcript.summary()
        command_gate = replace(
            evaluate_validation_commands(
                task_category=task.category,
                evidence=transcript,
                has_attributed_edits=evaluation.had_attributed_edits,
                validation_criteria=task.validation_criteria or "",
                changed_paths=validation_paths,
            ),
            item=10,
        )
    evaluation.extra["validation_commands"] = command_gate.details
    if command_gate.passed:
        evaluation.gates.append(command_gate)
    else:
        evaluation.record_gate_failure(command_gate, error="validation_command_required")

    acceptance_details: dict[str, object] = {
        "findings": [],
        "test_references": [],
        "evidence_files": [],
    }
    tdd_details: dict[str, object] = {"findings": [], "red_runs": [], "green_runs": []}
    test_bodies = "Named acceptance tests: none."
    # Gates 11 and 12 resolve what the criteria name out of the linked commit set, so
    # with either input already blocked they would report a missing artifact that the
    # blocker, not the deliverable, made unresolvable.
    artifacts_blocked_by = evaluation.failed_gate("criteria_present", "linked_commits")
    if reason in NO_WORK_CLOSE_REASONS:
        for item, name in (
            (11, "acceptance_artifacts"),
            (12, "tdd_evidence"),
        ):
            evaluation.pass_gate(
                item,
                name,
                "Skipped for a canonical no-work disposition.",
                skipped=True,
            )
    elif artifacts_blocked_by is not None:
        evaluation.skip_gate(11, "acceptance_artifacts", blocked_by=artifacts_blocked_by)
        evaluation.skip_gate(12, "tdd_evidence", blocked_by=artifacts_blocked_by)
    else:
        artifacts = await evaluate_acceptance_artifacts(
            criteria=task.validation_criteria or "",
            repo_path=repo_path,
            commit_shas=commit_shas,
        )
        acceptance_details = artifacts.details()
        if not artifacts.passed:
            evaluation.collect_failure(
                11,
                "acceptance_artifacts",
                "acceptance_artifacts_invalid",
                _acceptance_root_diagnostic(
                    artifacts.findings[0],
                    criteria=task.validation_criteria or "",
                    resolved_references={test.reference for test in artifacts.tests},
                    repo_path=repo_path,
                    close_root=close_root,
                    project_path=project_path,
                ),
                details=acceptance_details,
                extra={"acceptance_artifacts": acceptance_details},
            )
            # Gate 12 judges red and green evidence for the tests gate 11 resolved, so on
            # an unresolved set it would demand runs of a test that never resolved.
            evaluation.skip_gate(12, "tdd_evidence", blocked_by="acceptance_artifacts")
        else:
            evaluation.pass_gate(
                11,
                "acceptance_artifacts",
                "Named acceptance artifacts passed deterministic checks.",
                details=acceptance_details,
                skipped=not artifacts.tests and not artifacts.evidence_files,
            )
            test_bodies = render_acceptance_test_bodies(artifacts.tests)
            if task_requires_tdd(
                labels=task.labels or (),
                additional_skills=task.additional_skills or (),
                validation_criteria=task.validation_criteria,
            ):
                tdd = evaluate_tdd_evidence(artifacts.tests, transcript)
                tdd_details = tdd.details()
                # Gate 12 and gate 13 both ask whether the loop was followed rather than
                # whether the deliverable is sound, so a justified deliberate close waives
                # them together. The delivery gates above stay hard: a waived close still
                # proves the work is committed, in scope, clean, and validated.
                waive_tdd = not tdd.passed and _is_deliberate_close(task, override_justification)
                if not tdd.passed and not waive_tdd:
                    evaluation.collect_failure(
                        12,
                        "tdd_evidence",
                        "tdd_evidence_missing",
                        tdd.findings[0],
                        details=tdd_details,
                        extra={"tdd_evidence": tdd_details},
                    )
                else:
                    evaluation.pass_gate(
                        12,
                        "tdd_evidence",
                        _DELIBERATE_CLOSE_SKIP
                        if waive_tdd
                        else (
                            "Every named acceptance test has assertion-backed red and later "
                            "green evidence."
                        ),
                        details=tdd_details,
                        skipped=tdd.skipped or waive_tdd,
                    )
            else:
                evaluation.pass_gate(
                    12,
                    "tdd_evidence",
                    "Skipped because task metadata does not require TDD evidence.",
                    details=tdd_details,
                    skipped=True,
                )

    if task.is_escalated:
        _apply_escalated_close_gate(evaluation, override_justification)
        return evaluation

    blocker = evaluation.checklist.first_failure
    if blocker is not None:
        # Gate 13 spends a paid validator run, so it is the one gate that never starts
        # while a deterministic blocker is still on the checklist.
        evaluation.skip_gate(13, "criteria_review", blocked_by=blocker.name)
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
            13,
            "criteria_review",
            "validation_provider_unavailable",
            infra.message or "The task-close criteria reviewer is not configured.",
            extra=infra.extra,
        )

    try:
        diff_text = await collect_commit_diff_text(commit_shas, cwd=repo_path)
    except RuntimeError as exc:
        infra = record_validation_infrastructure_failure(
            task,
            ctx,
            resolved_id=resolved_id,
            message=f"Validation diff is unavailable: {exc}",
            error_type="validation_diff_unavailable",
        )
        return evaluation.fail(
            13,
            "criteria_review",
            infra.error_type or "validation_diff_unavailable",
            infra.message or str(exc),
            extra=infra.extra,
        )

    review_started = perf_counter()
    llm_result = await evaluate_criteria_review(
        task=evaluation_task,
        task_validator=task_validator,
        ctx=ctx,
        resolved_id=resolved_id,
        changes_summary=changes_summary or "",
        diff_text=diff_text,
        checklist_facts={
            "commit_count": len(commit_shas),
            "commit_shas": commit_shas,
            "had_attributed_edits": evaluation.had_attributed_edits,
            "attributed_paths": sorted(evaluation.edited_paths),
            "claim_started_at": evaluation.claim_started_at,
            "validation_commands": command_gate.details,
            "transcript_operational_actions": sorted(
                {
                    action
                    for run in transcript.validation_runs
                    if run.outcome == "success"
                    for action in operational_actions_from_command(run.command)
                }
            ),
            "acceptance_artifacts": acceptance_details,
            "tdd_evidence": tdd_details,
        },
        validation_config=ctx.validation_config,
        reason=reason,
        description=task.description or "",
        test_bodies=test_bodies,
        closing_session_id=resolved_session_id,
        submitted_review=submitted_review,
    )
    review_duration_ms = round((perf_counter() - review_started) * 1_000, 3)
    logger.info(
        "Task close gate 13 completed in %.3f ms for task %s (mode=%s)",
        review_duration_ms,
        resolved_id,
        "submitted" if submitted_review is not None else "detached",
    )
    llm_result.extra["criteria_review_duration_ms"] = review_duration_ms
    evaluation.validation_status = llm_result.validation_status
    evaluation.validation_feedback = llm_result.validation_feedback
    evaluation.validation_reset_reason = llm_result.reset_reason
    evaluation.verdict = llm_result.extra.get("verdict")
    if not llm_result.can_close:
        extra = dict(llm_result.extra)
        # The verdict's own reasons and actions become this gate's blockers; leaving
        # them in extra would restate every one of them a second time in the response.
        raw_reasons = extra.pop("blocking_reasons", None)
        raw_actions = extra.pop("required_actions", None)
        reasons = [str(reason) for reason in raw_reasons] if isinstance(raw_reasons, list) else None
        actions = [str(action) for action in raw_actions] if isinstance(raw_actions, list) else None
        message = reasons[0] if reasons else llm_result.message or "Criteria review did not pass."
        return evaluation.fail(
            13,
            "criteria_review",
            llm_result.error_type or "validation_failed",
            message,
            reasons=reasons,
            actions=actions,
            extra=extra,
        )
    evaluation.pass_gate(13, "criteria_review", "Task-close criteria review passed.")
    evaluation.extra.update(llm_result.extra)
    return evaluation


__all__ = [
    "_commit_close",
    "_evaluate_close",
]
