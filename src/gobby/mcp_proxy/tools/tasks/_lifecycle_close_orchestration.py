"""Durable launch and verdict orchestration for oversized close reviews."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from uuid import UUID, uuid4

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import SubmittedCloseReview
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.task_close_reviews import (
    QueuedAgentRunSpec,
    TaskCloseReview,
    TaskCloseReviewErrorClass,
    TaskCloseReviewStaleTaskError,
    TaskCloseReviewStore,
    TerminalTaskCloseReviewStatus,
    finalizing_in_process,
)
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.agentic_close_review import (
    TASK_CLOSE_REVIEWER_AGENT,
    build_agentic_review_prompt,
    build_terminal_review_payload,
    reviewer_spawn_overrides,
)
from gobby.utils.machine_id import get_machine_id
from gobby.utils.session_context import get_current_agent_run_id, get_current_session_id

logger = logging.getLogger(__name__)


type CloseEvaluator = Callable[..., Awaitable[CloseEvaluation]]
type CloseCommitter = Callable[..., Awaitable[dict[str, Any]]]


def active_review_response(ctx: RegistryContext, task_id: str) -> dict[str, Any] | None:
    """Return the task's active review before re-evaluating expensive close evidence."""
    try:
        resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
    except (TaskNotFoundError, ValueError):
        return None
    review = TaskCloseReviewStore(ctx.task_manager.db).get_active_for_task(resolved_id)
    return pending_review_response(review) if review is not None else None


def supersede_close_retry_wait(ctx: RegistryContext, task_id: str) -> None:
    """Consume the caller's prior wait before evaluating a new close request."""
    caller = get_current_session_id()
    if caller is None:
        return
    try:
        resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
    except (TaskNotFoundError, ValueError):
        return
    TaskCloseReviewStore(ctx.task_manager.db).supersede_retry_wait(
        resolved_id, caller_session_id=caller
    )


