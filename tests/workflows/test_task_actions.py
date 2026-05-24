"""Tests for workflow-task integration module."""

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.task_actions import (
    update_task_from_workflow,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase):
    """Create a test database with migrations."""
    database = temp_db
    return database


@pytest.fixture
def project_id(db):
    """Create a test project and return its ID."""
    project_manager = LocalProjectManager(db)
    project = project_manager.create(
        name="test-project",
        repo_path="/tmp/test",
    )
    return project.id


class TestUpdateTaskFromWorkflow:
    """Tests for update_task_from_workflow function."""

    def test_update_validation_status(self, db, project_id) -> None:
        """Test updating validation status."""
        task_manager = LocalTaskManager(db)
        task = task_manager.create_task(
            project_id=project_id,
            title="Test task",
        )

        updated = update_task_from_workflow(
            db=db,
            task_id=task.id,
            validation_status="valid",
            validation_feedback="All tests passed",
        )

        assert updated is not None
        assert updated.validation_status == "valid"
        assert updated.validation_feedback == "All tests passed"

    def test_update_nonexistent_task(self, db, project_id) -> None:
        """Test updating a task that doesn't exist."""
        result = update_task_from_workflow(
            db=db,
            task_id="gt-nonexistent",
            validation_status="invalid",
        )

        assert result is None

    def test_update_no_changes(self, db, project_id) -> None:
        """Test update with no fields returns current task."""
        task_manager = LocalTaskManager(db)
        task = task_manager.create_task(
            project_id=project_id,
            title="Test task",
        )

        result = update_task_from_workflow(
            db=db,
            task_id=task.id,
        )

        assert result is not None
        assert result.id == task.id
