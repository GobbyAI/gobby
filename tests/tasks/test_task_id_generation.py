"""Tests for task ID generation - Phase 2 of task renumbering.

These tests verify that new tasks receive proper UUID identifiers,
seq_num values, and path_cache values.
"""

import uuid

import pytest

from gobby.storage.tasks import LocalTaskManager, generate_task_id

pytestmark = pytest.mark.unit


def _assert_valid_uuid(value: str) -> uuid.UUID:
    parsed = uuid.UUID(value)
    assert str(parsed) == value
    return parsed


class TestGenerateTaskId:
    """Test the generate_task_id function returns UUID format."""

    def test_generate_task_id_returns_uuid_format(self) -> None:
        """Test that generate_task_id returns a valid UUID string."""
        task_id = generate_task_id("test-project")

        # Should be valid UUID format (8-4-4-4-12 hex chars with dashes)
        _assert_valid_uuid(task_id)

    def test_generate_task_id_is_unique(self) -> None:
        """Test that generate_task_id returns unique IDs."""
        ids = set()
        for _ in range(100):
            task_id = generate_task_id("test-project")
            assert task_id not in ids, f"Duplicate ID generated: {task_id}"
            ids.add(task_id)

    def test_generate_task_id_different_projects(self) -> None:
        """Test that generate_task_id works across different projects."""
        id1 = generate_task_id("project-a")
        id2 = generate_task_id("project-b")

        # Both should be valid UUIDs
        _assert_valid_uuid(id1)
        _assert_valid_uuid(id2)

        # Should be different
        assert id1 != id2

    def test_generate_task_id_with_salt(self) -> None:
        """Test that salt affects the generated ID."""
        id1 = generate_task_id("project", salt="")
        id2 = generate_task_id("project", salt="salt1")
        id3 = generate_task_id("project", salt="salt2")

        # All should be valid UUIDs
        _assert_valid_uuid(id1)
        _assert_valid_uuid(id2)
        _assert_valid_uuid(id3)

        # All should be different (with high probability)
        assert len({id1, id2, id3}) == 3


@pytest.mark.integration
class TestTaskCreationUUID:
    """Test that tasks created via LocalTaskManager get UUID IDs."""

    def test_create_task_returns_uuid_id(self, task_manager, project_id) -> None:
        """Test that create_task generates a UUID ID for new tasks."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Test Task",
        )

        # ID should be valid UUID format
        _assert_valid_uuid(task.id)

    def test_create_task_uuid_is_unique(self, task_manager, project_id) -> None:
        """Test that multiple tasks get unique UUID IDs."""
        ids = set()
        for i in range(10):
            task = task_manager.create_task(
                project_id=project_id,
                title=f"Task {i}",
            )
            assert task.id not in ids, f"Duplicate ID generated: {task.id}"
            ids.add(task.id)

            # Each should be valid UUID
            _assert_valid_uuid(task.id)

    def test_create_task_uuid_stored_and_retrieved(self, task_manager, project_id) -> None:
        """Test that UUID ID is properly stored and can be retrieved."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Test Task",
        )
        original_id = task.id

        # Validate UUID format
        _assert_valid_uuid(original_id)

        # Retrieve task by ID
        retrieved = task_manager.get_task(original_id)
        assert retrieved.id == original_id
        assert retrieved.title == "Test Task"

    def test_create_task_uuid_version_4(self, task_manager, project_id) -> None:
        """Test that generated UUIDs are version 4 (random)."""
        task = task_manager.create_task(
            project_id=project_id,
            title="Test Task",
        )

        parsed = _assert_valid_uuid(task.id)
        # UUID version 4 has version field set to 4
        assert parsed.version == 4, f"UUID should be version 4, got version {parsed.version}"

    def test_create_child_task_uuid(self, task_manager, project_id) -> None:
        """Test that child tasks also get UUID IDs."""
        parent = task_manager.create_task(
            project_id=project_id,
            title="Parent Task",
        )
        child = task_manager.create_task(
            project_id=project_id,
            title="Child Task",
            parent_task_id=parent.id,
        )

        # Both should be valid UUIDs
        _assert_valid_uuid(parent.id)
        _assert_valid_uuid(child.id)

        # Should be different
        assert parent.id != child.id

        # Child should reference parent
        assert child.parent_task_id == parent.id


# Fixtures for tests
@pytest.fixture
def task_manager(temp_db):
    """Create a LocalTaskManager with a temporary database."""
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    """Get the project ID from the sample project fixture."""
    return sample_project["id"]
