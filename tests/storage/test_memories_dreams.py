"""Focused storage contracts for memory dream scheduling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import PERSONAL_PROJECT_ID


def _insert_project(db, project_id: str) -> None:
    db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (project_id, f"Project {project_id}"),
    )


def _dream_state(db, memory_id: str) -> tuple[datetime | None, int]:
    row = db.fetchone(
        "SELECT last_dreamed_at, dream_due_version FROM memories WHERE id = %s",
        (memory_id,),
    )
    assert row is not None
    return row["last_dreamed_at"], int(row["dream_due_version"])


def test_mark_memories_due(temp_db) -> None:
    """Only listed rows that still match the expected active scope become due."""
    manager = LocalMemoryManager(temp_db)
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    _insert_project(temp_db, project_a)
    _insert_project(temp_db, project_b)
    dreamed_at = datetime.now(UTC)

    eligible = manager.create_memory(content="eligible", project_id=project_a)
    rescoped = manager.create_memory(content="rescoped", project_id=project_a)
    hidden = manager.create_memory(content="hidden", project_id=project_a)
    unrelated = manager.create_memory(content="unrelated", project_id=project_a)
    other_project = manager.create_memory(content="other project", project_id=project_b)
    global_memory = manager.create_memory(
        content="global", project_id=PERSONAL_PROJECT_ID, is_global=True
    )
    for memory in [eligible, rescoped, hidden, unrelated, other_project, global_memory]:
        manager.mark_dreamed(memory.id, when=dreamed_at)

    temp_db.execute(
        "UPDATE memories SET project_id = %s WHERE id = %s",
        (project_b, rescoped.id),
    )
    manager.mark_dreamed(hidden.id, hidden_as="review", when=dreamed_at)

    affected = manager.mark_memories_due(
        [eligible.id, rescoped.id, hidden.id, other_project.id, global_memory.id],
        expected_project_id=project_a,
    )

    assert affected == 1
    assert _dream_state(temp_db, eligible.id) == (None, 1)
    for untouched in [rescoped, hidden, unrelated, other_project, global_memory]:
        last_dreamed_at, dream_due_version = _dream_state(temp_db, untouched.id)
        assert last_dreamed_at is not None
        assert dream_due_version == 0

    global_affected = manager.mark_memories_due(
        [eligible.id, global_memory.id],
        expected_project_id=None,
    )

    assert global_affected == 1
    assert _dream_state(temp_db, global_memory.id) == (None, 1)
    assert _dream_state(temp_db, eligible.id) == (None, 1)
