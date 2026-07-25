"""Deterministic review-lesson recall by lesson class."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from gobby.review_learning.guidance import (
    format_review_lesson_guidance,
    has_actionable_guidance,
)
from gobby.review_learning.lessons import SOURCE_KIND_DOMAIN, pattern_key_for, slugify
from gobby.storage.hub.protocol import HubDatabase, ReviewLearningPatternMutation

_PAGE_SIZE = 100
_GUARDRAIL_TASK_LIMIT = 50
_VALID_LESSON_DOMAINS = frozenset(SOURCE_KIND_DOMAIN.values())


class ClassRecallMemoryManager(Protocol):
    """Memory operations required by class-scoped lesson recall."""

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


class RetirementMemoryManager(ClassRecallMemoryManager, Protocol):
    """Memory operations required to retire a review lesson."""

    db: HubDatabase

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Any: ...


class RetirementTaskManager(Protocol):
    """Task lookup required to report guardrails associated with a lesson."""

    def list_tasks(
        self,
        *,
        project_id: str,
        closed: bool,
        label: str,
        limit: int,
    ) -> list[Any]: ...


class ReviewLessonRetirement:
    """Project-scoped retirement for obsolete confirmed review lessons."""

    def __init__(
        self,
        memory_manager: RetirementMemoryManager,
        task_manager: RetirementTaskManager,
        project_id: str,
    ) -> None:
        if not project_id:
            raise RuntimeError("Review-learning retirement requires a project context")
        self._memory_manager = memory_manager
        self._task_manager = task_manager
        self._project_id = project_id

    async def retire(
        self,
        *,
        pattern_id: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Retag every confirmed occurrence for a pattern and report its guardrail tasks."""
        if not isinstance(pattern_id, str) or not pattern_id.strip():
            raise ValueError("retire_review_lesson requires a non-empty pattern_id")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("retire_review_lesson requires non-empty evidence")

        normalized_pattern_id = pattern_id.strip()
        pattern_key = pattern_key_for(normalized_pattern_id)
        lock = ReviewLearningPatternMutation(
            project_id=self._project_id,
            pattern_key=pattern_key,
        )
        async with self._memory_manager.db.advisory_lock(lock):
            memories = await self._confirmed_memories(pattern_key)
            affected_memory_ids: list[str] = []
            for memory in memories:
                memory_id = _memory_id(memory)
                if not memory_id:
                    continue
                await self._memory_manager.update_memory(
                    memory_id,
                    tags=_replace_confirmed_with_stale(getattr(memory, "tags", None)),
                )
                affected_memory_ids.append(memory_id)

            tasks = await asyncio.to_thread(
                self._task_manager.list_tasks,
                project_id=self._project_id,
                closed=False,
                label=f"pattern:{pattern_key}",
                limit=_GUARDRAIL_TASK_LIMIT,
            )
            guardrail_task_refs = [
                _task_ref(task)
                for task in tasks
                if str(getattr(task, "title", "")).startswith("Guardrail:")
            ]

        return {
            "pattern_id": normalized_pattern_id,
            "affected_memory_ids": affected_memory_ids,
            "guardrail_task_refs": guardrail_task_refs,
        }

    async def _confirmed_memories(self, pattern_key: str) -> list[Any]:
        memories: list[Any] = []
        offset = 0
        while True:
            page = await self._memory_manager.alist_memories(
                project_id=self._project_id,
                memory_type="pattern",
                limit=_PAGE_SIZE,
                offset=offset,
                tags_all=["review-lesson", "confirmed", f"pattern:{pattern_key}"],
                include_global=False,
            )
            memories.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += len(page)
        return memories


