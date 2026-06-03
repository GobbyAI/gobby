"""Deterministic review-learning service."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol

from gobby.review_learning.file_paths import (
    extract_file_paths_from_mapping,
    normalize_lesson_file_path,
    path_tag,
    paths_match,
)
from gobby.review_learning.fingerprint import (
    build_occurrence_key,
    derive_finding_fingerprint,
)
from gobby.review_learning.guidance import format_review_lesson_guidance
from gobby.review_learning.lessons import (
    CI_SOURCE_KINDS,
    has_verified_fix,
    normalize_lesson,
    validate_decision,
)
from gobby.review_learning.promotion import (
    PromotionMemoryManager,
    PromotionTaskManager,
    promote_lesson,
)
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_resolution import resolve_session_reference
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)

_SPACE_RE = re.compile(r"\s+")
_LESSON_FIELD_RE = re.compile(r"^-\s+(?P<key>[a-zA-Z_]+):\s*(?P<value>.*)$")
_LEGACY_SCAN_LIMIT = 200


class ReviewLearningMemoryManager(PromotionMemoryManager, Protocol):
    db: Any

    async def create_memory(
        self,
        *,
        content: str,
        memory_type: str,
        project_id: str,
        source_type: str,
        source_session_id: str | None,
        tags: list[str],
    ) -> Any: ...

    async def search_memories(
        self,
        *,
        query: str,
        project_id: str,
        limit: int,
        tags_all: list[str] | None,
    ) -> list[Any]: ...


class ReviewLearningService:
    """Record confirmed review lessons and recall relevant memory context."""

    def __init__(
        self,
        memory_manager: ReviewLearningMemoryManager,
        task_manager: PromotionTaskManager,
    ):
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

    async def recall_review_lessons_for_files(
        self,
        *,
        file_paths: Any | None = None,
        file_paths_json: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        limit: int = 3,
    ) -> dict[str, Any]:
        """Recall confirmed review lessons with deterministic file-path matching."""
        resolved_project_id = project_id or self._resolve_scope(session_id)[0]
        normalized_paths = _coerce_file_paths(
            file_paths=file_paths, file_paths_json=file_paths_json
        )
        bounded_limit = max(1, min(int(limit or 3), 10))
        if not normalized_paths:
            return {"count": 0, "lessons": [], "message": ""}

        memories = await self._candidate_lesson_memories(
            project_id=resolved_project_id,
            touched_paths=normalized_paths,
            limit=bounded_limit,
        )
        lessons: list[dict[str, Any]] = []
        seen: set[str] = set()
        for memory, tagged_path in memories:
            memory_id = str(getattr(memory, "id", "") or "")
            if not memory_id or memory_id in seen:
                continue
            lesson = _build_file_lesson(memory, normalized_paths, tagged_path)
            if lesson is None:
                continue
            seen.add(memory_id)
            lessons.append(lesson)
            if len(lessons) >= bounded_limit:
                break

        return {
            "count": len(lessons),
            "lessons": lessons,
            "message": format_review_lesson_guidance(lessons),
        }

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

        existing = await self.memory_manager.alist_memories(
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
        promotion = await promote_lesson(
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

    async def _candidate_lesson_memories(
        self,
        *,
        project_id: str,
        touched_paths: list[str],
        limit: int,
    ) -> list[tuple[Any, str | None]]:
        tagged_paths = {path_tag(path): path for path in touched_paths}
        candidates: list[tuple[Any, str | None]] = []
        seen: set[str] = set()

        for tag, touched_path in tagged_paths.items():
            tagged_memories = await asyncio.to_thread(
                self.memory_manager.list_memories,
                project_id=project_id,
                memory_type="pattern",
                limit=limit,
                tags_all=["review-lesson", "confirmed", tag],
            )
            for memory in tagged_memories:
                memory_id = str(getattr(memory, "id", "") or "")
                if not memory_id or memory_id in seen:
                    continue
                seen.add(memory_id)
                candidates.append((memory, touched_path))

        legacy_memories = await asyncio.to_thread(
            self.memory_manager.list_memories,
            project_id=project_id,
            memory_type="pattern",
            limit=max(_LEGACY_SCAN_LIMIT, limit),
            tags_all=["review-lesson", "confirmed"],
        )
        for memory in legacy_memories:
            memory_id = str(getattr(memory, "id", "") or "")
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            candidates.append((memory, None))

        return candidates


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


def _coerce_file_paths(*, file_paths: Any | None, file_paths_json: str | None) -> list[str]:
    values: list[Any] = []
    if file_paths is not None:
        values.extend(_coerce_path_input(file_paths))
    if file_paths_json:
        values.extend(_coerce_path_input(_parse_path_json(file_paths_json)))

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = normalize_lesson_file_path(value)
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized


def _coerce_path_input(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        parsed = _parse_path_json(value)
        if parsed is not value:
            return _coerce_path_input(parsed)
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


def _parse_path_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return value


def _build_file_lesson(
    memory: Any,
    touched_paths: list[str],
    tagged_path: str | None,
) -> dict[str, Any] | None:
    content = str(getattr(memory, "content", "") or "")
    tags = list(getattr(memory, "tags", []) or [])
    if "review-lesson" not in tags or "confirmed" not in tags:
        return None

    fields, evidence_paths = _parse_lesson_content(content)
    matched_path, evidence_path = _match_evidence_path(
        touched_paths=touched_paths,
        evidence_paths=evidence_paths,
        tagged_path=tagged_path,
    )
    if matched_path is None:
        return None

    prevention = fields.get("prevention", "")
    principle = fields.get("principle", "")
    avoid = _extract_avoid_text(prevention)
    do_text = _extract_do_text(prevention)
    return {
        "memory_id": str(getattr(memory, "id", "")),
        "pattern_id": fields.get("pattern_id") or _pattern_id_from_tags(tags),
        "matched_file_path": matched_path,
        "evidence_path": evidence_path or matched_path,
        "principle": principle,
        "prevention": prevention,
        "do": do_text or prevention or principle,
        "avoid": avoid,
    }


def _parse_lesson_content(content: str) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    for raw_line in content.splitlines():
        match = _LESSON_FIELD_RE.match(raw_line.strip())
        if not match:
            continue
        key = match.group("key")
        if key in {"pattern_id", "principle", "prevention", "path"}:
            fields[key] = match.group("value").strip()

    evidence_paths: list[str] = []
    if fields.get("path"):
        evidence_paths.append(fields["path"])

    evidence = _parse_evidence(content)
    if isinstance(evidence, dict):
        evidence_paths.extend(extract_file_paths_from_mapping(evidence))

    return fields, evidence_paths


def _parse_evidence(content: str) -> Any | None:
    marker = "## Evidence"
    if marker not in content:
        return None
    raw = content.split(marker, 1)[1].strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _match_evidence_path(
    *,
    touched_paths: list[str],
    evidence_paths: list[str],
    tagged_path: str | None,
) -> tuple[str | None, str | None]:
    for touched_path in touched_paths:
        for evidence_path in evidence_paths:
            if paths_match(touched_path, evidence_path):
                return touched_path, normalize_lesson_file_path(evidence_path)
    if tagged_path:
        return tagged_path, tagged_path
    return None, None


def _pattern_id_from_tags(tags: list[str]) -> str:
    for tag in tags:
        if tag.startswith("pattern:"):
            return tag.removeprefix("pattern:")
    return ""


def _extract_do_text(prevention: str) -> str:
    if "; avoid " in prevention.lower():
        index = prevention.lower().find("; avoid ")
        return prevention[:index].strip(" ;.")
    return prevention


def _extract_avoid_text(prevention: str) -> str:
    lower = prevention.lower()
    marker = "avoid "
    if marker not in lower:
        return ""
    index = lower.find(marker) + len(marker)
    return prevention[index:].strip(" ;.")
