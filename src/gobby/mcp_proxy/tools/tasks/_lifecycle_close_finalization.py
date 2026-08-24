"""Concurrency rechecks and mutations for checklist-based task closure."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools._task_query_pagination import collect_task_query_pages
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
    has_committable_edits as _has_committable_edits,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import (
    CloseEvaluation,
    link_close_commit_shas,
    resolve_close_commit_shas,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import determine_close_outcome
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.mcp_proxy.tools.tasks._task_scope import collect_commit_paths, evaluate_task_scope
from gobby.storage.tasks import Task, TaskStaleStateError
from gobby.tasks.state_semantics import get_claimed_session_id, is_task_closed

logger = logging.getLogger(__name__)


def children_state(
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


def _linked_commit_paths(task: Task, repo_path: str) -> frozenset[str]:
    """Return the paths this task's linked commits changed, or nothing on failure."""
    if not task.commits:
        return frozenset()
    try:
        return frozenset(collect_commit_paths(task.commits, repo_path))
    except RuntimeError as exc:
        # A sha git cannot inspect here (rebased away, wrong checkout) leaves the
        # checklist exactly where it was before this fallback existed. Gate 7 and
        # gate 8 still report the broken commit set on their own terms.
        logger.debug("Cannot resolve linked commit paths for task %s: %s", task.id, exc)
        return frozenset()


async def capture_attribution(
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
    if not raw_paths:
        # Session variables are a volatile cache of what the task edited: escalation,
        # dead-session recovery, and a fresh claiming session all leave them empty for
        # a task that really did edit files. Linked commits are the durable record, so
        # fall back to them instead of reading committed work as a no-edit close --
        # which would skip gate 10 and starve gate 12 of transcript evidence.
        raw_paths = await asyncio.to_thread(_linked_commit_paths, task, repo_path)
        attributed = attributed or bool(raw_paths)
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


async def commit_close(
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
        return stale_close_response(
            evaluation, "Task state changed after evaluation; retry close_task."
        )
    fresh_children, fresh_children_state = children_state(ctx, task.id)
    fresh_skip_leaf_checks = bool(fresh_children) or fresh.task_type == "epic"
    fresh_attribution: CloseAttributionSnapshot | None = None
    if not fresh_skip_leaf_checks:
        if evaluation.resolved_session_id is None or evaluation.repo_path is None:
            return stale_close_response(
                evaluation,
                "Close evaluation context is incomplete; retry close_task.",
            )
        try:
            fresh_attribution = await capture_attribution(
                ctx,
                task=fresh,
                task_id=task.id,
                resolved_session_id=evaluation.resolved_session_id,
                repo_path=evaluation.repo_path,
            )
        except (KeyError, TypeError, ValueError):
            return stale_close_response(
                evaluation,
                "Task edit attribution changed after evaluation; retry close_task.",
            )
    fresh_fingerprint = CloseEvaluationFingerprint.capture(
        fresh,
        children_state=fresh_children_state,
        attribution=fresh_attribution,
    )
    if evaluation.fingerprint is None or fresh_fingerprint != evaluation.fingerprint:
        return stale_close_response(
            evaluation, "Task gate inputs changed after evaluation; retry close_task."
        )
    # Off the loop: the re-resolve forks git per sha, same as the evaluation's
    # own call did before #20861.
    commit_shas, error = await asyncio.to_thread(
        resolve_close_commit_shas,
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
        return stale_close_response(
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
            return stale_close_response(
                evaluation,
                "Task scope inputs changed after evaluation; retry close_task.",
            )
        if fresh_scope.snapshot() != evaluation.scope_snapshot:
            return stale_close_response(
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
        return stale_close_response(
            evaluation,
            "Task-attributed files changed after evaluation; commit them and retry close_task.",
        )
    # Off the loop: this reaches git by its own route -- the storage layer's
    # link_commit -> normalize_commit_sha -> run_git_command -> subprocess.run --
    # which is why #20861's three offloads did not cover it (#20862).
    linked, link_error = await asyncio.to_thread(
        link_close_commit_shas,
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
        # Off the loop: the transition runs synchronous psycopg, and
        # _close_eligible_ancestors walks up the tree inside the transaction it
        # holds open, so the wait grows with depth and sibling count (#20862).
        # closed_ancestors is filled in place, so the worker thread's writes are
        # visible here once the await returns.
        await asyncio.to_thread(
            ctx.task_manager.close_task,
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
        return stale_close_response(evaluation, str(exc))

    ancestor_summaries = _record_closed_ancestors(ctx, closed_ancestors)
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
) -> list[dict[str, str]]:
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


def stale_close_response(evaluation: CloseEvaluation, message: str) -> dict[str, Any]:
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


__all__ = ["capture_attribution", "children_state", "commit_close", "stale_close_response"]
