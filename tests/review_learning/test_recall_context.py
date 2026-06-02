from __future__ import annotations

import pytest

from gobby.review_learning.service import ReviewLearningService, build_recall_queries
from tests.review_learning.conftest import FakeMemory

pytestmark = pytest.mark.unit


def test_query_construction_includes_diagnostic_and_fix_terms() -> None:
    queries = build_recall_queries(
        finding={
            "title": "Wrong SQL placeholder",
            "message": "Use $1",
            "suggestion": "replace %s with $1",
            "path": "src/gobby/storage/example.py",
            "symbol": "save",
            "rule_id": "SQL001",
            "query_hints": ["psycopg", "%s"],
        },
        proposed_changes={"fix": "change placeholders"},
        source="coderabbit",
        source_kind="review_comment",
        repo="josh/gobby",
        language="python",
    )

    query = queries[0]
    assert "Wrong SQL placeholder" in query
    assert "replace %s with $1" in query
    assert "src/gobby/storage/example.py" in query
    assert "SQL001" in query
    assert "psycopg" in query
    assert "change placeholders" in query


@pytest.mark.asyncio
async def test_recall_returns_ordinary_and_review_lesson_memories(
    fake_memory_manager, fake_task_manager
) -> None:
    fake_memory_manager.search_results = [
        FakeMemory(
            id="mem-ordinary",
            content="Gobby psycopg storage uses %s placeholders.",
            tags=["sql"],
        ),
        FakeMemory(
            id="mem-lesson",
            content="Review lesson: reject $1 recommendations for psycopg.",
            tags=["review-lesson", "pattern:sql-placeholders"],
        ),
    ]
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.recall_context(
        findings=[{"title": "Use $1 placeholders", "query_hints": ["psycopg", "%s"]}],
        source="coderabbit",
        source_kind="review_comment",
    )

    memory_ids = {match["memory_id"] for match in result["matches"]}
    assert memory_ids == {"mem-ordinary", "mem-lesson"}
    assert result["findings"][0]["finding_index"] == 0
    assert all(match["finding_index"] == 0 for match in result["matches"])


@pytest.mark.asyncio
async def test_recall_fails_open_on_memory_search_errors(
    fake_memory_manager, fake_task_manager
) -> None:
    fake_memory_manager.raise_on_search = True
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.recall_context(findings=[{"title": "anything"}])

    assert result["findings"] == [{"finding_index": 0, "matches": []}]
    assert result["matches"] == []
