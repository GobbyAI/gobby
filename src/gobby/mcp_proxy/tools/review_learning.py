"""Internal MCP tools for review-signal learning."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.review_learning.service import ReviewLearningService


def create_review_learning_registry(
    memory_manager: Any,
    task_manager: Any,
) -> InternalToolRegistry:
    """Create the review-learning MCP registry."""
    service = ReviewLearningService(memory_manager=memory_manager, task_manager=task_manager)
    registry = InternalToolRegistry(
        name="gobby-review-learning",
        description="Review signal learning - recall lessons and record confirmed findings",
    )

    @registry.tool(
        name="recall_review_context",
        description="Recall project memories and review lessons relevant to review findings.",
    )
    async def recall_review_context(
        findings: list[dict[str, Any]],
        proposed_changes: Any | None = None,
        source: str | None = None,
        source_kind: str | None = None,
        session_id: str | None = None,
        repo: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Recall targeted context before review triage decisions."""
        try:
            result = await service.recall_context(
                findings=findings,
                proposed_changes=proposed_changes,
                source=source,
                source_kind=source_kind,
                session_id=session_id,
                repo=repo,
                language=language,
            )
            return {"success": True, **result}
        except (TypeError, ValueError, RuntimeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    @registry.tool(
        name="record_review_lesson",
        description="Record a confirmed review lesson and promote repeated patterns.",
    )
    async def record_review_lesson(
        source_kind: str,
        source: str,
        source_review: str,
        decision: str,
        finding: dict[str, Any],
        evidence: dict[str, Any],
        session_id: str | None = None,
        repo: str | None = None,
        language: str | None = None,
        risk: str = "medium",
    ) -> dict[str, Any]:
        """Record durable learning after a confirmed fix or no-fix-policy decision."""
        try:
            result = await service.record(
                source_kind=source_kind,
                source=source,
                source_review=source_review,
                decision=decision,
                finding=finding,
                evidence=evidence,
                session_id=session_id,
                repo=repo,
                language=language,
                risk=risk,
            )
            return {"success": True, **result}
        except (TypeError, ValueError, RuntimeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    return registry
