"""Workflow-owned backfill tool for durable plan-review lesson checkpoints."""

from __future__ import annotations

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.review_learning.recorders import mint_plan_review_lessons
from gobby.utils.session_context import get_current_session_id


def register_plan_review_backfill_tool(
    registry: InternalToolRegistry,
    ctx: RegistryContext,
) -> None:
    """Register the idempotent task/stage backfill operation."""

    async def backfill_plan_review_lessons(
        task_id: str,
        stage: str,
    ) -> dict[str, object]:
        service = ctx.review_learning_service
        if service is None:
            return ReviewEvidenceError(
                "review_learning_unavailable",
                "Review-learning service is unavailable",
            ).to_dict()
        resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
        session_id = _resolved_session_id(ctx)
        try:
            return await mint_plan_review_lessons(
                resolved_id,
                stage,
                db=ctx.task_manager.db,
                review_learning_service=service,
                session_id=session_id,
            )
        except ReviewEvidenceError as error:
            return error.to_dict()

    registry.register(
        name="backfill_plan_review_lessons",
        description=(
            "Retry the durable lesson-mint checkpoint for an already-approved "
            "planning-stage review."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "stage": {"type": "string"},
            },
            "required": ["task_id", "stage"],
        },
        output_schema={"type": "object"},
        func=backfill_plan_review_lessons,
    )


def _resolved_session_id(ctx: RegistryContext) -> str | None:
    session_ref = get_current_session_id()
    if session_ref is None:
        return None
    try:
        return ctx.resolve_session_id(session_ref)
    except Exception:
        return None
