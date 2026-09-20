"""Stand-ins for the detached close review used by mock-based close_task suites."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from gobby.mcp_proxy.tools.tasks import _lifecycle_close as lifecycle_close
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import SubmittedCloseReview


async def complete_close_review(
    ctx: RegistryContext,
    *,
    evaluation: CloseEvaluation,
    close_arguments: dict[str, Any],
    evaluate_close: Callable[..., Awaitable[CloseEvaluation]],
    status: str,
    feedback: str,
) -> dict[str, Any]:
    """Apply a matching detached verdict without exercising review persistence."""
    reviewed = await evaluate_close(
        ctx,
        task_id=close_arguments["task_id"],
        reason=close_arguments["reason"],
        changes_summary=close_arguments["changes_summary"],
        commit_sha=close_arguments["commit_sha"],
        project_path=close_arguments["project_path"],
        response_detail=close_arguments["response_detail"],
        override_justification=close_arguments["override_justification"],
        scope_justification=close_arguments["scope_justification"],
        submitted_review=SubmittedCloseReview(
            verdict={
                "status": status,
                "criteria": [
                    {
                        "index": 1,
                        "satisfied": status == "valid",
                        "gap": None if status == "valid" else feedback,
                    }
                ],
                "feedback": feedback,
            },
            review_fingerprint=evaluation.extra["review_fingerprint"],
            evidence_fingerprint=evaluation.extra["deterministic_evidence_fingerprint"],
            diff_sha=evaluation.extra["diff_sha"],
            test_bodies_sha=evaluation.extra["test_bodies_sha"],
            stable_facts=evaluation.extra["stable_facts"],
        ),
    )
    if not reviewed.ready:
        return reviewed.response(preview=bool(close_arguments["preview"]))
    result = await lifecycle_close._commit_close(
        ctx,
        reviewed,
        changes_summary=close_arguments["changes_summary"] or "",
        reason=close_arguments["reason"],
        skip_validation=bool(close_arguments["skip_validation"]),
        override_justification=close_arguments["override_justification"],
        commit_sha=close_arguments["commit_sha"],
    )
    result.update(
        {
            "preview": bool(close_arguments["preview"]),
            "can_close": result.get("closed") is True,
        }
    )
    return result


async def return_detached_response(
    _ctx: RegistryContext,
    *,
    evaluation: CloseEvaluation,
    close_arguments: dict[str, Any],
    evaluate_close: Callable[..., Awaitable[CloseEvaluation]],
) -> dict[str, Any]:
    del evaluate_close
    return evaluation.response(preview=bool(close_arguments["preview"]))


async def complete_valid_close_review(
    ctx: RegistryContext,
    *,
    evaluation: CloseEvaluation,
    close_arguments: dict[str, Any],
    evaluate_close: Callable[..., Awaitable[CloseEvaluation]],
) -> dict[str, Any]:
    return await complete_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=close_arguments,
        evaluate_close=evaluate_close,
        status="valid",
        feedback="All criteria satisfied. Strict mypy and focused tests are clean.",
    )


async def complete_invalid_close_review(
    ctx: RegistryContext,
    *,
    evaluation: CloseEvaluation,
    close_arguments: dict[str, Any],
    evaluate_close: Callable[..., Awaitable[CloseEvaluation]],
) -> dict[str, Any]:
    return await complete_close_review(
        ctx,
        evaluation=evaluation,
        close_arguments=close_arguments,
        evaluate_close=evaluate_close,
        status="invalid",
        feedback="The mypy criterion failed.",
    )
