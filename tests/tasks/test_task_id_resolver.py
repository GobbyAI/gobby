"""Tests for task ID resolver utility.

These tests verify that the resolver correctly handles:
- `#N` format resolution to UUID
- Path format (e.g., "1.2.3") resolution
- UUID pass-through
- Error handling for invalid/non-existent references
- Unknown format errors for `gt-*` format (no longer special-cased)
"""

import pytest

from gobby.storage.tasks import LocalTaskManager, TaskNotFoundError

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    """Create a LocalTaskManager with a temporary database."""
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    """Get the project ID from the sample project fixture."""
    return sample_project["id"]


@pytest.mark.integration
class TestTaskIdResolver:
    """Tests for resolve_task_reference function."""

    def test_resolve_seq_num_format(self, task_manager, project_id) -> None:
        """Test resolving #N format to UUID."""
        task = task_manager.create_task(project_id=project_id, title="Test Task")

        resolved = task_manager.resolve_task_reference("#1", project_id)
        assert resolved == task.id

    def test_resolve_seq_num_multiple_tasks(self, task_manager, project_id) -> None:
        """Test resolving #N with multiple tasks."""
        task1 = task_manager.create_task(project_id=project_id, title="Task 1")
        task2 = task_manager.create_task(project_id=project_id, title="Task 2")
        task3 = task_manager.create_task(project_id=project_id, title="Task 3")

        assert task_manager.resolve_task_reference("#1", project_id) == task1.id
        assert task_manager.resolve_task_reference("#2", project_id) == task2.id
        assert task_manager.resolve_task_reference("#3", project_id) == task3.id

    def test_resolve_high_seq_num(self, task_manager, project_id) -> None:
        """Test resolving high seq_num like #123."""
        # Create 123 tasks to get to #123
        for i in range(123):
            task = task_manager.create_task(project_id=project_id, title=f"Task {i + 1}")

        # The 123rd task should be resolvable as #123
        resolved = task_manager.resolve_task_reference("#123", project_id)
        assert resolved == task.id

    def test_resolve_invalid_seq_num_zero(self, task_manager, project_id) -> None:
        """Test that #0 raises an error (seq_num starts at 1)."""
        task_manager.create_task(project_id=project_id, title="Test Task")

        with pytest.raises(TaskNotFoundError):
            task_manager.resolve_task_reference("#0", project_id)

    def test_resolve_nonexistent_seq_num(self, task_manager, project_id) -> None:
        """Test that non-existent #999 raises an error."""
        task_manager.create_task(project_id=project_id, title="Test Task")

        with pytest.raises(TaskNotFoundError):
            task_manager.resolve_task_reference("#999", project_id)

    def test_resolve_uuid_passthrough(self, task_manager, project_id) -> None:
        """Test that valid UUID passes through unchanged."""
        task = task_manager.create_task(project_id=project_id, title="Test Task")

        resolved = task_manager.resolve_task_reference(task.id, project_id)
        assert resolved == task.id

    def test_resolve_uuid_validates_exists(self, task_manager, project_id) -> None:
        """Test that UUID is validated to exist."""
        task_manager.create_task(project_id=project_id, title="Test Task")

        with pytest.raises(TaskNotFoundError):
            task_manager.resolve_task_reference("00000000-0000-0000-0000-000000000000", project_id)

    def test_resolve_path_format(self, task_manager, project_id) -> None:
        """Test resolving path format like 1.2.3."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )

        # Child should have path "1.2"
        resolved = task_manager.resolve_task_reference("1.2", project_id)
        assert resolved == child.id

    def test_resolve_deep_path(self, task_manager, project_id) -> None:
        """Test resolving deep path like 1.2.3.4."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )
        grandchild = task_manager.create_task(
            project_id=project_id, title="Grandchild", parent_task_id=child.id
        )

        resolved = task_manager.resolve_task_reference("1.2.3", project_id)
        assert resolved == grandchild.id

    def test_resolve_path_nonexistent(self, task_manager, project_id) -> None:
        """Test that non-existent path raises error."""
        task_manager.create_task(project_id=project_id, title="Test Task")

        with pytest.raises(TaskNotFoundError):
            task_manager.resolve_task_reference("1.2.3", project_id)

    def test_resolve_gt_format_raises_unknown_format_error(self, task_manager, project_id) -> None:
        """Test that gt-* format raises an 'unknown format' error (no longer special-cased)."""
        task_manager.create_task(project_id=project_id, title="Test Task")

        with pytest.raises(TaskNotFoundError, match="Unknown task reference format"):
            task_manager.resolve_task_reference("gt-abc123", project_id)

    def test_resolve_invalid_format(self, task_manager, project_id) -> None:
        """Test that completely invalid format raises error."""
        task_manager.create_task(project_id=project_id, title="Test Task")

        with pytest.raises((ValueError, TaskNotFoundError)):
            task_manager.resolve_task_reference("invalid-format", project_id)

    def test_resolve_empty_string(self, task_manager, project_id) -> None:
        """Test that empty string raises error."""
        with pytest.raises((ValueError, TaskNotFoundError)):
            task_manager.resolve_task_reference("", project_id)

    def test_resolve_respects_project_scope(self, task_manager, temp_db) -> None:
        """Test that #N resolution is project-scoped."""
        # Create two projects
        temp_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())",
            ("proj-a", "Project A"),
        )
        temp_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())",
            ("proj-b", "Project B"),
        )

        # Create tasks in each project
        task_a = task_manager.create_task(project_id="proj-a", title="Task A")
        task_b = task_manager.create_task(project_id="proj-b", title="Task B")

        # #1 in proj-a should resolve to task_a
        assert task_manager.resolve_task_reference("#1", "proj-a") == task_a.id

        # #1 in proj-b should resolve to task_b (different task!)
        assert task_manager.resolve_task_reference("#1", "proj-b") == task_b.id

    def test_resolve_after_deletion_gap(self, task_manager, project_id) -> None:
        """Test resolution after creating a gap via deletion."""
        task1 = task_manager.create_task(project_id=project_id, title="Task 1")
        task2 = task_manager.create_task(project_id=project_id, title="Task 2")
        task3 = task_manager.create_task(project_id=project_id, title="Task 3")

        # Delete task2, creating a gap
        task_manager.delete_task(task2.id)

        # #1 and #3 should still resolve correctly
        assert task_manager.resolve_task_reference("#1", project_id) == task1.id
        assert task_manager.resolve_task_reference("#3", project_id) == task3.id

        # #2 should now fail (deleted)
        with pytest.raises(TaskNotFoundError):
            task_manager.resolve_task_reference("#2", project_id)