async def launch_close_review(
    ctx: RegistryContext,
    *,
    evaluation: CloseEvaluation,
    close_arguments: Mapping[str, Any],
    evaluate_close: CloseEvaluator,
) -> dict[str, Any]:
    """Persist a waitable FIFO review and promote available project capacity."""
    if (
        evaluation.task is None
        or evaluation.task_id is None
        or evaluation.resolved_session_id is None
    ):
        return evaluation.response(preview=bool(close_arguments.get("preview")))
    review_fingerprint = str(evaluation.extra.get("review_fingerprint") or "")
    evidence_fingerprint = str(
        evaluation.extra.get("deterministic_evidence_fingerprint")
        or evaluation.extra.get("evidence_fingerprint")
        or ""
    )
    diff_sha = str(evaluation.extra.get("diff_sha") or "")
    test_bodies_sha = str(evaluation.extra.get("test_bodies_sha") or "")
    stable_facts = evaluation.extra.get("stable_facts")
    criterion_count = evaluation.extra.get("criterion_count")
    if (
        not review_fingerprint
        or not evidence_fingerprint
        or not diff_sha
        or not test_bodies_sha
        or not isinstance(stable_facts, Mapping)
        or not isinstance(criterion_count, int)
        or criterion_count < 1
    ):
        return evaluation.response(preview=bool(close_arguments.get("preview")))

    task = evaluation.task
    task_ref = f"#{task.seq_num}" if task.seq_num else task.id
    store = TaskCloseReviewStore(ctx.task_manager.db)
    reusable_rejection = store.get_delivered_rejected_verdict(
        task_id=task.id,
        evidence_fingerprint=evidence_fingerprint,
        expected_task_updated_at=task.updated_at,
    )
    if reusable_rejection is not None and reusable_rejection.result_payload is not None:
        return dict(reusable_rejection.result_payload)

    validation_config = ctx.validation_config or TaskValidationConfig()
    validator_timeout_seconds = validation_config.close_review_validator_timeout_seconds
    review_id = str(uuid4())
    run_id = str(uuid4())
    validation_commands = evaluation.extra.get("validation_commands")
    prompt = build_agentic_review_prompt(
        review_id=review_id,
        task_id=task.id,
        commit_shas=evaluation.commit_shas,
        changes_summary=str(close_arguments.get("changes_summary") or ""),
        closure_reason=str(close_arguments.get("reason") or "completed"),
        review_fingerprint=review_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
        criterion_count=criterion_count,
        validation_commands=(
            validation_commands if isinstance(validation_commands, Mapping) else None
        ),
        coordinator_owned_pending=evaluation.extra.get("coordinator_owned_pending") is True,
        close_review_min_severity=validation_config.close_review_min_severity,
    )
    prompt_limit = validation_config.close_review_prompt_max_chars
    if len(prompt) > prompt_limit:
        evaluation.error = "close_review_prompt_too_large"
        evaluation.message = (
            f"Task-close reviewer prompt is {len(prompt)} characters, exceeding the "
            f"configured limit of {prompt_limit}."
        )
        evaluation.extra.update({"prompt_chars": len(prompt), "prompt_limit": prompt_limit})
        return evaluation.response(preview=False)
    overrides = reviewer_spawn_overrides(
        ctx.validation_config,
        unjudged_attempts=TaskCloseReviewStore(ctx.task_manager.db).count_unjudged_attempts(
            task.id
        ),
    )
    machine_id = get_machine_id()
    if machine_id is None or ctx.agent_registry is None:
        evaluation.error = "close_review_unavailable"
        evaluation.message = "Close-review queue infrastructure is unavailable."
        return evaluation.response(preview=False)
    persisted_arguments = dict(close_arguments)
    persisted_arguments.update(
        {
            "_review_timeout_seconds": validator_timeout_seconds,
            "_criterion_count": criterion_count,
            "_manifest_count": evaluation.extra.get("manifest_count"),
            "_excerpt_chars": evaluation.extra.get("excerpt_chars"),
            "_review_provider": overrides.get("provider"),
            "_review_model": overrides.get("model"),
        }
    )
    try:
        review, created = store.create_or_get_active(
            task_id=task.id,
            task_ref=task_ref,
            caller_session_id=evaluation.resolved_session_id,
            commit_shas=evaluation.commit_shas,
            close_arguments=persisted_arguments,
            expected_task_updated_at=task.updated_at,
            review_fingerprint=review_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            diff_sha=diff_sha,
            test_bodies_sha=test_bodies_sha,
            stable_facts=stable_facts,
            review_id=review_id,
            run=QueuedAgentRunSpec(
                id=run_id,
                machine_id=machine_id,
                provider=str(overrides.get("provider") or "codex"),
                model=(str(overrides["model"]) if overrides.get("model") else None),
                agent_name=TASK_CLOSE_REVIEWER_AGENT,
                prompt=prompt,
                timeout_seconds=validator_timeout_seconds,
                requested_reasoning_effort=(
                    str(overrides["reasoning_effort"])
                    if overrides.get("reasoning_effort")
                    else None
                ),
            ),
        )
    except TaskCloseReviewStaleTaskError:
        evaluation.error = "stale_task_state"
        evaluation.message = (
            "The task changed after close_task evaluated it and before review launch. "
            "No reviewer was launched."
        )
        evaluation.action = "Retry close_task; the existing evaluation will not be reused."
        evaluation.extra["stale_state"] = True
        return evaluation.response(preview=bool(close_arguments.get("preview")))
    if not created:
        return pending_review_response(review)

    await promote_close_reviews(
        ctx,
        evaluate_close=evaluate_close,
        project_id=str(task.project_id),
    )
    current = store.get(review.id)
    if current is None:
        return _finish_launch_error(store, review, "Queued task-close review disappeared.")
    if current.terminal:
        terminal = dict(current.result_payload or {})
        terminal.setdefault("success", False)
        terminal.setdefault("can_close", False)
        terminal.setdefault("closed", False)
        terminal.setdefault("review_status", current.status)
        return terminal
    pending = pending_review_response(current)
    pending.update(
        {
            "commit_shas": list(evaluation.commit_shas),
            "close_review_duration_ms": evaluation.extra.get("close_review_duration_ms"),
            "criterion_indexes": list(range(1, criterion_count + 1)),
            "prompt_chars": len(prompt),
            "prompt_limit": prompt_limit,
        }
    )
    if close_arguments.get("response_detail") == "diagnostic" and isinstance(
        validation_commands, Mapping
    ):
        pending["validation_commands"] = validation_commands
    return pending


