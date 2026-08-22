from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry
from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.lessons import normalize_lesson, pattern_key_for
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from gobby.storage.hub.protocol import ReviewLearningPatternMutation
from tests.review_learning.conftest import (
    PROJECT_SCOPE_ID,
    FakeDB,
    FakeMemory,
    FakeMemoryManager,
    FakeTask,
    FakeTaskManager,
)

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-1111-1111-111111111111"
PATTERN_ID = "retire-obsolete-check"
PATTERN_KEY = pattern_key_for(PATTERN_ID)


class RecordingDB(FakeDB):
    def __init__(self) -> None:
        super().__init__(session_id=SESSION_ID, project_id=PROJECT_SCOPE_ID)
        self.entered_locks: list[object] = []
        self.active_locks: list[object] = []

    @asynccontextmanager
    async def advisory_lock(self, lock: object) -> AsyncIterator[None]:
        self.entered_locks.append(lock)
        self.active_locks.append(lock)
        try:
            yield
        finally:
            self.active_locks.remove(lock)


class RetirementMemoryManager(FakeMemoryManager):
    def __init__(self) -> None:
        self.recording_db = RecordingDB()
        super().__init__(db=self.recording_db)
        self.updated_memory_ids: list[str] = []

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> FakeMemory:
        assert self.recording_db.active_locks
        memory = next(memory for memory in self.memories if memory.id == memory_id)
        if content is not None:
            memory.content = content
        if tags is not None:
            memory.tags = tags
        if memory_type is not None:
            memory.memory_type = memory_type
        self.updated_memory_ids.append(memory_id)
        return memory


def _registry(
    memory_manager: RetirementMemoryManager,
    task_manager: FakeTaskManager,
) -> InternalToolRegistry:
    service = ReviewLearningService(
        memory_manager=cast(ReviewLearningMemoryManager, memory_manager),
        task_manager=cast(RetirementTaskManager, task_manager),
    )
    return create_review_learning_registry(service)


def _confirmed_lesson(memory_id: str, occurrence: str) -> FakeMemory:
    lesson = normalize_lesson(
        source_kind="agent_review",
        source="test-reviewer",
        source_review=f"review-{occurrence}",
        decision="confirmed",
        finding={
            "title": "Obsolete review check",
            "pattern_id": PATTERN_ID,
            "lesson_type": "idempotency",
            "principle": "Use the current state transition contract.",
            "prevention": "Check the current transition contract before reporting.",
            "path": "src/gobby/tasks/state.py",
        },
        evidence={"commit": "abc123"},
        finding_fingerprint=f"fingerprint-{occurrence}",
        occurrence_key=f"occurrence-{occurrence}",
        repo=None,
        language="python",
        risk="medium",
    )
    return FakeMemory(
        id=memory_id,
        content=lesson.content,
        project_id=PROJECT_SCOPE_ID,
        tags=lesson.tags,
    )


