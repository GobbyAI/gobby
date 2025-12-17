"""Tests for task labels."""

import pytest
from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager


@pytest.fixture
def manager(temp_db):
    return LocalTaskManager(temp_db)


def test_add_label(manager, sample_project):
    """Test adding labels."""
    proj_id = sample_project["id"]
    task = manager.create_task(proj_id, "Label Task")

    # Add new label
    updated = manager.add_label(task.id, "urgent")
    assert "urgent" in updated.labels

    # Add existing label (no-op)
    updated = manager.add_label(task.id, "urgent")
    assert len(updated.labels) == 1
    assert "urgent" in updated.labels

    # Add another label
    updated = manager.add_label(task.id, "frontend")
    assert len(updated.labels) == 2
    assert "urgent" in updated.labels
    assert "frontend" in updated.labels


def test_remove_label(manager, sample_project):
    """Test removing labels."""
    proj_id = sample_project["id"]
    task = manager.create_task(proj_id, "Label Task", labels=["urgent", "backend"])

    # Remove existing label
    updated = manager.remove_label(task.id, "urgent")
    assert "urgent" not in updated.labels
    assert "backend" in updated.labels

    # Remove non-existing label (no-op)
    updated = manager.remove_label(task.id, "urgent")
    assert len(updated.labels) == 1

    # Remove last label
    updated = manager.remove_label(task.id, "backend")
    assert len(updated.labels) == 0