async def _launch_promoted_review(
    ctx: RegistryContext,
    review: TaskCloseReview,
    evaluation: CloseEvaluation,
) -> dict[str, Any]:
    """Start one fingerprint-revalidated review with its reserved run identity."""
    store = TaskCloseReviewStore(ctx.task_manager.db)
    task = evaluation.task
    if task is None or review.agent_run_id is None:
        return _finish_launch_error(store, review, "Promoted close review lost its identity.")
    persisted_task = await asyncio.to_thread(ctx.task_manager.get_task, task.id)
    if persisted_task is None:
        return _finish_launch_error(store, review, "Promoted close review lost its task.")
    persisted_commits = set(persisted_task.commits or [])
    missing_commits = [sha for sha in evaluation.commit_shas if sha not in persisted_commits]
    if missing_commits:
        return _finish_launch_error(
            store,
            review,
            "Promoted close review commit links are not visible on the task: "
            f"{', '.join(missing_commits)}. No reviewer was launched.",
            error="close_review_commit_links_missing",
        )
    evaluation.task = persisted_task
    task = persisted_task
    validation_config = ctx.validation_config or TaskValidationConfig()
    validator_timeout_seconds = validation_config.close_review_validator_timeout_seconds
    close_arguments = review.close_arguments
    criterion_count = evaluation.extra.get("criterion_count")
    if not isinstance(criterion_count, int) or criterion_count < 1:
        return _finish_launch_error(store, review, "Promoted close review lost its criteria.")

    registry = ctx.agent_registry
    if registry is None:
        return _finish_launch_error(
            store,
            review,
            "Internal agent registry is unavailable.",
            error_class="retryable_infrastructure",
        )
    validation_commands = evaluation.extra.get("validation_commands")
    prompt = build_agentic_review_prompt(
        review_id=review.id,
        task_id=task.id,
        commit_shas=evaluation.commit_shas,
        changes_summary=str(close_arguments.get("changes_summary") or ""),
        closure_reason=str(close_arguments.get("reason") or "completed"),
        review_fingerprint=review.review_fingerprint,
        evidence_fingerprint=review.evidence_fingerprint,
        criterion_count=criterion_count,
        validation_commands=(
            validation_commands if isinstance(validation_commands, Mapping) else None
        ),
        coordinator_owned_pending=evaluation.extra.get("coordinator_owned_pending") is True,
        close_review_min_severity=validation_config.close_review_min_severity,
    )
    prompt_limit = validation_config.close_review_prompt_max_chars
    if len(prompt) > prompt_limit:
        message = (
            f"Task-close reviewer prompt is {len(prompt)} characters, exceeding the configured "
            f"limit of {prompt_limit}. Reduce oversized close evidence before retrying. "
            "No reviewer was launched; the task remains open."
        )
        return {
            **_finish_launch_error(store, review, message, error="agentic_review_prompt_too_large"),
            "prompt_chars": len(prompt),
            "prompt_limit": prompt_limit,
        }
    # Earlier attempts that ended without judging the evidence move this one
    # along the ordered candidate list; relaunching onto a runtime that just
    # died only reproduces the failure, and the caller is told to call
    # close_task again after exactly that error.
    overrides = reviewer_spawn_overrides(
        ctx.validation_config,
        unjudged_attempts=store.count_unjudged_attempts(task.id),
    )
    queued_run = LocalAgentRunManager(ctx.task_manager.db).update_queued_prompt(
        review.agent_run_id,
        prompt,
    )
    if queued_run is None:
        return _finish_launch_error(store, review, "Queued reviewer run could not be refreshed.")
    try:
        launch = await registry.call(
            "spawn_agent",
            {
                "prompt": prompt,
                "agent": TASK_CLOSE_REVIEWER_AGENT,
                "task_id": None,
                "isolation": "none",
                "parent_session_id": review.caller_session_id,
                "project_path": evaluation.repo_path,
                "notify_parent_on_completion": True,
                "timeout": validator_timeout_seconds,
                "reserved_run_id": review.agent_run_id,
                **overrides,
            },
        )
    except Exception as exc:
        logger.warning("Task-close reviewer launch failed", exc_info=True)
        return _finish_launch_error(
            store,
            review,
            str(exc),
            error_class=(
                "retryable_infrastructure" if isinstance(exc, OSError) else "action_required"
            ),
        )
    if not isinstance(launch, Mapping) or launch.get("success") is not True:
        message = (
            str(launch.get("error") or "Task-close reviewer launch failed.")
            if isinstance(launch, Mapping)
            else "Task-close reviewer launch returned an invalid response."
        )
        failed_run_id = launch.get("run_id") if isinstance(launch, Mapping) else None
        try:
            failed_run_id = str(UUID(failed_run_id)) if isinstance(failed_run_id, str) else None
        except ValueError:
            failed_run_id = None
        failed_run = (
            LocalAgentRunManager(ctx.task_manager.db).get(failed_run_id)
            if isinstance(failed_run_id, str) and failed_run_id
            else None
        )
        return _finish_launch_error(
            store,
            review,
            message,
            error_class=(
                "retryable_infrastructure"
                if failed_run is not None
                and failed_run.terminal_reason == "spawn_rollback"
                and (failed_run.resume_metadata_json or {}).get("spawn_retryable_infrastructure")
                is True
                else "action_required"
            ),
        )
    run_id = str(launch.get("run_id") or "")
    if not run_id:
        return _finish_launch_error(store, review, "Task-close reviewer launch omitted run_id.")
    try:
        run_id = str(UUID(run_id))
    except ValueError:
        return _finish_launch_error(
            store, review, "Task-close reviewer launch returned invalid run_id."
        )
    running = store.bind_run(review.id, run_id)
    if running is None:
        return _finish_launch_error(
            store, review, "Task-close review could not bind its agent run."
        )
    return {
        "success": True,
        "preview": bool(close_arguments.get("preview")),
        "can_close": False,
        "closed": False,
        "task_id": task.id,
        "commit_shas": list(evaluation.commit_shas),
        "error": "close_review_required",
        "message": (
            "Close evidence is being reviewed by a daemon-managed reviewer. "
            "The task stays open and claimed; the verdict is applied and delivered to "
            "this session automatically. Do not poll agent runs or re-call close_task."
        ),
        "blocking_reasons": [],
        "required_actions": [],
        "review_id": running.id,
        "reviewer_run_id": run_id,
        "review_fingerprint": running.review_fingerprint,
        "deterministic_evidence_fingerprint": running.evidence_fingerprint,
        "review_status": running.status,
        "reviewer_provider": overrides.get("provider"),
        "reviewer_model": overrides.get("model"),
        "prompt_chars": len(prompt),
        "prompt_limit": prompt_limit,
        "criterion_count": criterion_count,
        "criterion_indexes": list(range(1, criterion_count + 1)),
        "manifest_count": evaluation.extra.get("manifest_count"),
        "excerpt_chars": evaluation.extra.get("excerpt_chars"),
        "close_review_duration_ms": evaluation.extra.get("close_review_duration_ms"),
        **(
            {"validation_commands": validation_commands}
            if close_arguments.get("response_detail") == "diagnostic"
            else {}
        ),
    }