class ReviewLessonClassRecall:
    """Project-scoped deterministic recall for review-lesson classes."""

    def __init__(self, memory_manager: ClassRecallMemoryManager, project_id: str) -> None:
        if not project_id:
            raise RuntimeError("Review-learning class recall requires a project context")
        self._memory_manager = memory_manager
        self._project_id = project_id

    async def recall_review_lessons_by_class(
        self,
        lesson_domain: str,
        lesson_types: list[str],
        source_kinds: list[str] | None = None,
        limit: int = 3,
    ) -> dict[str, Any]:
        """Recall lessons matching a domain-qualified lesson class."""
        validated_domain = _validate_lesson_domain(lesson_domain)
        validated_types = _validate_lesson_types(lesson_types)
        validated_sources = _validate_source_kinds(validated_domain, source_kinds)
        bounded_limit = max(1, min(int(limit), 5))

        memories: dict[str, Any] = {}
        for lesson_type in validated_types:
            base_tags = [
                "review-lesson",
                "confirmed",
                f"lesson-domain:{validated_domain}",
                f"lesson-type:{lesson_type}",
            ]
            if validated_sources is not None:
                queries = [base_tags + [f"source-kind:{source}"] for source in validated_sources]
            else:
                queries = [base_tags]
            for tags_all in queries:
                for memory in await self._list_all(tags_all):
                    memory_id = _memory_id(memory)
                    if memory_id:
                        memories[memory_id] = memory

        ranked = _dedupe_and_rank(list(memories.values()))
        lessons: list[dict[str, Any]] = []
        for group in ranked:
            lesson = _build_lesson(group)
            if not has_actionable_guidance(lesson):
                continue
            lessons.append(lesson)
            if len(lessons) >= bounded_limit:
                break
        return {
            "count": len(lessons),
            "lessons": lessons,
            "message": format_review_lesson_guidance(
                lessons,
                scope_label="matched lesson class",
            ),
        }

    async def list_check_keys(
        self,
        lesson_domain: str,
        lesson_type: str,
        category: str | None = None,
    ) -> dict[str, Any]:
        """List every distinct check key recorded for one lesson class."""
        validated_domain = _validate_lesson_domain(lesson_domain)
        validated_type = _validate_lesson_types([lesson_type])[0]
        tags_all = [
            "review-lesson",
            "confirmed",
            f"lesson-domain:{validated_domain}",
            f"lesson-type:{validated_type}",
        ]
        if category is not None:
            if not isinstance(category, str) or not category.strip():
                raise ValueError("category must be a non-empty string")
            tags_all.append(f"category:{slugify(category)}")

        keys = {
            value
            for memory in await self._list_all(tags_all)
            if (value := _tag_value(memory, "check-key:"))
        }
        ordered = sorted(keys)
        return {"count": len(ordered), "check_keys": ordered}

    async def _list_all(self, tags_all: list[str]) -> list[Any]:
        memories: list[Any] = []
        seen: set[str] = set()
        offset = 0
        while True:
            page = await self._memory_manager.alist_memories(
                project_id=self._project_id,
                memory_type="pattern",
                limit=_PAGE_SIZE,
                offset=offset,
                tags_all=tags_all,
                include_global=False,
            )
            if not page:
                break
            for memory in page:
                memory_id = _memory_id(memory)
                if memory_id and memory_id not in seen:
                    seen.add(memory_id)
                    memories.append(memory)
            offset += len(page)
            if len(page) < _PAGE_SIZE:
                break
        return memories


def _validate_lesson_domain(lesson_domain: str) -> str:
    if lesson_domain not in _VALID_LESSON_DOMAINS:
        choices = ", ".join(sorted(_VALID_LESSON_DOMAINS))
        raise ValueError(f"lesson_domain must be one of: {choices}")
    return lesson_domain


def _validate_lesson_types(lesson_types: list[str]) -> list[str]:
    if not isinstance(lesson_types, list) or not lesson_types:
        raise ValueError("lesson_types must contain at least one lesson type")
    normalized: set[str] = set()
    for lesson_type in lesson_types:
        if not isinstance(lesson_type, str) or not lesson_type.strip():
            raise ValueError("lesson_types must contain non-empty strings")
        normalized.add(slugify(lesson_type, max_length=40))
    return sorted(normalized)


