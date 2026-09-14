"""MCP registration for close_task and submit_close_review."""

from __future__ import annotations

from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close import _commit_close, _evaluate_close
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    active_review_response,
    launch_close_review,
    supersede_close_retry_wait,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    submit_close_review as finalize_close_review,
)
from gobby.tasks.generation_schemas import TASK_CLOSE_VALIDATION_SCHEMA


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
        supersede_close_retry_wait(ctx, task_id)
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
            changes_summary=changes_summary or "",
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
            "Epics and parents that own no work close when they have no open children; a "
            "claimed task or one with linked commits keeps its leaf gates even with "
            "children. Closing the last child auto-closes eligible ancestors, stopping at "
            "a claimed ancestor, which its owner closes through its own gates. "
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


__all__ = ["register_close_task"]