async def promote_close_reviews(
    ctx: RegistryContext,
    *,
    evaluate_close: CloseEvaluator,
    project_id: str | None = None,
) -> list[str]:
    """Fill each project's reviewer slots from its durable FIFO queue."""
    store = TaskCloseReviewStore(ctx.task_manager.db)
    project_ids = [project_id] if project_id is not None else store.list_queued_project_ids()
    promoted_ids: list[str] = []
    max_concurrency = (
        ctx.validation_config.close_review_max_concurrency_per_project
        if ctx.validation_config is not None
        else 3
    )
    for queued_project_id in project_ids:
        while True:
            claimed = store.claim_queued(
                project_id=queued_project_id,
                max_concurrency=max_concurrency,
            )
            if not claimed:
                break
            for review in claimed:
                promoted_ids.append(review.id)
                args = review.close_arguments
                try:
                    evaluation = await evaluate_close(
                        ctx,
                        task_id=_required_string(args, "task_id"),
                        reason=_required_string(args, "reason"),
                        changes_summary=_optional_string(args, "changes_summary"),
                        commit_sha=_optional_string(args, "commit_sha"),
                        project_path=_optional_string(args, "project_path"),
                        response_detail=_response_detail(args),
                        override_justification=_optional_string(args, "override_justification"),
                        scope_justification=_optional_string(args, "scope_justification"),
                        closing_session_id=review.caller_session_id,
                        run_close_review=True,
                    )
                except Exception as exc:
                    logger.warning("Queued close-review revalidation failed", exc_info=True)
                    _finish_launch_error(store, review, str(exc))
                    if review.agent_run_id:
                        LocalAgentRunManager(ctx.task_manager.db).fail(
                            review.agent_run_id,
                            str(exc),
                        )
                    continue
                current_review_fingerprint = str(evaluation.extra.get("review_fingerprint") or "")
                current_evidence_fingerprint = str(
                    evaluation.extra.get("deterministic_evidence_fingerprint")
                    or evaluation.extra.get("evidence_fingerprint")
                    or ""
                )
                review_ready = evaluation.error == "close_review_required"
                fingerprints_match = (
                    current_review_fingerprint == review.review_fingerprint
                    and current_evidence_fingerprint == review.evidence_fingerprint
                )
                if not review_ready or not fingerprints_match:
                    message = (
                        "Task-close evidence or policy changed while the review was queued. "
                        "Run close_task again after addressing the current deterministic state."
                    )
                    payload = build_terminal_review_payload(
                        review,
                        status="stale",
                        message=message,
                        close_result={"error": "close_review_stale"},
                    )
                    store.finish(
                        review.id,
                        status="stale",
                        result_payload=payload,
                        error=message,
                    )
                    if review.agent_run_id:
                        LocalAgentRunManager(ctx.task_manager.db).fail(
                            review.agent_run_id,
                            message,
                        )
                    continue
                await _launch_promoted_review(ctx, review, evaluation)
    return promoted_ids