def _validate_source_kinds(
    lesson_domain: str,
    source_kinds: list[str] | None,
) -> list[str] | None:
    if source_kinds is None:
        return None
    if not isinstance(source_kinds, list):
        raise ValueError("source_kinds must be a list")
    validated: set[str] = set()
    for source_kind in source_kinds:
        if source_kind not in SOURCE_KIND_DOMAIN:
            raise ValueError(f"unknown source kind: {source_kind!r}")
        if SOURCE_KIND_DOMAIN[source_kind] != lesson_domain:
            raise ValueError(
                f"source kind {source_kind!r} does not belong to lesson_domain {lesson_domain!r}"
            )
        validated.add(source_kind)
    return sorted(validated)


def _dedupe_and_rank(memories: list[Any]) -> list[list[Any]]:
    groups: dict[str, list[Any]] = {}
    for memory in memories:
        pattern = _tag_value(memory, "pattern:")
        if not pattern:
            pattern = _content_fields(memory).get("pattern_id") or _memory_id(memory)
        if pattern:
            groups.setdefault(pattern, []).append(memory)

    ranked = list(groups.values())
    for group in ranked:
        group.sort(key=_memory_id)
        group.sort(key=_created_at, reverse=True)
    ranked.sort(key=lambda group: _memory_id(group[0]))
    ranked.sort(key=lambda group: _created_at(group[0]), reverse=True)
    ranked.sort(key=_occurrence_count, reverse=True)
    return ranked


def _build_lesson(group: list[Any]) -> dict[str, Any]:
    memory = group[0]
    fields = _content_fields(memory)
    prevention = fields.get("prevention", "")
    avoid = _extract_avoid_text(prevention)
    do_text = _extract_do_text(prevention)
    return {
        "memory_id": _memory_id(memory),
        "pattern_id": fields.get("pattern_id") or _tag_value(memory, "pattern:"),
        "lesson_domain": _tag_value(memory, "lesson-domain:"),
        "lesson_type": _tag_value(memory, "lesson-type:"),
        "source_kind": _tag_value(memory, "source-kind:"),
        "category": _tag_value(memory, "category:"),
        "check_key": _tag_value(memory, "check-key:"),
        "occurrence_count": _occurrence_count(group),
        "principle": fields.get("principle", ""),
        "prevention": prevention,
        "do": do_text if do_text or avoid else prevention or fields.get("principle", ""),
        "avoid": avoid,
    }


def _memory_id(memory: Any) -> str:
    return str(getattr(memory, "id", "") or "")


def _created_at(memory: Any) -> datetime:
    value = getattr(memory, "created_at", None)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=UTC)


def _occurrence_count(memories: list[Any]) -> int:
    occurrences = {
        tag
        for memory in memories
        for tag in (getattr(memory, "tags", None) or [])
        if isinstance(tag, str) and tag.startswith("occurrence:")
    }
    return len(occurrences)


def _tag_value(memory: Any, prefix: str) -> str:
    for tag in getattr(memory, "tags", None) or []:
        if isinstance(tag, str) and tag.startswith(prefix):
            return tag.removeprefix(prefix)
    return ""


def _content_fields(memory: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in str(getattr(memory, "content", "") or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line.removeprefix("- ").split(":", 1)
        if key in {"pattern_id", "principle", "prevention"}:
            fields[key] = value.strip()
    return fields


def _extract_do_text(prevention: str) -> str:
    lower = prevention.lower()
    if lower.startswith("avoid "):
        return ""
    if "; avoid " in lower:
        return prevention[: lower.find("; avoid ")].strip(" ;.")
    return prevention


def _extract_avoid_text(prevention: str) -> str:
    marker = "avoid "
    lower = prevention.lower()
    if marker not in lower:
        return ""
    return prevention[lower.find(marker) + len(marker) :].strip(" ;.")


def _replace_confirmed_with_stale(tags: list[str] | None) -> list[str]:
    retagged: list[str] = []
    for tag in tags or []:
        replacement = "stale" if tag == "confirmed" else tag
        if replacement not in retagged:
            retagged.append(replacement)
    if "stale" not in retagged:
        retagged.append("stale")
    return retagged


def _task_ref(task: Any) -> str:
    seq_num = getattr(task, "seq_num", None)
    if seq_num:
        return f"#{seq_num}"
    return str(getattr(task, "id", ""))[:8]
