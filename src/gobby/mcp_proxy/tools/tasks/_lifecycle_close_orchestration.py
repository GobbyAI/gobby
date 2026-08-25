"""Durable launch and verdict orchestration for oversized close reviews."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import SubmittedCloseReview
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.task_close_reviews import (
    TaskCloseReview,
    TaskCloseReviewStore,
    TerminalTaskCloseReviewStatus,
)
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.agentic_close_review import (
    TASK_CLOSE_VALIDATOR_AGENT,
    build_agentic_review_prompt,
    build_terminal_review_payload,
    validator_spawn_overrides,
)
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


async def launch_close_review(
    ctx: RegistryContext,
    *,
    evaluation: CloseEvaluation,
    close_arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one launch intent, start its taskless validator, and return immediately."""
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
    if not review_fingerprint or not evidence_fingerprint:
        return evaluation.response(preview=bool(close_arguments.get("preview")))

    task = evaluation.task
    task_ref = f"#{task.seq_num}" if task.seq_num else task.id
    store = TaskCloseReviewStore(ctx.task_manager.db)
    review, created = store.create_or_get_active(
        task_id=task.id,
        task_ref=task_ref,
        caller_session_id=evaluation.resolved_session_id,
        close_arguments=close_arguments,
        review_fingerprint=review_fingerprint,
        evidence_fingerprint=evidence_fingerprint,
    )
    if not created:
        return pending_review_response(review)

    registry = ctx.agent_registry
    if registry is None:
        return _finish_launch_error(store, review, "Internal agent registry is unavailable.")
    prompt = build_agentic_review_prompt(
        review_id=review.id,
        task_id=task.id,
        commit_shas=evaluation.commit_shas,
        changes_summary=str(close_arguments.get("changes_summary") or ""),
        review_fingerprint=review.review_fingerprint,
        evidence_fingerprint=review.evidence_fingerprint,
    )
    try:
        launch = await registry.call(
            "spawn_agent",
            {
                "prompt": prompt,
                "agent": TASK_CLOSE_VALIDATOR_AGENT,
                "task_id": None,
                "isolation": "none",
                "parent_session_id": review.caller_session_id,
                "project_path": evaluation.repo_path,
                "notify_parent_on_completion": True,
                **validator_spawn_overrides(ctx.validation_config),
            },
        )
    except Exception as exc:
        logger.warning("Task-close validator launch failed", exc_info=True)
        return _finish_launch_error(store, review, str(exc))
    if not isinstance(launch, Mapping) or launch.get("success") is not True:
        message = (
            str(launch.get("error") or "Task-close validator launch failed.")
            if isinstance(launch, Mapping)
            else "Task-close validator launch returned an invalid response."
        )
        return _finish_launch_error(store, review, message)
    run_id = str(launch.get("run_id") or "")
    if not run_id:
        return _finish_launch_error(store, review, "Task-close validator launch omitted run_id.")
    running = store.bind_run(review.id, run_id)
    if running is None:
        return _finish_launch_error(
            store, review, "Task-close review could not bind its agent run."
        )
    return {
        "success": False,
        "preview": bool(close_arguments.get("preview")),
        "can_close": False,
        "closed": False,
        "task_id": task.id,
        "commit_shas": list(evaluation.commit_shas),
        "error": "agentic_review_required",
        "message": (
            "Oversized close evidence is being reviewed by a daemon-managed validator. "
            "The task stays open and claimed; the verdict is applied and delivered to "
            "this session automatically. Do not poll agent runs or re-call close_task."
        ),
        "blocking_reasons": [],
        "required_actions": [],
        "review_id": running.id,
        "review_fingerprint": running.review_fingerprint,
        "deterministic_evidence_fingerprint": running.evidence_fingerprint,
        "review_status": running.status,
    }


async def submit_close_review(
    ctx: RegistryContext,
    *,
    review_id: str,
    verdict: Mapping[str, object],
    evaluate_close: CloseEvaluator,
    commit_close: CloseCommitter,
) -> dict[str, Any]:
    """Authenticate a validator, rerun close gates, and persist one terminal result."""
    store = TaskCloseReviewStore(ctx.task_manager.db)
    review = store.get(review_id)
    authenticated = _authenticate_submission(ctx, review)
    if authenticated is not None:
        return authenticated
    assert review is not None and review.agent_run_id is not None
    run_id = review.agent_run_id
    claimed = store.claim_finalizing(review.id, run_id)
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
            ),
        )
    except Exception as exc:
        logger.warning("Task-close review finalization failed", exc_info=True)
        return _finish_submission_error(store, claimed, str(exc))

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
            "invalid" if evaluation.validation_status == "invalid" else _failure_status(evaluation)
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
        return _submission_result(claimed, payload)

    close_result = await commit_close(
        ctx,
        evaluation,
        reason=_required_string(args, "reason"),
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
    return _submission_result(claimed, payload)


def pending_review_response(review: TaskCloseReview) -> dict[str, Any]:
    return {
        "success": False,
        "preview": bool(review.close_arguments.get("preview")),
        "can_close": False,
        "closed": False,
        "task_id": review.task_id,
        "error": "agentic_review_pending",
        "message": (
            "A daemon-managed task-close review is already active for this task. "
            "Its verdict is applied and delivered to the claiming session automatically. "
            "Do not poll agent runs or re-call close_task."
        ),
        "blocking_reasons": [],
        "required_actions": [],
        "review_id": review.id,
        "review_fingerprint": review.review_fingerprint,
        "deterministic_evidence_fingerprint": review.evidence_fingerprint,
        "review_status": review.status,
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
        return _auth_error(review, "Validator run and session context are required.")
    if review.agent_run_id != run_id:
        return _auth_error(review, "Validator run does not own this task-close review.")
    run = LocalAgentRunManager(ctx.task_manager.db).get(run_id)
    if run is None:
        return _auth_error(review, "Validator agent run was not found.")
    if (
        run.agent_name != TASK_CLOSE_VALIDATOR_AGENT
        or run.task_id is not None
        or run.parent_session_id != review.caller_session_id
        or run.child_session_id != session_id
    ):
        return _auth_error(review, "Validator agent identity does not match the persisted review.")
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
) -> dict[str, Any]:
    payload = build_terminal_review_payload(review, status="error", message=message)
    store.finish(review.id, status="error", result_payload=payload, error=message)
    return {
        **payload,
        "success": False,
        "can_close": False,
        "error": "agentic_review_launch_failed",
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
