"""Tests for task compaction."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from gobby.storage.compaction import TaskCompactor
from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager


@pytest.fixture
def manager(temp_db):
    return LocalTaskManager(temp_db)


def test_find_candidates(manager, sample_project):
    """Test identifying tasks eligible for compaction."""
    proj_id = sample_project["id"]

    # Create tasks
    # 1. Closed recently (not candidate)
    t1 = manager.create_task(proj_id, "Recent Closed")
    manager.close_task(t1.id)

    # 2. Closed long ago (candidate)
    t2 = manager.create_task(proj_id, "Old Closed")
    manager.close_task(t2.id)

    # Manually update updated_at to be old
    old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    manager.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old_date, t2.id))

    # 3. Open task (not candidate)
    t3 = manager.create_task(proj_id, "Open Task")
    # Manually update updated_at to be old
    manager.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old_date, t3.id))

    compactor = TaskCompactor(manager)
    candidates = compactor.find_candidates(days_closed=30)

    assert len(candidates) == 1
    assert candidates[0]["id"] == t2.id


def test_compact_task(manager, sample_project):
    """Test compaction application."""
    proj_id = sample_project["id"]
    t1 = manager.create_task(proj_id, "To Be Compacted", description="Original description")
    manager.close_task(t1.id)

    compactor = TaskCompactor(manager)
    summary = "This task was completed."

    compactor.compact_task(t1.id, summary)

    # Verify updates
    task = manager.get_task(t1.id)
    assert task.description == summary

    # Verify DB column manually since Task object might not have it exposed in properties if not updated
    # (Task object *does* map columns now? No, we didn't add it to Task dataclass yet, only DB)
    # Wait, did we add it to Task dataclass? No.
    row = manager.db.fetchone("SELECT * FROM tasks WHERE id = ?", (t1.id,))
    assert row["summary"] == summary
    assert row["compacted_at"] is not None


def test_get_stats(manager, sample_project):
    """Test statistics calculation."""
    proj_id = sample_project["id"]
    # Create mix of tasks
    manager.create_task(proj_id, "Open")

    t1 = manager.create_task(proj_id, "Closed Normal")
    manager.close_task(t1.id)

    t2 = manager.create_task(proj_id, "Closed Compacted")
    manager.close_task(t2.id)

    compactor = TaskCompactor(manager)
    compactor.compact_task(t2.id, "Summary")

    stats = compactor.get_stats()
    assert stats["total_closed"] == 2
    assert stats["compacted"] == 1
    assert stats["rate"] == 50.0
