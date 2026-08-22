from __future__ import annotations

import logging
import threading
from typing import Any, cast

import pytest

from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.lessons import CODE_DOMAIN_EXCLUDED_TAGS
from gobby.review_learning.service import (
    ReviewLearningMemoryManager,
    ReviewLearningService,
    build_recall_queries,
)
from tests.review_learning.conftest import FakeDB, FakeMemory, FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit
SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _service(
    memory_manager: FakeMemoryManager,
    task_manager: FakeTaskManager,
) -> ReviewLearningService:
    return ReviewLearningService(
        cast(ReviewLearningMemoryManager, memory_manager),
        cast(RetirementTaskManager, task_manager),
    )


def _scoped_memory_manager(project_id: str = "project") -> FakeMemoryManager:
    return FakeMemoryManager(db=FakeDB(session_id=SESSION_ID, project_id=project_id))


@pytest.mark.asyncio
async def test_resolve_scope_offloads_database_io(
    fake_task_manager: FakeTaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_loop_thread = threading.get_ident()
    database_threads: list[int] = []
    db = FakeDB(session_id=SESSION_ID)
    fetchone = db.fetchone

    def tracking_fetchone(sql: str, params: tuple[Any, ...]) -> dict[str, str] | None:
        database_threads.append(threading.get_ident())
        return fetchone(sql, params)

    monkeypatch.setattr(db, "fetchone", tracking_fetchone)
    service = _service(FakeMemoryManager(db=db), fake_task_manager)

    project_id, resolved_session_id = await service._resolve_scope(SESSION_ID)

    assert project_id == "session-project"
    assert resolved_session_id == SESSION_ID
    assert database_threads
    assert all(thread_id != event_loop_thread for thread_id in database_threads)


@pytest.mark.asyncio
async def test_record_rejects_unresolvable_explicit_session(
    fake_task_manager: FakeTaskManager,
) -> None:
    memory_manager = FakeMemoryManager(db=FakeDB())
    service = _service(memory_manager, fake_task_manager)

    with pytest.raises(RuntimeError, match="could not resolve explicit session"):
        await service.record(
            source_kind="agent_review",
            source="code-reviewer",
            source_review="review-1",
            decision="confirmed",
            finding={
                "title": "Durable writes missing",
                "pattern_id": "durable-write-after-state-change",
                "lesson_type": "durable-writes",
                "principle": "Persist state after changing it",
                "root_cause": "Mutation happened without a storage write",
                "prevention": "Add regression coverage around persistence",
                "path": "src/gobby/tasks/state.py",
            },
            evidence={"commit": "abc"},
            session_id="missing-session",
        )

    assert memory_manager.memories == []


@pytest.mark.asyncio
async def test_recall_falls_back_to_project_for_unresolvable_explicit_session(
    fake_task_manager: FakeTaskManager,
) -> None:
    service = _service(FakeMemoryManager(db=FakeDB()), fake_task_manager)

    result = await service.recall_context(
        findings=[{"title": "Durable writes missing"}],
        session_id="missing-session",
    )

    assert result == {"findings": [{"finding_index": 0, "matches": []}], "matches": []}


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
async def test_recall_context_deep_copies_nested_finding_data(
    fake_task_manager: FakeTaskManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_memory_manager = _scoped_memory_manager()
    finding = {
        "title": "Preserve caller data",
        "query_hints": ["copy"],
        "metadata": {"paths": ["src/gobby/review_learning/service.py"]},
    }
    service = _service(fake_memory_manager, fake_task_manager)

    def mutating_build_recall_queries(**kwargs: Any) -> list[str]:
        normalized = kwargs["finding"]
        normalized["query_hints"].append("mutated")
        normalized["metadata"]["paths"].append("tests/review_learning/test_recall_context.py")
        return ["copy"]

    monkeypatch.setattr(
        "gobby.review_learning.service.build_recall_queries",
        mutating_build_recall_queries,
    )
    await service.recall_context(findings=[finding], session_id=SESSION_ID)

    assert finding == {
        "title": "Preserve caller data",
        "query_hints": ["copy"],
        "metadata": {"paths": ["src/gobby/review_learning/service.py"]},
    }


@pytest.mark.asyncio
async def test_recall_returns_ordinary_and_review_lesson_memories(
    fake_task_manager: FakeTaskManager,
) -> None:
    fake_memory_manager = _scoped_memory_manager()
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
    service = _service(fake_memory_manager, fake_task_manager)

    result = await service.recall_context(
        findings=[{"title": "Use $1 placeholders", "query_hints": ["psycopg", "%s"]}],
        source="coderabbit",
        source_kind="review_comment",
        session_id=SESSION_ID,
    )

    memory_ids = {match["memory_id"] for match in result["matches"]}
    assert memory_ids == {"mem-ordinary", "mem-lesson"}
    assert result["findings"][0]["finding_index"] == 0
    assert all(match["finding_index"] == 0 for match in result["matches"])


@pytest.mark.asyncio
async def test_code_domain_excludes_plan_lessons(
    fake_task_manager: FakeTaskManager,
) -> None:
    fake_memory_manager = _scoped_memory_manager()
    fake_memory_manager.search_results = [
        FakeMemory(
            id="mem-code-lesson",
            content="Code-domain review lesson.",
            tags=["review-lesson", "lesson-domain:code"],
        ),
        FakeMemory(
            id="mem-plan-lesson",
            content="Plan-domain review lesson.",
            tags=["review-lesson", "lesson-domain:plan"],
        ),
    ]
    service = _service(fake_memory_manager, fake_task_manager)

    result = await service.recall_context(
        findings=[{"title": "Review this implementation"}],
        source="coderabbit",
        source_kind="review_comment",
        session_id=SESSION_ID,
    )

    assert {match["memory_id"] for match in result["matches"]} == {"mem-code-lesson"}
    lesson_calls = [
        query
        for query in fake_memory_manager.search_queries
        if query["tags_all"] == ["review-lesson"]
    ]
    ordinary_calls = [
        query for query in fake_memory_manager.search_queries if query["tags_all"] is None
    ]
    assert lesson_calls
    assert all(query["tags_none"] == list(CODE_DOMAIN_EXCLUDED_TAGS) for query in lesson_calls)
    assert all(query["tags_none"] == ["review-lesson"] for query in ordinary_calls)


@pytest.mark.asyncio
async def test_recall_keeps_global_ordinary_memory_but_excludes_global_review_lessons(
    fake_task_manager: FakeTaskManager,
) -> None:
    fake_memory_manager = _scoped_memory_manager()
    fake_memory_manager.search_results = [
        FakeMemory(
            id="mem-global-ordinary",
            content="Gobby psycopg storage uses %s placeholders.",
            project_id=None,
            tags=["sql"],
        ),
        FakeMemory(
            id="mem-global-lesson",
            content="Review lesson: reject $1 recommendations for psycopg.",
            project_id=None,
            tags=["review-lesson", "pattern:sql-placeholders"],
        ),
        FakeMemory(
            id="mem-project-lesson",
            content="Project review lesson: keep %s placeholders for psycopg.",
            tags=["review-lesson", "pattern:sql-placeholders"],
        ),
    ]
    service = _service(fake_memory_manager, fake_task_manager)

    result = await service.recall_context(
        findings=[{"title": "Use $1 placeholders", "query_hints": ["psycopg", "%s"]}],
        source="coderabbit",
        source_kind="review_comment",
        session_id=SESSION_ID,
    )

    memory_ids = {match["memory_id"] for match in result["matches"]}
    assert memory_ids == {"mem-global-ordinary", "mem-project-lesson"}

    ordinary_calls = [
        query for query in fake_memory_manager.search_queries if query["tags_all"] is None
    ]
    lesson_calls = [
        query
        for query in fake_memory_manager.search_queries
        if query["tags_all"] == ["review-lesson"]
    ]
    assert ordinary_calls
    assert lesson_calls
    assert all(query["tags_none"] == ["review-lesson"] for query in ordinary_calls)
    assert all(query["include_global"] is True for query in ordinary_calls)
    assert all(query["include_global"] is False for query in lesson_calls)


@pytest.mark.asyncio
async def test_recall_fails_open_on_memory_search_errors(
    fake_task_manager: FakeTaskManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_memory_manager = _scoped_memory_manager()
    fake_memory_manager.raise_on_search = True
    service = _service(fake_memory_manager, fake_task_manager)

    with caplog.at_level(logging.WARNING, logger="gobby.review_learning.service"):
        result = await service.recall_context(
            findings=[{"title": "anything"}],
            session_id=SESSION_ID,
        )

    assert result["findings"] == [{"finding_index": 0, "matches": []}]
    assert result["matches"] == []
    record = next(
        (
            record
            for record in caplog.records
            if record.message.startswith("Review-learning recall failed open")
        ),
        None,
    )
    assert record is not None, "Expected fail-open recall warning log record"
    assert "finding_index=0" in record.message
    assert "exception_class=RuntimeError" in record.message
    assert getattr(record, "finding_index", None) == 0
    assert getattr(record, "exception_class", None) == "RuntimeError"


@pytest.mark.asyncio
async def test_recall_requires_project_scope(
    fake_task_manager: FakeTaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opt out of the package-level autouse project-context pin: this test
    # asserts the no-scope failure mode.
    monkeypatch.setattr(
        "gobby.review_learning.service.get_project_context",
        lambda: None,
    )
    monkeypatch.setattr(
        "gobby.review_learning.service.get_current_session_id",
        lambda: None,
    )
    service = _service(FakeMemoryManager(db=FakeDB()), fake_task_manager)

    with pytest.raises(RuntimeError, match="requires a project context"):
        await service.recall_context(findings=[{"title": "anything"}])
