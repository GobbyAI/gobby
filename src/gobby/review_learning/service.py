"""Deterministic review-learning service."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

from gobby.review_learning.class_recall import ReviewLessonClassRecall, ReviewLessonRetirement
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
from gobby.review_learning.guidance import (
    format_review_lesson_guidance,
    has_actionable_guidance,
)
from gobby.review_learning.lessons import (
    CI_SOURCE_KINDS,
    CODE_DOMAIN_EXCLUDED_TAGS,
    derive_lesson_domain,
    has_verified_fix,
    normalize_lesson,
    validate_decision,
    validate_source_kind,
)
from gobby.review_learning.promotion import (
    PromotionMemoryManager,
    PromotionTaskManager,
    promote_lesson,
)
from gobby.storage.hub.protocol import HubDatabase, ReviewLearningPatternMutation
from gobby.storage.session_resolution import resolve_session_reference
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)

# Recall builds at most two queries and searches ordinary and review-lesson
# scopes for each query, so this caps one request at 80 backend searches.
MAX_RECALL_FINDINGS = 20
# Matches also appear in per-finding groups; cap the flattened duplicate view.
MAX_RECALL_FLAT_MATCHES = 100

_SPACE_RE = re.compile(r"\s+")
_LESSON_FIELD_RE = re.compile(r"^-\s+(?P<key>[a-zA-Z_]+):\s*(?P<value>.*)$")
_LEGACY_SCAN_LIMIT = 200


class ReviewLearningMemoryManager(PromotionMemoryManager, Protocol):
    db: HubDatabase

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
        tags_none: list[str] | None = None,
        caller: str = "memory.search",
        include_global: bool = True,
    ) -> list[Any]: ...

    async def alist_memories(
        self,
        *,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_none: list[str] | None = None,
        include_global: bool = True,
    ) -> list[Any]: ...

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Any: ...


class ReviewLearningService:
    """Record confirmed review lessons and recall relevant memory context."""

    def __init__(
        self,
        memory_manager: ReviewLearningMemoryManager | None,
        task_manager: PromotionTaskManager,
        memory_manager_resolver: Callable[[], ReviewLearningMemoryManager | None] | None = None,
    ) -> None:
        self._seed_memory_manager = memory_manager
        self.task_manager = task_manager
        self._memory_manager_resolver = memory_manager_resolver

    @property
    def memory_manager(self) -> ReviewLearningMemoryManager:
        """Resolve the current-epoch memory manager, falling back to the seed."""
        if self._memory_manager_resolver is not None:
            resolved = self._memory_manager_resolver()
            if resolved is not None:
                return resolved
        if self._seed_memory_manager is None:
            raise RuntimeError("Review-learning memory manager is unavailable")
        return self._seed_memory_manager

    async def recall_context(
        self,
        findings: list[dict[str, Any] | str],
        proposed_changes: Any | None = None,
        source: str | None = None,
        source_kind: str | None = None,
        session_id: str | None = None,
        repo: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Search memories for review-context relevant to each finding."""
        normalized_findings = _normalize_recall_findings(findings)
        project_id, _ = await self._resolve_scope(session_id)
        grouped: list[dict[str, Any]] = []
        flat_matches: list[dict[str, Any]] = []
        for index, finding in enumerate(normalized_findings):
            queries = build_recall_queries(
                finding=finding,
                proposed_changes=proposed_changes,
                source=source,
                source_kind=source_kind,
                repo=repo,
                language=language,
            )
            try:
                matches = await self._search_recall_matches(project_id, index, queries)
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning(
                    "Review-learning recall failed open for finding_index=%s exception_class=%s",
                    index,
                    exc.__class__.__name__,
                    exc_info=True,
                    extra={
                        "finding_index": index,
                        "exception_class": exc.__class__.__name__,
                    },
                )
                matches = []
            grouped.append({"finding_index": index, "matches": matches})
            remaining_flat_matches = MAX_RECALL_FLAT_MATCHES - len(flat_matches)
            if remaining_flat_matches > 0:
                flat_matches.extend(matches[:remaining_flat_matches])
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
        resolved_project_id = project_id or (await self._resolve_scope(session_id))[0]
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
            if lesson is None or not has_actionable_guidance(lesson):
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

    async def recall_review_lessons_by_class(
        self,
        lesson_domain: str,
        lesson_types: list[str],
        source_kinds: list[str] | None = None,
        limit: int = 3,
    ) -> dict[str, Any]:
        """Recall confirmed lessons for a domain-qualified lesson class."""
        project_id = (await self._resolve_scope(None))[0]
        recall = ReviewLessonClassRecall(self.memory_manager, project_id)
        return await recall.recall_review_lessons_by_class(
            lesson_domain=lesson_domain,
            lesson_types=lesson_types,
            source_kinds=source_kinds,
            limit=limit,
        )

    async def list_check_keys(
        self,
        lesson_domain: str,
        lesson_type: str,
        category: str | None = None,
    ) -> dict[str, Any]:
        """List complete check-key identities for one lesson class."""
        project_id = (await self._resolve_scope(None))[0]
        recall = ReviewLessonClassRecall(self.memory_manager, project_id)
        return await recall.list_check_keys(
            lesson_domain=lesson_domain,
            lesson_type=lesson_type,
            category=category,
        )

    async def retire_review_lesson(
        self,
        *,
        pattern_id: str,
        evidence: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Retire confirmed occurrences of an obsolete review lesson."""
        project_id, _ = await self._resolve_record_scope(session_id)
        retirement = ReviewLessonRetirement(
            memory_manager=self.memory_manager,
            task_manager=self.task_manager,
            project_id=project_id,
        )
        return await retirement.retire(pattern_id=pattern_id, evidence=evidence)

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
        validated_source_kind = validate_source_kind(source_kind)
        lesson_domain = derive_lesson_domain(validated_source_kind)
        validated_decision = validate_decision(decision)
        if validated_decision in {"stale", "invalid"}:
            return {
                "decision": validated_decision,
                "skipped_reason": validated_decision,
                "promotable": False,
            }
        _validate_recorded_finding(finding)

        project_id, source_session_id = await self._resolve_record_scope(session_id)
        finding_fingerprint = derive_finding_fingerprint(finding)
        occurrence_key = build_occurrence_key(source_review, finding_fingerprint)
        normalized = normalize_lesson(
            source_kind=validated_source_kind,
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
            lesson_domain=lesson_domain,
        )
        if normalized.source_kind == "qa_rejection" and source == "epic-reviewer":
            missing_fields = _missing_qa_rejection_fields(finding, evidence)
            if missing_fields:
                skipped_reason = (
                    "missing_verified_fix"
                    if "confirmed_fix_evidence" in missing_fields
                    else "incomplete_finding"
                )
                return {
                    "pattern_id": normalized.identity.pattern_id,
                    "finding_fingerprint": finding_fingerprint,
                    "occurrence_key": occurrence_key,
                    "decision": validated_decision,
                    "promotable": normalized.identity.promotable,
                    "skipped_reason": skipped_reason,
                    "missing_fields": missing_fields,
                }

        lock = ReviewLearningPatternMutation(
            project_id=project_id,
            pattern_key=normalized.identity.pattern_key,
        )
        async with self.memory_manager.db.advisory_lock(lock):
            existing = await self.memory_manager.alist_memories(
                project_id=project_id,
                memory_type="pattern",
                limit=1,
                tags_all=["review-lesson", normalized.occurrence_tag],
            )
            if existing:
                memory = existing[0]
                promotion = await promote_lesson(
                    lesson=normalized,
                    evidence_memory_id=memory.id,
                    memory_manager=self.memory_manager,
                    task_manager=self.task_manager,
                    project_id=project_id,
                    source_session_id=source_session_id,
                )
                return {
                    "lesson_id": getattr(memory, "id", None),
                    "pattern_id": normalized.identity.pattern_id,
                    "finding_fingerprint": finding_fingerprint,
                    "occurrence_key": occurrence_key,
                    "decision": validated_decision,
                    "promotable": normalized.identity.promotable,
                    **promotion,
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
                tags_none = (
                    ["review-lesson"] if tags_all is None else list(CODE_DOMAIN_EXCLUDED_TAGS)
                )
                include_global = tags_all is None
                memories = await self.memory_manager.search_memories(
                    query=query,
                    project_id=project_id,
                    limit=5,
                    tags_all=tags_all,
                    tags_none=tags_none,
                    caller="review_learning.related_lessons",
                    include_global=include_global,
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

    async def _resolve_scope(self, session_id: str | None) -> tuple[str, str | None]:
        return await asyncio.to_thread(self._resolve_scope_sync, session_id)

    async def _resolve_record_scope(self, session_id: str | None) -> tuple[str, str | None]:
        if session_id is None:
            return await self._resolve_scope(None)
        return await asyncio.to_thread(self._resolve_explicit_scope_sync, session_id)

    def _resolve_explicit_scope_sync(self, session_id: str) -> tuple[str, str]:
        try:
            resolved_session_id = resolve_session_reference(
                self.memory_manager.db,
                session_id,
                _current_project_id(),
            )
            row = self.memory_manager.db.fetchone(
                "SELECT project_id FROM sessions WHERE id = %s",
                (resolved_session_id,),
            )
            if row and row.get("project_id"):
                return str(row["project_id"]), resolved_session_id
            raise RuntimeError(f"Session {resolved_session_id!r} has no project")
        except (AttributeError, RuntimeError, ValueError, OSError) as exc:
            raise RuntimeError(
                f"Review-learning could not resolve explicit session {session_id!r}"
            ) from exc

    def _resolve_scope_sync(self, session_id: str | None) -> tuple[str, str | None]:
        project_id = _current_project_id()
        effective_session_id = session_id or get_current_session_id()
        if not effective_session_id:
            if project_id is None:
                raise RuntimeError(
                    "Review-learning requires a project context or resolvable session_id"
                )
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
                return str(row["project_id"]), resolved_session_id
            if project_id is not None:
                return project_id, resolved_session_id
            raise RuntimeError(
                f"Review-learning could not resolve a project for session {effective_session_id!r}"
            )
        except (AttributeError, RuntimeError, ValueError, OSError) as exc:
            logger.debug(
                "Could not resolve review-learning session %r: %s",
                effective_session_id,
                exc,
                exc_info=True,
            )
            if project_id is not None:
                return project_id, None
            raise RuntimeError(
                "Review-learning requires a project context or resolvable session_id"
            ) from exc

    async def _candidate_lesson_memories(
        self,
        *,
        project_id: str,
        touched_paths: list[str],
        limit: int,
    ) -> list[tuple[Any, str | None]]:
        tagged_paths = {tag: path for path in touched_paths if (tag := path_tag(path))}
        tagged_candidates: list[tuple[Any, str | None]] = []
        untagged_candidates: list[tuple[Any, str | None]] = []
        seen: set[str] = set()
        code_domain_exclusions = list(CODE_DOMAIN_EXCLUDED_TAGS)

        memories = await self.memory_manager.alist_memories(
            project_id=project_id,
            memory_type="pattern",
            limit=max(_LEGACY_SCAN_LIMIT, limit),
            tags_all=["review-lesson", "confirmed"],
            tags_none=code_domain_exclusions,
            include_global=False,
        )
        for memory in memories:
            memory_id = str(getattr(memory, "id", "") or "")
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            memory_tags = set(getattr(memory, "tags", []) or [])
            matched_path = next(
                (path for tag, path in tagged_paths.items() if tag in memory_tags),
                None,
            )
            candidate = (memory, matched_path)
            if matched_path is None:
                untagged_candidates.append(candidate)
            else:
                tagged_candidates.append(candidate)

        return tagged_candidates + untagged_candidates


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


def _normalize_recall_findings(findings: list[dict[str, Any] | str]) -> list[dict[str, Any]]:
    """Normalize supported finding shapes up to the documented recall cap."""
    if not isinstance(findings, list):
        raise ValueError("findings must be an array")

    normalized: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        if index >= MAX_RECALL_FINDINGS:
            break
        if isinstance(finding, dict):
            normalized.append(copy.deepcopy(finding))
            continue
        if isinstance(finding, str):
            normalized.append({"message": finding})
            continue
        raise ValueError(f"findings[{index}] must be an object or string")
    return normalized


def _current_project_id() -> str | None:
    project_ctx = get_project_context()
    if project_ctx and project_ctx.get("id"):
        return str(project_ctx["id"])
    return None


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {val}" for key, val in value.items())
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _validate_recorded_finding(finding: dict[str, Any]) -> None:
    missing_groups: list[str] = []
    if not any(str(finding.get(field) or "").strip() for field in ("title", "message")):
        missing_groups.append("title or message")
    if not has_actionable_guidance(
        {
            "principle": finding.get("principle"),
            "prevention": finding.get("prevention"),
        }
    ):
        missing_groups.append("principle or prevention")
    if missing_groups:
        joined = "; ".join(missing_groups)
        raise ValueError(f"finding missing required non-empty field group(s): {joined}")


def _missing_qa_rejection_fields(
    finding: dict[str, Any],
    evidence: dict[str, Any],
) -> list[str]:
    def present(value: object) -> bool:
        return bool(str(value or "").strip())

    missing: list[str] = []
    for field in ("check_key", "lesson_type", "prevention", "path", "finding_fingerprint"):
        if not present(finding.get(field)):
            missing.append(field)
    if not any(present(finding.get(field)) for field in ("principle", "root_cause")):
        missing.append("principle_or_root_cause")
    if not present(evidence.get("leaf_task_ref")):
        missing.append("leaf_task_ref")
    if not has_verified_fix(evidence):
        missing.append("confirmed_fix_evidence")
    return missing


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
        "do": do_text if do_text or avoid else prevention or principle,
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
    matches = list(re.finditer(r"(?m)^## Evidence[ \t]*\r?$", content))
    if not matches:
        return None
    raw = content[matches[-1].end() :].strip()
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
    lower = prevention.lower()
    if lower.startswith("avoid "):
        return ""
    if "; avoid " in lower:
        index = lower.find("; avoid ")
        return prevention[:index].strip(" ;.")
    return prevention


def _extract_avoid_text(prevention: str) -> str:
    lower = prevention.lower()
    marker = "avoid "
    if marker not in lower:
        return ""
    index = lower.find(marker) + len(marker)
    return prevention[index:].strip(" ;.")
