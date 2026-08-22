"""Internal MCP tools for review-signal learning."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.review_learning.service import (
    MAX_RECALL_FINDINGS,
    ReviewLearningService,
)
from gobby.utils.session_context import get_current_session_id

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
            "description": (
                f"Up to {MAX_RECALL_FINDINGS} review findings as structured objects or plain text."
            ),
            "maxItems": MAX_RECALL_FINDINGS,
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
        "session_id": {
            "type": "string",
            "description": "Interactive session; defaults to the ambient caller session.",
        },
        "repo": {"type": "string", "description": "Repository identifier."},
        "language": {"type": "string", "description": "Programming language context."},
    },
    "required": ["findings"],
}


def create_review_learning_registry(service: ReviewLearningService) -> InternalToolRegistry:
    """Create the review-learning MCP registry."""
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
        """Recall targeted context for an explicit or ambient interactive session."""
        try:
            result = await service.recall_context(
                findings=findings,
                proposed_changes=proposed_changes,
                source=source,
                source_kind=source_kind,
                session_id=session_id or get_current_session_id(),
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
        name="recall_review_lessons_by_class",
        description="Recall confirmed review lessons for a domain-qualified lesson class.",
    )
    async def recall_review_lessons_by_class(
        lesson_domain: str,
        lesson_types: list[str],
        source_kinds: list[str] | None = None,
        limit: int = 3,
    ) -> dict[str, Any]:
        """Recall compact guidance for class-scoped guardrail injection."""
        try:
            result = await service.recall_review_lessons_by_class(
                lesson_domain=lesson_domain,
                lesson_types=lesson_types,
                source_kinds=source_kinds,
                limit=limit,
            )
            return {"success": True, **result}
        except (AttributeError, ValueError, RuntimeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    @registry.tool(
        name="list_check_keys",
        description="List all check-key identities recorded for a review-lesson class.",
    )
    async def list_check_keys(
        lesson_domain: str,
        lesson_type: str,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Enumerate the complete class-scoped check-key set."""
        try:
            result = await service.list_check_keys(
                lesson_domain=lesson_domain,
                lesson_type=lesson_type,
                category=category,
            )
            return {"success": True, **result}
        except (AttributeError, ValueError, RuntimeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    @registry.tool(
        name="retire_review_lesson",
        description="Retire an obsolete review lesson and report its open guardrail tasks.",
    )
    async def retire_review_lesson(
        pattern_id: str,
        evidence: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Retag confirmed lesson occurrences as stale after verifying obsolescence."""
        try:
            result = await service.retire_review_lesson(
                pattern_id=pattern_id,
                evidence=evidence,
                session_id=session_id,
            )
            return {"success": True, **result}
        except (AttributeError, ValueError, RuntimeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    @registry.tool(
        name="record_review_lesson",
        description=(
            "Record a confirmed or no-fix-policy review lesson for later recall. "
            "Finding requires non-empty title or message and non-empty "
            "principle or prevention."
        ),
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
