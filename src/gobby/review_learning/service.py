"""Deterministic review-learning service."""

from __future__ import annotations

import logging
import re
from typing import Any

from gobby.review_learning.fingerprint import (
    build_occurrence_key,
    derive_finding_fingerprint,
)
from gobby.review_learning.lessons import (
    CI_SOURCE_KINDS,
    has_verified_fix,
    normalize_lesson,
    validate_decision,
)
from gobby.review_learning.promotion import promote_lesson
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_resolution import resolve_session_reference
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)

_SPACE_RE = re.compile(r"\s+")


class ReviewLearningService:
    """Record confirmed review lessons and recall relevant memory context."""

    def __init__(self, memory_manager: Any, task_manager: Any):
        self.memory_manager = memory_manager
        self.task_manager = task_manager

    async def recall_context(
        self,
        findings: list[dict[str, Any]],
        proposed_changes: Any | None = None,
        source: str | None = None,
        source_kind: str | None = None,
        session_id: str | None = None,
        repo: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Search memories for review-context relevant to each finding."""
        project_id, _ = self._resolve_scope(session_id)
        grouped: list[dict[str, Any]] = []
        flat_matches: list[dict[str, Any]] = []
        for index, finding in enumerate(findings):
            try:
                queries = build_recall_queries(
                    finding=finding,
                    proposed_changes=proposed_changes,
                    source=source,
                    source_kind=source_kind,
                    repo=repo,
                    language=language,
                )
                matches = await self._search_recall_matches(project_id, index, queries)
            except (AttributeError, RuntimeError, ValueError, OSError) as exc:
                logger.debug("Review-learning recall failed open: %s", exc, exc_info=True)
                matches = []
            grouped.append({"finding_index": index, "matches": matches})
            flat_matches.extend(matches)
        return {"findings": grouped, "matches": flat_matches}

    async def record(
        self,
        *,
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
        """Record a review lesson memory and promote repeated patterns."""
        validated_decision = validate_decision(decision)
        if validated_decision in {"stale", "invalid"}:
            return {
                "decision": validated_decision,
                "skipped_reason": validated_decision,
                "promotable": False,
            }

        project_id, source_session_id = self._resolve_scope(session_id)
        finding_fingerprint = derive_finding_fingerprint(finding)
        occurrence_key = build_occurrence_key(source_review, finding_fingerprint)
        normalized = normalize_lesson(
            source_kind=source_kind,
            source=source,
            source_review=source_review,
            decision=validated_decision,
            finding=finding,
            evidence=evidence,
            finding_fingerprint=finding_fingerprint,
            occurrence_key=occurrence_key,
            repo=repo,
            language=language,
            risk=risk,
        )

        existing = self.memory_manager.list_memories(
            project_id=project_id,
            memory_type="pattern",
            limit=1,
            tags_all=["review-lesson", normalized.occurrence_tag],
        )
        if existing:
            memory = existing[0]
            return {
                "lesson_id": getattr(memory, "id", None),
                "pattern_id": normalized.identity.pattern_id,
                "finding_fingerprint": finding_fingerprint,
                "occurrence_key": occurrence_key,
                "decision": validated_decision,
                "promotable": normalized.identity.promotable,
                "skipped_reason": "duplicate_occurrence",
            }

        if (
            normalized.source_kind in CI_SOURCE_KINDS
            and normalized.decision == "confirmed"
            and not has_verified_fix(evidence)
        ):
            return {
                "pattern_id": normalized.identity.pattern_id,
                "finding_fingerprint": finding_fingerprint,
                "occurrence_key": occurrence_key,
                "decision": validated_decision,
                "promotable": normalized.identity.promotable,
                "skipped_reason": "missing_verified_fix",
            }

        memory = await self.memory_manager.create_memory(
            content=normalized.content,
            memory_type="pattern",
            project_id=project_id,
            source_type="agent",
            source_session_id=source_session_id,
            tags=normalized.tags,
        )
        promotion = promote_lesson(
            lesson=normalized,
            evidence_memory_id=memory.id,
            memory_manager=self.memory_manager,
            task_manager=self.task_manager,
            project_id=project_id,
            source_session_id=source_session_id,
        )
        return {
            "lesson_id": memory.id,
            "pattern_id": normalized.identity.pattern_id,
            "finding_fingerprint": finding_fingerprint,
            "occurrence_key": occurrence_key,
            "decision": validated_decision,
            "promotable": normalized.identity.promotable,
            **promotion,
        }

    async def _search_recall_matches(
        self,
        project_id: str,
        finding_index: int,
        queries: list[str],
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        matches: list[dict[str, Any]] = []
        for query in queries:
            for tags_all in (None, ["review-lesson"]):
                memories = await self.memory_manager.search_memories(
                    query=query,
                    project_id=project_id,
                    limit=5,
                    tags_all=tags_all,
                )
                for memory in memories:
                    memory_id = str(memory.id)
                    if memory_id in seen:
                        continue
                    seen.add(memory_id)
                    tags = memory.tags or []
                    matches.append(
                        {
                            "finding_index": finding_index,
                            "memory_id": memory_id,
                            "content_snippet": _snippet(memory.content),
                            "tags": tags,
                            "reason": (
                                "matched review lesson"
                                if "review-lesson" in tags
                                else "matched project memory"
                            ),
                        }
                    )
        return matches

    def _resolve_scope(self, session_id: str | None) -> tuple[str, str | None]:
        project_id = _current_project_id()
        effective_session_id = session_id or get_current_session_id()
        if not effective_session_id:
            return project_id, None

        try:
            resolved_session_id = resolve_session_reference(
                self.memory_manager.db,
                effective_session_id,
                project_id,
            )
            row = self.memory_manager.db.fetchone(
                "SELECT project_id FROM sessions WHERE id = %s",
                (resolved_session_id,),
            )
            if row and row.get("project_id"):
                project_id = str(row["project_id"])
            return project_id, resolved_session_id
        except (AttributeError, RuntimeError, ValueError, OSError) as exc:
            logger.debug(
                "Could not resolve review-learning session %r: %s",
                effective_session_id,
                exc,
                exc_info=True,
            )
            return project_id, None


def build_recall_queries(
    *,
    finding: dict[str, Any],
    proposed_changes: Any | None,
    source: str | None,
    source_kind: str | None,
    repo: str | None,
    language: str | None,
) -> list[str]:
    """Build focused memory queries for a review finding."""
    terms: list[str] = []
    for key in ("title", "message", "suggestion", "path", "symbol", "rule_id"):
        value = finding.get(key)
        if value:
            terms.append(str(value))
    hints = finding.get("query_hints")
    if isinstance(hints, list):
        terms.extend(str(hint) for hint in hints if hint)
    elif hints:
        terms.append(str(hints))
    for value in (proposed_changes, source, source_kind, repo, language):
        if value:
            terms.append(_flatten(value))
    combined = " ".join(term for term in terms if term).strip()
    if not combined:
        return [str(finding)]
    compact = _SPACE_RE.sub(" ", combined)
    queries = [compact]
    if len(compact) > 240:
        queries.append(compact[:240])
    return queries


def _current_project_id() -> str:
    project_ctx = get_project_context()
    if project_ctx and project_ctx.get("id"):
        return str(project_ctx["id"])
    return PERSONAL_PROJECT_ID


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {val}" for key, val in value.items())
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _snippet(content: str, length: int = 240) -> str:
    compact = _SPACE_RE.sub(" ", content.strip())
    if len(compact) <= length:
        return compact
    return f"{compact[: length - 3]}..."
