"""Internal MCP tools for review-signal learning."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.review_learning.promotion import PromotionTaskManager
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService

_FINDING_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Structured review finding.",
    "additionalProperties": True,
    "properties": {
        "title": {"type": "string", "description": "Short finding title."},
        "message": {"type": "string", "description": "Finding message or diagnostic text."},
        "suggestion": {"type": "string", "description": "Reviewer-suggested change."},
        "path": {"type": "string", "description": "Repository path the finding applies to."},
        "symbol": {"type": "string", "description": "Code symbol the finding applies to."},
        "rule_id": {"type": "string", "description": "Reviewer or analyzer rule identifier."},
        "query_hints": {
            "description": "Additional recall search terms.",
            "oneOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string"},
            ],
        },
    },
}

_RECALL_REVIEW_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "description": "Review findings as structured objects or plain finding text.",
            "items": {
                "oneOf": [
                    _FINDING_OBJECT_SCHEMA,
                    {"type": "string", "description": "Plain finding message."},
                ]
            },
        },
        "proposed_changes": {
            "type": "object",
            "description": "Optional proposed change context for recall search.",
        },
        "source": {"type": "string", "description": "Review source identifier."},
        "source_kind": {"type": "string", "description": "Kind of review signal."},
        "session_id": {"type": "string", "description": "Session scope for project resolution."},
        "repo": {"type": "string", "description": "Repository identifier."},
        "language": {"type": "string", "description": "Programming language context."},
    },
    "required": ["findings"],
}


def create_review_learning_registry(
    memory_manager: ReviewLearningMemoryManager,
    task_manager: PromotionTaskManager,
) -> InternalToolRegistry:
    """Create the review-learning MCP registry."""
    service = ReviewLearningService(memory_manager=memory_manager, task_manager=task_manager)
    registry = InternalToolRegistry(
        name="gobby-review-learning",
        description="Review signal learning - recall lessons and record confirmed findings",
    )

    async def recall_review_context(
        findings: list[dict[str, Any] | str],
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
        except (ValueError, RuntimeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    registry.register(
        name="recall_review_context",
        description="Recall project memories and review lessons relevant to review findings.",
        input_schema=_RECALL_REVIEW_CONTEXT_SCHEMA,
        func=recall_review_context,
    )

    @registry.tool(
        name="recall_review_lessons_for_files",
        description="Recall compact confirmed review lessons relevant to touched files.",
    )
    async def recall_review_lessons_for_files(
        file_paths: list[str] | str | None = None,
        file_paths_json: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        limit: int = 3,
    ) -> dict[str, Any]:
        """Recall compact guidance for file-scoped guardrail injection."""
        try:
            result = await service.recall_review_lessons_for_files(
                file_paths=file_paths,
                file_paths_json=file_paths_json,
                project_id=project_id,
                session_id=session_id,
                limit=limit,
            )
            return {"success": True, **result}
        except (AttributeError, ValueError, RuntimeError, OSError) as exc:
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
        except (AttributeError, ValueError, RuntimeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    return registry