async def submit_close_review(
    ctx: RegistryContext,
    *,
    review_id: str,
    verdict: Mapping[str, object],
    evaluate_close: CloseEvaluator,
    commit_close: CloseCommitter,
) -> dict[str, Any]:
    """Authenticate a reviewer, rerun close gates, and persist one terminal result."""
    store = TaskCloseReviewStore(ctx.task_manager.db)
    review = store.get(review_id)
    authenticated = _authenticate_submission(ctx, review)
    if authenticated is not None:
        return authenticated
    assert review is not None and review.agent_run_id is not None
    run_id = review.agent_run_id
    claimed = store.claim_finalizing(review.id, run_id, verdict=verdict)
    if claimed is None:
        current = store.get(review.id)
        if current is not None and current.terminal and current.result_payload is not None:
            return {
                "success": True,
                "review_id": current.id,
                "review_status": current.status,
                "closed": current.status == "closed",
                "terminal_payload": current.result_payload,
            }
        return {
            "success": False,
            "error": "agentic_review_pending",
            "message": "This review is already being finalized.",
            "review_id": review.id,
            "review_status": current.status if current is not None else review.status,
        }

    # The set entry is this daemon's proof of liveness for the row: the
    # reconciler leaves a `finalizing` review alone while a submission here
    # holds it, and sweeps it once this block exits by any path.
    with finalizing_in_process(claimed.id):
        args = claimed.close_arguments
        try:
            evaluation = await evaluate_close(
                ctx,
                task_id=_required_string(args, "task_id"),
                reason=_required_string(args, "reason"),
                changes_summary=_optional_string(args, "changes_summary"),
                commit_sha=_optional_string(args, "commit_sha"),
                project_path=_optional_string(args, "project_path"),
                response_detail=_response_detail(args),
                override_justification=_optional_string(args, "override_justification"),
                scope_justification=_optional_string(args, "scope_justification"),
                closing_session_id=claimed.caller_session_id,
                submitted_review=SubmittedCloseReview(
                    verdict=verdict,
                    review_fingerprint=claimed.review_fingerprint,
                    evidence_fingerprint=claimed.evidence_fingerprint,
                    diff_sha=claimed.diff_sha,
                    test_bodies_sha=claimed.test_bodies_sha,
                    stable_facts=claimed.stable_facts,
                ),
            )
        except Exception as exc:
            logger.warning("Task-close review finalization failed", exc_info=True)
            result = _finish_submission_error(store, claimed, str(exc))
            await promote_close_reviews(ctx, evaluate_close=evaluate_close)
            return result

        if evaluation.error == "agentic_review_malformed":
            message = evaluation.message or "Background close-review verdict is invalid."
            store.restore_running(claimed.id, run_id, error=message)
            return {
                "success": False,
                "error": "agentic_review_malformed",
                "message": message,
                "review_id": claimed.id,
                "review_status": "running",
                "closed": False,
            }
        if not evaluation.ready:
            status: TerminalTaskCloseReviewStatus = (
                "invalid"
                if evaluation.validation_status == "invalid"
                else _failure_status(evaluation)
            )
            result = evaluation.response(preview=bool(args.get("preview")))
            payload = build_terminal_review_payload(
                claimed,
                status=status,
                close_result=result,
                message=evaluation.message,
            )
            store.finish(
                claimed.id,
                status=status,
                result_payload=payload,
                error=evaluation.message if status == "error" else None,
            )
            result = _submission_result(claimed, payload)
            await promote_close_reviews(ctx, evaluate_close=evaluate_close)
            return result

        close_result = await commit_close(
            ctx,
            evaluation,
            reason=_required_string(args, "reason"),
            changes_summary=_optional_string(args, "changes_summary") or "",
            skip_validation=bool(args.get("skip_validation", False)),
            override_justification=_optional_string(args, "override_justification"),
            commit_sha=_optional_string(args, "commit_sha"),
        )
        close_result.update(
            {
                "preview": bool(args.get("preview")),
                "can_close": close_result.get("closed") is True,
            }
        )
        status = (
            "closed" if close_result.get("closed") is True else _commit_failure_status(close_result)
        )
        payload = build_terminal_review_payload(
            claimed,
            status=status,
            close_result=close_result,
            message=cast(str | None, close_result.get("message")),
        )
        store.finish(
            claimed.id,
            status=status,
            result_payload=payload,
            error=cast(str | None, close_result.get("message")) if status == "error" else None,
        )
        result = _submission_result(claimed, payload)
        await promote_close_reviews(ctx, evaluate_close=evaluate_close)
        return result