@pytest.mark.asyncio
async def test_retire_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    memory_manager = RetirementMemoryManager()
    memory_manager.memories = [
        _confirmed_lesson("memory-1", "one"),
        _confirmed_lesson("memory-2", "two"),
        FakeMemory(
            id="already-stale",
            content="retired",
            project_id=PROJECT_SCOPE_ID,
            tags=["review-lesson", "stale", f"pattern:{PATTERN_KEY}"],
        ),
        FakeMemory(
            id="other-pattern",
            content="active",
            project_id=PROJECT_SCOPE_ID,
            tags=["review-lesson", "confirmed", "pattern:other-pattern"],
        ),
    ]
    task_manager = FakeTaskManager()
    task_manager.tasks = [
        FakeTask(
            id="guardrail-open",
            seq_num=71,
            title="Guardrail: idempotency - retire-obsolete-check",
            description="open",
            labels=["review-learning", "guardrail", f"pattern:{PATTERN_KEY}"],
            category="test",
            validation_criteria="covered",
        ),
        FakeTask(
            id="non-guardrail-open",
            seq_num=72,
            title="Investigate retire-obsolete-check",
            description="open",
            labels=[f"pattern:{PATTERN_KEY}"],
            category="research",
            validation_criteria="covered",
        ),
        FakeTask(
            id="guardrail-closed",
            seq_num=73,
            title="Guardrail: old idempotency check",
            description="closed",
            labels=["guardrail", f"pattern:{PATTERN_KEY}"],
            category="test",
            validation_criteria="covered",
            closed_at="2026-07-23T00:00:00Z",
        ),
    ]
    registry = _registry(memory_manager, task_manager)

    missing_evidence = await registry.call(
        "retire_review_lesson",
        {"pattern_id": PATTERN_ID, "evidence": {}, "session_id": SESSION_ID},
    )
    assert missing_evidence == {
        "success": False,
        "error": "retire_review_lesson requires non-empty evidence",
    }

    result = await registry.call(
        "retire_review_lesson",
        {
            "pattern_id": PATTERN_ID,
            "evidence": {"reason": "The transition contract was replaced.", "commit": "def456"},
            "session_id": SESSION_ID,
        },
    )

    assert result == {
        "success": True,
        "pattern_id": PATTERN_ID,
        "affected_memory_ids": ["memory-1", "memory-2"],
        "guardrail_task_refs": ["#71"],
    }
    assert memory_manager.updated_memory_ids == ["memory-1", "memory-2"]
    for memory in memory_manager.memories[:2]:
        assert "confirmed" not in (memory.tags or [])
        assert "stale" in (memory.tags or [])
    assert memory_manager.recording_db.entered_locks == [
        ReviewLearningPatternMutation(
            project_id=PROJECT_SCOPE_ID,
            pattern_key=PATTERN_KEY,
        )
    ]
    assert task_manager.updated == []
    assert task_manager.tasks[0].closed_at is None

    monkeypatch.setattr("gobby.review_learning.service.get_project_context", lambda: None)
    monkeypatch.setattr("gobby.review_learning.service.get_current_session_id", lambda: None)
    missing_scope = await _registry(RetirementMemoryManager(), FakeTaskManager()).call(
        "retire_review_lesson",
        {"pattern_id": PATTERN_ID, "evidence": {"reason": "obsolete"}},
    )
    assert missing_scope["success"] is False
    assert "requires a project context" in missing_scope["error"]


@pytest.mark.asyncio
async def test_retired_absent_from_recalls() -> None:
    memory_manager = RetirementMemoryManager()
    memory_manager.memories = [_confirmed_lesson("memory-1", "one")]
    registry = _registry(memory_manager, FakeTaskManager())

    before_file = await registry.call(
        "recall_review_lessons_for_files",
        {"file_paths": ["src/gobby/tasks/state.py"], "session_id": SESSION_ID},
    )
    before_class = await registry.call(
        "recall_review_lessons_by_class",
        {"lesson_domain": "code", "lesson_types": ["idempotency"]},
    )
    assert before_file["count"] == 1
    assert before_class["count"] == 1

    retired = await registry.call(
        "retire_review_lesson",
        {
            "pattern_id": PATTERN_ID,
            "evidence": {"reason": "The injected lesson is obsolete."},
            "session_id": SESSION_ID,
        },
    )
    assert retired["success"] is True

    after_file = await registry.call(
        "recall_review_lessons_for_files",
        {"file_paths": ["src/gobby/tasks/state.py"], "session_id": SESSION_ID},
    )
    after_class = await registry.call(
        "recall_review_lessons_by_class",
        {"lesson_domain": "code", "lesson_types": ["idempotency"]},
    )
    assert after_file == {"success": True, "count": 0, "lessons": [], "message": ""}
    assert after_class == {"success": True, "count": 0, "lessons": [], "message": ""}
