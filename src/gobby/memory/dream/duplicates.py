"""Duplicate detection for memory dream."""

from __future__ import annotations

from collections import defaultdict

from gobby.memory.dream.models import DreamCandidate, DuplicateGroup


def find_duplicate_groups(candidates: list[DreamCandidate]) -> list[DuplicateGroup]:
    """Find exact duplicate content groups among dream candidates."""
    grouped: dict[tuple[str | None, str], list[DreamCandidate]] = defaultdict(list)
    for candidate in candidates:
        normalized = _normalize_content(candidate.content)
        if normalized:
            grouped[(candidate.project_id, normalized)].append(candidate)

    duplicate_groups: list[DuplicateGroup] = []
    for entries in grouped.values():
        if len(entries) < 2:
            continue
        ordered = sorted(entries, key=lambda item: (item.created_at, item.id))
        canonical = _select_canonical(ordered)
        duplicate_groups.append(
            DuplicateGroup(
                memory_ids=[item.id for item in ordered],
                canonical_content=canonical.content,
                reason="Exact normalized duplicate content",
            )
        )

    return duplicate_groups


def _select_canonical(ordered: list[DreamCandidate]) -> DreamCandidate:
    """Choose the longest content; ties keep the earliest candidate."""
    return max(
        enumerate(ordered),
        key=lambda item: (len(item[1].content), -item[0]),
    )[1]


def _normalize_content(content: object) -> str:
    if not isinstance(content, str):
        return ""
    return " ".join(content.strip().lower().split())
