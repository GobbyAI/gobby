from __future__ import annotations

import pytest

from gobby.review_learning.fingerprint import build_occurrence_key
from gobby.review_learning.lessons import (
    derive_lesson_identity,
    has_verified_fix,
    normalize_lesson,
)

pytestmark = pytest.mark.unit


def test_pattern_id_derives_from_lesson_type_and_principle() -> None:
    identity = derive_lesson_identity(
        {"lesson_type": "sql-placeholders", "principle": "Use psycopg %s placeholders"}
    )

    assert identity.promotable is True
    assert identity.pattern_id.startswith("sql-placeholders:")
    assert identity.pattern_key.startswith("sql-placeholders")


def test_non_promotable_fallback_when_pattern_is_underivable() -> None:
    identity = derive_lesson_identity({"title": "One-off finding"})

    assert identity.promotable is False
    assert identity.pattern_id.startswith("non-promotable:")


def test_normalized_lesson_uses_bounded_tags_and_full_content() -> None:
    finding = {
        "title": "Wrong placeholder",
        "pattern_id": "Use psycopg %s placeholders in Gobby storage code",
        "lesson_type": "sql-placeholders",
        "rule_id": "SQL001",
        "severity": "high",
        "path": "src/gobby/storage/example.py",
        "start_line": 4,
        "query_hints": ["psycopg", "%s"],
    }
    occurrence_key = build_occurrence_key("review-1", "native-1")

    lesson = normalize_lesson(
        source_kind="review_comment",
        source="coderabbit",
        source_review="review-1",
        decision="confirmed",
        finding=finding,
        evidence={"commit": "abc123"},
        finding_fingerprint="native-1",
        occurrence_key=occurrence_key,
        repo="josh/gobby",
        language="python",
        risk="high",
    )

    assert "review-lesson" in lesson.tags
    assert "confirmed" in lesson.tags
    assert "source-kind:review_comment" in lesson.tags
    assert "source:coderabbit" in lesson.tags
    assert any(tag.startswith("fingerprint:") for tag in lesson.tags)
    assert any(tag.startswith("occurrence:") for tag in lesson.tags)
    assert "Use psycopg %s placeholders in Gobby storage code" in lesson.content
    assert '"commit": "abc123"' in lesson.content


@pytest.mark.parametrize(
    "evidence",
    [
        {"commit": "abc"},
        {"commit_sha": "abc"},
        {"verified_fix_ref": "abc"},
        {"fix_ref": "abc"},
        {"changes_id": "abc"},
    ],
)
def test_verified_fix_detection(evidence: dict[str, str]) -> None:
    assert has_verified_fix(evidence)
