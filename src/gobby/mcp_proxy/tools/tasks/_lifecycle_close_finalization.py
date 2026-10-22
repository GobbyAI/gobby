"""Atomic finalization after rechecking the same four close invariants."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._notifications import notify_parent_on_task_state_change
from gobby.storage.tasks import TaskHasOpenChildrenError, TaskStaleStateError

logger = logging.getLogger(__name__)


async def commit_close(
    ctx: RegistryContext,
    evaluation: CloseEvaluation,
    *,
    reason: str,
    changes_summary: str,
    commit_sha: str | None,
) -> dict[str, Any]:
    from gobby.mcp_proxy.tools.tasks._lifecycle_close import _evaluate_close

    if not evaluation.ready or evaluation.task is None:
        return evaluation.response(preview=False)
    fresh = await _evaluate_close(
        ctx,
        task_id=evaluation.task_id,
        reason=reason,
        commit_sha=commit_sha,
        project_path=evaluation.repo_path,
        response_detail=evaluation.response_detail,
        closing_session_id=evaluation.resolved_session_id,
    )
    if fresh.already_closed or not fresh.ready:
        return fresh.response(preview=False)
    task = fresh.task
    if task is None or task.updated_at != evaluation.task.updated_at:
        return evaluation.refuse("close_state_changed", "Task changed; retry close_task.").response(
            preview=False
        )
    if fresh.commit_shas != evaluation.commit_shas:
        return evaluation.refuse(
            "close_state_changed", "Linked commits changed; retry close_task."
        ).response(preview=False)
    ancestors: list[str] = []
    try:
        if commit_sha is not None and fresh.candidate_commit_sha is not None:
            await asyncio.to_thread(
                ctx.task_manager.close_task_with_commit,
                task.id,
                fresh.candidate_commit_sha,
                reason=reason,
                closed_in_session_id=fresh.resolved_session_id,
                cwd=fresh.repo_path,
                expected_updated_at=task.updated_at,
                closed_ancestors=ancestors,
            )
        else:
            await asyncio.to_thread(
                ctx.task_manager.close_task,
                task.id,
                reason=reason,
                closed_in_session_id=fresh.resolved_session_id,
                closed_commit_sha=fresh.candidate_commit_sha,
                expected_updated_at=task.updated_at,
                closed_ancestors=ancestors,
            )
    except (TaskStaleStateError, TaskHasOpenChildrenError) as exc:
        return fresh.refuse("close_state_changed", str(exc)).response(preview=False)

    await asyncio.to_thread(
        _queue_close_memory_review, ctx, fresh, task.id, changes_summary, reason, fresh.commit_shas
    )
    summaries = await asyncio.to_thread(_collect_closed_ancestors, ctx, ancestors)
    if summaries:
        fresh.extra["closed_ancestors"] = summaries
    for summary in summaries:
        notify_parent_on_task_state_change(
            ctx.task_manager.db, summary["id"], "closed", task_ref=summary["ref"]
        )
    if fresh.is_epic and reason in {"done", "moot"}:
        from gobby.hooks.event_handlers._plan import on_epic_terminal

        await asyncio.to_thread(
            on_epic_terminal,
            {
                "task_ref": f"#{task.seq_num}" if task.seq_num else task.id,
                "project_id": task.project_id,
                "status": "closed",
                "closure_reason": reason.casefold(),
            },
            db=ctx.task_manager.db,
        )
    # The notifier schedules a coroutine and must run on the event-loop thread.
    notify_parent_on_task_state_change(
        ctx.task_manager.db,
        task.id,
        "closed",
        task_ref=f"#{task.seq_num}" if task.seq_num else None,
    )
    await asyncio.to_thread(_cleanup_closed_claim, ctx, fresh, fresh.commit_shas)
    return fresh.response(preview=False, closed=True)


def _queue_close_memory_review(
    ctx: RegistryContext,
    evaluation: CloseEvaluation,
    task_id: str,
    changes_summary: str,
    reason: str,
    commit_shas: list[str],
) -> None:
    """Queue best-effort review after closure, retaining pre-close attribution."""
    from gobby.agents.session import ChildSessionManager
    from gobby.workflows.memory_review_conditions import classify_memory_review_close
    from gobby.workflows.state_manager import SessionVariableManager

    try:
        seed = evaluation.edit_session_id
        lineage = ChildSessionManager(ctx.session_manager).get_session_lineage(seed) if seed else []
        target = next(
            (
                session
                for session in reversed(lineage)
                if not session.agent_run_id and not session.agent_depth
            ),
            None,
        )
        if target is None:
            logger.debug("No interactive ancestor for memory review of task %s", task_id)
            return
        candidate = classify_memory_review_close(
            ctx.task_manager,
            task_id=task_id,
            changes_summary=changes_summary,
            reason=reason,
            commit_shas=commit_shas,
        )
        if candidate is not None:
            SessionVariableManager(ctx.task_manager.db).queue_memory_review(target.id, candidate)
    except Exception:
        logger.warning("Failed to queue memory review for closed task %s", task_id, exc_info=True)


def _collect_closed_ancestors(
    ctx: RegistryContext,
    ancestor_ids: list[str],
) -> list[dict[str, str]]:
    """Describe each ancestor the transition closed. Notifying is the caller's."""
    summaries: list[dict[str, str]] = []
    for ancestor_id in ancestor_ids:
        ancestor = ctx.task_manager.get_task(ancestor_id)
        if ancestor is None:
            continue
        ref = f"#{ancestor.seq_num}" if ancestor.seq_num else ancestor.id
        summaries.append({"id": ancestor.id, "ref": ref, "title": ancestor.title})
    return summaries


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
        except ValueError:
            raise
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
        from gobby.sessions.title_lifecycle import recompute_automatic_title

        recompute_automatic_title(ctx.session_manager, evaluation.edit_session_id)
    except Exception as exc:
        logger.warning("Failed to clean closed-task claim state: %s", exc)
