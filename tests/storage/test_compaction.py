"""Tests for task compaction."""

from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.compaction import TaskCompactor
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.mark.integration
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
    old_date = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    manager.db.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (old_date, t2.id))

    # 3. Open task (not candidate)
    t3 = manager.create_task(proj_id, "Open Task")
    # Manually update updated_at to be old
    manager.db.execute("UPDATE tasks SET updated_at = %s WHERE id = %s", (old_date, t3.id))

    compactor = TaskCompactor(manager)
    candidates = compactor.find_candidates(days_closed=30)

    assert len(candidates) == 1
    assert candidates[0]["id"] == t2.id


@pytest.mark.integration
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

    # Verify compacted_at was set
    row = manager.db.fetchone("SELECT compacted_at FROM tasks WHERE id = %s", (t1.id,))
    assert row["compacted_at"] is not None


@pytest.mark.integration
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
