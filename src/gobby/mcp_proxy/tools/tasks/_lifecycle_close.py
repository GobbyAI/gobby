"""Checklist-based task closure with one evaluation and one commit phase."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Literal

from gobby.mcp_proxy.tools._task_query_pagination import collect_task_query_pages
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    resolve_task_repo_path,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    CloseAttributionSnapshot,
    CloseEvaluationFingerprint,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    claimed_session_window_start as _claimed_session_window_start,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    committable_task_paths as _committable_task_paths,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    derive_close_transcript_evidence as _derive_close_transcript_evidence,
)
from gobby.mcp_proxy.tools.tasks._close_evaluation_support import (
    has_committable_edits as _has_committable_edits,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import (
    CloseEvaluation,
    link_close_commit_shas,
    resolve_close_commit_shas,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    active_validation_backoff,
    determine_close_outcome,
    evaluate_criteria_review,
    record_validation_infrastructure_failure,
    validate_commit_requirements,
    validate_parent_task,
)
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.mcp_proxy.tools.tasks._task_scope import evaluate_task_scope
from gobby.storage.tasks import Task, TaskNotFoundError, TaskStaleStateError
from gobby.tasks.close_checklist import evaluate_validation_commands
from gobby.tasks.commits import collect_commit_diff_text
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed
from gobby.tasks.transcript_evidence import (
    TranscriptEvidence,
    TranscriptEvidenceUnavailable,
)

logger = logging.getLogger(__name__)


def _apply_escalated_close_gate(
    evaluation: CloseEvaluation,
    override_justification: str | None,
) -> None:
    """Gate 11 for an escalated task: require justification, then skip review."""
    if not (override_justification or "").strip():
        evaluation.fail(
            11,
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
    evaluation.pass_gate(
        11,
        "criteria_review",
        "Skipped for a justified deliberate close of an escalated task.",
        skipped=True,
    )


def _children_state(
    ctx: RegistryContext,
    task_id: str,
) -> tuple[list[Task], tuple[tuple[str, str | None, bool], ...]]:
    """Return children and the gate-relevant structural state."""
    children = collect_task_query_pages(
        ctx.task_manager.list_tasks,
        parent_task_id=task_id,
    )
    state = tuple(
        sorted((child.id, child.parent_task_id, is_task_closed(child)) for child in children)
    )
    return children, state


async def _capture_attribution(
    ctx: RegistryContext,
    *,
    task: Task,
    task_id: str,
    resolved_session_id: str,
    repo_path: str,
) -> CloseAttributionSnapshot:
    """Capture the session-owned inputs used by close gates 7 through 9."""
    owner_session_id = get_claimed_session_id(task) or resolved_session_id
    session_vars = ctx.session_var_manager.get_variables(owner_session_id)

    from gobby.workflows.task_claim_state import target_task_has_edits, task_edited_file_set

    attributed = target_task_has_edits(session_vars, task_id)
    raw_paths = frozenset(task_edited_file_set(session_vars, task_id))
    edited_paths = frozenset(
        await asyncio.to_thread(
            _committable_task_paths,
            set(raw_paths),
            repo_path,
        )
    )
    return CloseAttributionSnapshot(
        owner_session_id=owner_session_id,
        attributed=attributed,
        raw_paths=raw_paths,
        edited_paths=edited_paths,
        had_attributed_edits=attributed and bool(edited_paths),
        claim_started_at=_claimed_session_window_start(
            ctx,
            task=task,
            resolved_id=task_id,
        ),
    )


async def _evaluate_close(
    ctx: RegistryContext,
    *,
    task_id: str,
    reason: str,
    changes_summary: str | None,
    commit_sha: str | None,
    project_path: str | None,
    response_detail: Literal["concise", "diagnostic"],
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

    session_ref = get_current_session_id() or get_claimed_session_id(task)
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
        evaluation.commit_shas, _commit_error = resolve_close_commit_shas(
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
            11,
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
    commit_shas, commit_error = resolve_close_commit_shas(
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
        commit_result = validate_commit_requirements(evaluation_task, reason, repo_path)
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

    if task.is_escalated:
        _apply_escalated_close_gate(evaluation, override_justification)
        return evaluation

    if ctx.task_validator is None:
        infra = record_validation_infrastructure_failure(
            task,
            ctx,
            resolved_id=resolved_id,
            message="The bounded task-close criteria reviewer is not configured.",
        )
        return evaluation.fail(
            11,
            "criteria_review",
            "validation_provider_unavailable",
            infra.message or "The bounded task-close criteria reviewer is not configured.",
            extra=infra.extra,
        )
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
            11,
            "criteria_review",
            infra.error_type or "validation_diff_unavailable",
            infra.message or str(exc),
            extra=infra.extra,
        )
    llm_result = await evaluate_criteria_review(
        task=evaluation_task,
        task_validator=ctx.task_validator,
        ctx=ctx,
        resolved_id=resolved_id,
        changes_summary=changes_summary or "",
        diff_text=diff_text,
        checklist_facts={
            "commit_count": len(commit_shas),
            "had_attributed_edits": evaluation.had_attributed_edits,
            "validation_commands": command_gate.details,
        },
        validation_config=ctx.validation_config,
        reason=reason,
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
            11,
            "criteria_review",
            llm_result.error_type or "validation_failed",
            message,
            extra=llm_result.extra,
        )
    evaluation.pass_gate(11, "criteria_review", "Bounded criteria review passed.")
    evaluation.extra.update(llm_result.extra)
    return evaluation


async def _commit_close(
    ctx: RegistryContext,
    evaluation: CloseEvaluation,
    *,
    reason: str,
    skip_validation: bool,
    override_justification: str | None,
    commit_sha: str | None,
) -> dict[str, Any]:
    """Apply close mutations after cheap concurrency-sensitive rechecks."""
    task = evaluation.task
    if task is None or evaluation.task_id is None:
        return evaluation.response(preview=False)
    fresh = ctx.task_manager.get_task(task.id)
    if fresh is None:
        return _stale_close_response(
            evaluation, "Task state changed after evaluation; retry close_task."
        )
    fresh_children, fresh_children_state = _children_state(ctx, task.id)
    fresh_skip_leaf_checks = bool(fresh_children) or fresh.task_type == "epic"
    fresh_attribution: CloseAttributionSnapshot | None = None
    if not fresh_skip_leaf_checks:
        if evaluation.resolved_session_id is None or evaluation.repo_path is None:
            return _stale_close_response(
                evaluation,
                "Close evaluation context is incomplete; retry close_task.",
            )
        try:
            fresh_attribution = await _capture_attribution(
                ctx,
                task=fresh,
                task_id=task.id,
                resolved_session_id=evaluation.resolved_session_id,
                repo_path=evaluation.repo_path,
            )
        except (KeyError, TypeError, ValueError):
            return _stale_close_response(
                evaluation,
                "Task edit attribution changed after evaluation; retry close_task.",
            )
    fresh_fingerprint = CloseEvaluationFingerprint.capture(
        fresh,
        children_state=fresh_children_state,
        attribution=fresh_attribution,
    )
    if evaluation.fingerprint is None or fresh_fingerprint != evaluation.fingerprint:
        return _stale_close_response(
            evaluation, "Task gate inputs changed after evaluation; retry close_task."
        )
    commit_shas, error = resolve_close_commit_shas(
        ctx.task_manager,
        task=fresh,
        task_id=task.id,
        claim_started_at=(
            fresh_attribution.claim_started_at if fresh_attribution is not None else None
        ),
        commit_sha=commit_sha,
        cwd=evaluation.repo_path,
        project_name=ctx.get_current_project_name(),
    )
    if error or commit_shas != evaluation.commit_shas:
        return _stale_close_response(
            evaluation,
            "The prospective commit set changed after evaluation; retry close_task.",
        )
    if not fresh_skip_leaf_checks:
        try:
            fresh_scope = await asyncio.to_thread(
                evaluate_task_scope,
                db=ctx.task_manager.db,
                task=fresh,
                commit_shas=commit_shas,
                attributed_paths=(
                    fresh_attribution.edited_paths if fresh_attribution is not None else ()
                ),
                repo_path=evaluation.repo_path,
                scope_justification=evaluation.scope_justification,
            )
        except RuntimeError:
            return _stale_close_response(
                evaluation,
                "Task scope inputs changed after evaluation; retry close_task.",
            )
        if fresh_scope.snapshot() != evaluation.scope_snapshot:
            return _stale_close_response(
                evaluation,
                "Task scope inputs changed after evaluation; retry close_task.",
            )
    fresh_edited_paths = (
        set(fresh_attribution.edited_paths) if fresh_attribution is not None else set()
    )
    has_dirty_edits = bool(fresh_edited_paths and evaluation.repo_path) and (
        await asyncio.to_thread(
            _has_committable_edits,
            fresh_edited_paths,
            evaluation.repo_path or "",
        )
    )
    if has_dirty_edits:
        return _stale_close_response(
            evaluation,
            "Task-attributed files changed after evaluation; commit them and retry close_task.",
        )
    linked, link_error = link_close_commit_shas(
        ctx.task_manager,
        task=fresh,
        commit_shas=commit_shas,
        cwd=evaluation.repo_path,
    )
    if link_error:
        evaluation.error = str(link_error["error"])
        evaluation.message = str(link_error["message"])
        return evaluation.response(preview=False)

    _route, store_override = determine_close_outcome(
        linked,
        skip_validation and evaluation.skip_leaf_checks,
        override_justification,
    )
    audit_reason = (
        override_justification.strip() if store_override and override_justification else None
    )
    if evaluation.scope_justification:
        scope_reason = f"Task scope justification: {evaluation.scope_justification}"
        audit_reason = (
            f"Validation override: {audit_reason}\n\n{scope_reason}"
            if audit_reason
            else scope_reason
        )
    current_commit_sha = commit_shas[-1] if commit_shas else None
    closed_ancestors: list[str] = []
    try:
        ctx.task_manager.close_task(
            task.id,
            reason=reason,
            closed_in_session_id=evaluation.resolved_session_id,
            closed_commit_sha=current_commit_sha,
            closed_ancestors=closed_ancestors,
            validation_override_reason=audit_reason,
            expected_updated_at=linked.updated_at,
            reset_validation_fail_count=evaluation.validation_reset_reason is not None,
            validation_status=evaluation.validation_status or "valid",
            validation_feedback=evaluation.validation_feedback,
        )
    except TaskStaleStateError as exc:
        return _stale_close_response(evaluation, str(exc))

    ancestor_summaries = _record_closed_ancestors(ctx, closed_ancestors, reason)
    if ancestor_summaries:
        evaluation.extra["closed_ancestors"] = ancestor_summaries

    if evaluation.is_epic and reason.casefold() in {"completed", "obsolete"}:
        from gobby.hooks.event_handlers._plan import on_epic_terminal

        on_epic_terminal(
            {
                "task_ref": f"#{task.seq_num}" if task.seq_num else task.id,
                "project_id": task.project_id,
                "status": "closed",
                "closure_reason": reason.casefold(),
            },
            db=ctx.task_manager.db,
        )
    notify_parent_on_task_state_change(
        ctx.task_manager.db,
        task.id,
        "closed",
        task_ref=f"#{task.seq_num}" if task.seq_num else None,
    )
    _cleanup_closed_claim(ctx, evaluation, commit_shas)
    return evaluation.response(preview=False, closed=True)


def _record_closed_ancestors(
    ctx: RegistryContext,
    ancestor_ids: list[str],
    reason: str,
) -> list[dict[str, str]]:
    del reason
    summaries: list[dict[str, str]] = []
    for ancestor_id in ancestor_ids:
        ancestor = ctx.task_manager.get_task(ancestor_id)
        if ancestor is None:
            continue
        ref = f"#{ancestor.seq_num}" if ancestor.seq_num else ancestor.id
        summaries.append({"id": ancestor.id, "ref": ref, "title": ancestor.title})
        notify_parent_on_task_state_change(
            ctx.task_manager.db,
            ancestor.id,
            "closed",
            task_ref=ref,
        )
    return summaries


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


def _stale_close_response(evaluation: CloseEvaluation, message: str) -> dict[str, Any]:
    evaluation.error = "stale_task_state"
    evaluation.message = message
    evaluation.action = "Retry close_task; the existing evaluation will not be reused."
    evaluation.extra["stale_state"] = True
    return evaluation.response(preview=False)


def _cleanup_closed_claim(
    ctx: RegistryContext,
    evaluation: CloseEvaluation,
    commit_shas: list[str],
) -> None:
    if evaluation.resolved_session_id and evaluation.task_id:
        try:
            ctx.session_task_manager.link_task(
                evaluation.resolved_session_id,
                evaluation.task_id,
                "closed",
            )
        except Exception as exc:
            logger.debug("Best-effort session close link failed: %s", exc)
    if not evaluation.edit_session_id or not evaluation.task_id:
        return
    try:
        from gobby.workflows.task_claim_state import remove_claimed_task

        variables = ctx.session_var_manager.get_variables(evaluation.edit_session_id)
        updates = remove_claimed_task(variables, evaluation.task_id)
        remaining = updates.get("task_edited_files")
        ctx.session_var_manager.merge_variables(evaluation.edit_session_id, updates)
        if commit_shas and not remaining:
            ctx.session_manager.clear_had_edits(evaluation.edit_session_id)
    except Exception as exc:
        logger.warning("Failed to clean closed-task claim state: %s", exc)


__all__ = [
    "_claimed_session_window_start",
    "_commit_close",
    "_evaluate_close",
    "_has_committable_edits",
    "register_close_task",
]