def pending_review_response(review: TaskCloseReview) -> dict[str, Any]:
    return {
        "success": True,
        "preview": bool(review.close_arguments.get("preview")),
        "can_close": False,
        "closed": False,
        "task_id": review.task_id,
        "error": "close_review_required",
        "message": (
            "A daemon-managed task-close review is queued or active for this task. "
            "Its verdict is applied and delivered to the claiming session automatically. "
            "Do not poll agent runs or re-call close_task."
        ),
        "blocking_reasons": [],
        "required_actions": [],
        "review_id": review.id,
        "reviewer_run_id": review.agent_run_id,
        "review_fingerprint": review.review_fingerprint,
        "deterministic_evidence_fingerprint": review.evidence_fingerprint,
        "review_status": review.status,
        "reviewer_provider": review.close_arguments.get("_review_provider"),
        "reviewer_model": review.close_arguments.get("_review_model"),
        "criterion_count": review.close_arguments.get("_criterion_count"),
        "manifest_count": review.close_arguments.get("_manifest_count"),
        "excerpt_chars": review.close_arguments.get("_excerpt_chars"),
    }


def _authenticate_submission(
    ctx: RegistryContext,
    review: TaskCloseReview | None,
) -> dict[str, Any] | None:
    if review is None:
        return {
            "success": False,
            "error": "agentic_review_not_found",
            "message": "Task-close review was not found.",
        }
    run_id = get_current_agent_run_id()
    session_id = get_current_session_id()
    if not run_id or not session_id:
        return _auth_error(review, "Reviewer run and session context are required.")
    if review.agent_run_id != run_id:
        return _auth_error(review, "Reviewer run does not own this task-close review.")
    run = LocalAgentRunManager(ctx.task_manager.db).get(run_id)
    if run is None:
        return _auth_error(review, "Reviewer agent run was not found.")
    if (
        run.agent_name != TASK_CLOSE_REVIEWER_AGENT
        or run.task_id is not None
        or run.parent_session_id != review.caller_session_id
        or run.child_session_id != session_id
    ):
        return _auth_error(review, "Reviewer agent identity does not match the persisted review.")
    return None


