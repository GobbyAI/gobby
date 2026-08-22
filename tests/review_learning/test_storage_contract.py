from __future__ import annotations

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.review_learning.file_paths import path_tag
from gobby.review_learning.fingerprint import fingerprint_tag, occurrence_tag
from gobby.review_learning.service import ReviewLearningService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.integration

PROMOTION_QUERY_LIMIT = 500
NOISE_MEMORY_COUNT = PROMOTION_QUERY_LIMIT * 3 + 1


def _finding() -> dict[str, str]:
    return {
        "title": "Durable writes missing",
        "pattern_id": "durable-write-after-state-change",
        "lesson_type": "durable-writes",
        "principle": "Persist state after changing it",
        "root_cause": "Mutation happened without a storage write",
        "prevention": "Add regression coverage around persistence",
        "path": "src/gobby/tasks/state.py",
    }


@pytest.mark.asyncio
async def test_storage_backed_dedupe_ignores_large_unrelated_window_without_tasks(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="review-learning-storage-contract",
        repo_path="/tmp/review-learning-storage-contract",
    )
    monkeypatch.setattr(
        "gobby.review_learning.service.get_project_context",
        lambda: {"id": project.id},
    )
    memory_manager = MemoryManager(db=temp_db, config=MemoryConfig())
    task_manager = LocalTaskManager(temp_db)
    service = ReviewLearningService(memory_manager, task_manager)

    try:
        first = await service.record(
            source_kind="agent_review",
            source="code-reviewer",
            source_review="review-1",
            decision="confirmed",
            finding=_finding(),
            evidence={"commit": "abc"},
        )
        first_memory = memory_manager.storage.get_memory(first["lesson_id"])
        assert first_memory is not None
        pattern_tag = next(tag for tag in (first_memory.tags or []) if tag.startswith("pattern:"))
        legacy_tags = {
            "review-lesson",
            "confirmed",
            "source-kind:agent_review",
            "source:code-reviewer",
            "pattern:durable-write-after-state-change",
            fingerprint_tag(first["finding_fingerprint"]),
            occurrence_tag(first["occurrence_key"]),
            "lesson-type:durable-writes",
            path_tag("src/gobby/tasks/state.py"),
        }
        assert set(first_memory.tags or []) == legacy_tags | {"lesson-domain:code"}

        for index in range(NOISE_MEMORY_COUNT):
            memory_manager.storage.create_memory(
                content=f"Unrelated review pattern {index}",
                memory_type="pattern",
                project_id=project.id,
                tags=[
                    "review-lesson",
                    "confirmed",
                    f"pattern:unrelated-{index}",
                    f"occurrence:unrelated-{index}",
                ],
            )

        duplicate = await service.record(
            source_kind="agent_review",
            source="code-reviewer",
            source_review="review-1",
            decision="confirmed",
            finding=_finding(),
            evidence={"commit": "abc"},
        )
        second = await service.record(
            source_kind="agent_review",
            source="code-reviewer",
            source_review="review-2",
            decision="confirmed",
            finding=_finding(),
            evidence={"commit": "def"},
        )

        target_memories = memory_manager.list_memories(
            project_id=project.id,
            memory_type="pattern",
            tags_all=["review-lesson", "confirmed", pattern_tag],
            limit=10,
            include_global=False,
        )
        assert duplicate["skipped_reason"] == "duplicate_occurrence"
        assert second["lesson_id"] != first["lesson_id"]
        assert "task_ref" not in second
        assert len(target_memories) == 2
        assert task_manager.list_tasks(project_id=project.id, closed=False, limit=10) == []
    finally:
        await memory_manager.close()