def _auth_error(review: TaskCloseReview, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": "agentic_review_unauthorized",
        "message": message,
        "review_id": review.id,
        "review_status": review.status,
        "closed": False,
    }


def _finish_launch_error(
    store: TaskCloseReviewStore,
    review: TaskCloseReview,
    message: str,
    *,
    error_class: TaskCloseReviewErrorClass = "action_required",
    error: str = "agentic_review_launch_failed",
) -> dict[str, Any]:
    payload = build_terminal_review_payload(
        review,
        status="error",
        message=message,
        error_class=error_class,
        close_result={"error": error},
    )
    store.finish(review.id, status="error", result_payload=payload, error=message)
    if review.agent_run_id:
        LocalAgentRunManager(store.db).fail(review.agent_run_id, message)
    return {
        **payload,
        "success": False,
        "can_close": False,
        "error": error,
        "review_status": "error",
    }


def _finish_submission_error(
    store: TaskCloseReviewStore,
    review: TaskCloseReview,
    message: str,
) -> dict[str, Any]:
    payload = build_terminal_review_payload(review, status="error", message=message)
    store.finish(review.id, status="error", result_payload=payload, error=message)
    return _submission_result(review, payload, success=False)


def _submission_result(
    review: TaskCloseReview,
    payload: Mapping[str, Any],
    *,
    success: bool = True,
) -> dict[str, Any]:
    return {
        "success": success,
        "review_id": review.id,
        "review_status": payload["status"],
        "closed": payload["closed"],
        "message": payload["message"],
        "terminal_payload": dict(payload),
    }


def _failure_status(evaluation: CloseEvaluation) -> TerminalTaskCloseReviewStatus:
    if evaluation.error == "external_pending":
        return "external_pending"
    if evaluation.error in {
        "validation_provider_unavailable",
        "validation_infrastructure_unavailable",
        "validation_evidence_unavailable",
        "validation_diff_unavailable",
        "task_scope_unavailable",
    }:
        return "error"
    return "stale"


def _commit_failure_status(result: Mapping[str, Any]) -> TerminalTaskCloseReviewStatus:
    return "stale" if result.get("error") == "stale_task_state" else "error"


def _required_string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Persisted close argument {key!r} is invalid")
    return value


def _optional_string(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Persisted close argument {key!r} is invalid")
    return value


def _response_detail(arguments: Mapping[str, Any]) -> str:
    value = arguments.get("response_detail", "concise")
    if value not in {"concise", "diagnostic"}:
        raise ValueError("Persisted close argument 'response_detail' is invalid")
    return cast(str, value)


__all__ = [
    "active_review_response",
    "launch_close_review",
    "pending_review_response",
    "submit_close_review",
]
