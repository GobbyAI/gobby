"""Integration tests for complete ID generation flow.

These tests verify the full task ID generation system:
- UUID generation for task IDs
- Project-scoped seq_num auto-increment
- Hierarchical path_cache computation
- Interaction between create, reparent, and delete operations
"""

from collections.abc import Iterator
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def task_manager(temp_db):
    """Create a LocalTaskManager with a temporary database."""
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    """Get the project ID from the sample project fixture."""
    return sample_project["id"]


@pytest.mark.integration
class TestCompleteIDGenerationFlow:
    """Integration tests for the complete ID generation flow."""

    def test_full_task_lifecycle(self, task_manager, project_id, temp_db) -> None:
        """Test complete lifecycle: create -> verify ID/seq/path -> update -> close."""
        session = SessionManager(temp_db).register(
            external_id="id-flow-ext",
            machine_id="21000000-0000-4000-8000-000000000002",
            source="codex",
            project_id=project_id,
        )

        # Create a task
        task = task_manager.create_task(
            project_id=project_id,
            title="Test Task",
            description="Integration test task",
            validation_criteria="Test task completion is observable.",
        )

        # Verify ID is a valid UUID
        assert "-" in task.id
        parts = task.id.split("-")
        assert len(parts) == 5, f"Expected 5 UUID parts, got {len(parts)}"

        # Verify seq_num is assigned
        assert task.seq_num == 1

        # Verify path_cache is computed
        assert task.path_cache == "1"

        # Verify all values are persisted
        retrieved = task_manager.get_task(task.id)
        assert retrieved.id == task.id
        assert retrieved.seq_num == task.seq_num
        assert retrieved.path_cache == task.path_cache

        # Claim task
        updated = task_manager.claim_task(task.id, session.id)
        assert updated.claimed_by_session_id == session.id
        # ID, seq_num, path_cache should be unchanged
        assert updated.id == task.id
        assert updated.seq_num == task.seq_num
        assert updated.path_cache == task.path_cache

    def test_multiple_projects_independent_sequences(self, task_manager, temp_db) -> None:
        """Test that each project has independent ID sequences."""
        alpha_project_id = str(uuid4())
        beta_project_id = str(uuid4())
        # Create two projects
        temp_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())",
            (alpha_project_id, "Alpha Project"),
        )
        temp_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())",
            (beta_project_id, "Beta Project"),
        )

        # Create tasks in each project
        alpha_task1 = task_manager.create_task(
            project_id=alpha_project_id,
            title="Alpha 1",
            validation_criteria="Test task completion is observable.",
        )
        alpha_task2 = task_manager.create_task(
            project_id=alpha_project_id,
            title="Alpha 2",
            validation_criteria="Test task completion is observable.",
        )
        beta_task1 = task_manager.create_task(
            project_id=beta_project_id,
            title="Beta 1",
            validation_criteria="Test task completion is observable.",
        )
        beta_task2 = task_manager.create_task(
            project_id=beta_project_id,
            title="Beta 2",
            validation_criteria="Test task completion is observable.",
        )

        # Each project has independent seq_num sequences
        assert alpha_task1.seq_num == 1
        assert alpha_task2.seq_num == 2
        assert beta_task1.seq_num == 1  # Independent from alpha
        assert beta_task2.seq_num == 2

        # All task IDs are unique UUIDs
        all_ids = {alpha_task1.id, alpha_task2.id, beta_task1.id, beta_task2.id}
        assert len(all_ids) == 4, "All task IDs should be unique"

    def test_nested_task_hierarchy(self, task_manager, project_id) -> None:
        """Test creating a nested task hierarchy with proper paths."""
        # Create hierarchy:
        #   root (seq=1, path=1)
        #     child1 (seq=2, path=1.2)
        #       grandchild1 (seq=3, path=1.2.3)
        #       grandchild2 (seq=4, path=1.2.4)
        #     child2 (seq=5, path=1.5)
        #   root2 (seq=6, path=6)

        root = task_manager.create_task(
            project_id=project_id,
            title="Root",
            validation_criteria="Test task completion is observable.",
        )
        child1 = task_manager.create_task(
            project_id=project_id,
            title="Child 1",
            parent_task_id=root.id,
            validation_criteria="Test task completion is observable.",
        )
        grandchild1 = task_manager.create_task(
            project_id=project_id,
            title="Grandchild 1",
            parent_task_id=child1.id,
            validation_criteria="Test task completion is observable.",
        )
        grandchild2 = task_manager.create_task(
            project_id=project_id,
            title="Grandchild 2",
            parent_task_id=child1.id,
            validation_criteria="Test task completion is observable.",
        )
        child2 = task_manager.create_task(
            project_id=project_id,
            title="Child 2",
            parent_task_id=root.id,
            validation_criteria="Test task completion is observable.",
        )
        root2 = task_manager.create_task(
            project_id=project_id,
            title="Root 2",
            validation_criteria="Test task completion is observable.",
        )

        # Verify seq_nums are sequential (flat, not hierarchical)
        assert root.seq_num == 1
        assert child1.seq_num == 2
        assert grandchild1.seq_num == 3
        assert grandchild2.seq_num == 4
        assert child2.seq_num == 5
        assert root2.seq_num == 6

        # Verify paths reflect hierarchy
        assert root.path_cache == "1"
        assert child1.path_cache == "1.2"
        assert grandchild1.path_cache == "1.2.3"
        assert grandchild2.path_cache == "1.2.4"
        assert child2.path_cache == "1.5"
        assert root2.path_cache == "6"

    def test_reparent_updates_subtree_paths(self, task_manager, project_id) -> None:
        """Test that reparenting a task updates all descendant paths."""
        # Create initial hierarchy under root1
        root1 = task_manager.create_task(
            project_id=project_id,
            title="Root 1",
            validation_criteria="Test task completion is observable.",
        )
        root2 = task_manager.create_task(
            project_id=project_id,
            title="Root 2",
            validation_criteria="Test task completion is observable.",
        )
        branch = task_manager.create_task(
            project_id=project_id,
            title="Branch",
            parent_task_id=root1.id,
            validation_criteria="Test task completion is observable.",
        )
        leaf = task_manager.create_task(
            project_id=project_id,
            title="Leaf",
            parent_task_id=branch.id,
            validation_criteria="Test task completion is observable.",
        )

        # Initial paths
        assert branch.path_cache == "1.3"
        assert leaf.path_cache == "1.3.4"

        # Reparent branch (and leaf) under root2
        task_manager.update_task(branch.id, parent_task_id=root2.id)

        # Verify both branch and leaf paths are updated
        branch = task_manager.get_task(branch.id)
        leaf = task_manager.get_task(leaf.id)

        assert branch.path_cache == "2.3"
        assert leaf.path_cache == "2.3.4"

    def test_reparent_to_root(self, task_manager, project_id) -> None:
        """Test reparenting a task to become a root task."""
        parent = task_manager.create_task(
            project_id=project_id,
            title="Parent",
            validation_criteria="Test task completion is observable.",
        )
        child = task_manager.create_task(
            project_id=project_id,
            title="Child",
            parent_task_id=parent.id,
            validation_criteria="Test task completion is observable.",
        )
        grandchild = task_manager.create_task(
            project_id=project_id,
            title="Grandchild",
            parent_task_id=child.id,
            validation_criteria="Test task completion is observable.",
        )

        # Initial paths
        assert child.path_cache == "1.2"
        assert grandchild.path_cache == "1.2.3"

        # Make child a root task
        task_manager.update_task(child.id, parent_task_id=None)

        # Verify paths updated
        child = task_manager.get_task(child.id)
        grandchild = task_manager.get_task(grandchild.id)

        assert child.path_cache == "2"
        assert grandchild.path_cache == "2.3"

    def test_seq_num_gaps_preserved(self, task_manager, project_id) -> None:
        """Test that seq_num gaps are preserved after deletion."""
        task1 = task_manager.create_task(
            project_id=project_id,
            title="Task 1",
            validation_criteria="Test task completion is observable.",
        )
        task2 = task_manager.create_task(
            project_id=project_id,
            title="Task 2",
            validation_criteria="Test task completion is observable.",
        )
        task3 = task_manager.create_task(
            project_id=project_id,
            title="Task 3",
            validation_criteria="Test task completion is observable.",
        )

        assert task1.seq_num == 1
        assert task2.seq_num == 2
        assert task3.seq_num == 3

        # Delete task2
        task_manager.delete_task(task2.id)

        # Create a new task - should get seq_num 4, not 2
        task4 = task_manager.create_task(
            project_id=project_id,
            title="Task 4",
            validation_criteria="Test task completion is observable.",
        )
        assert task4.seq_num == 4

    def test_deep_hierarchy(self, task_manager, project_id) -> None:
        """Test creating and reparenting a deep task hierarchy."""
        tasks = []
        parent_id = None

        # Create 10-deep hierarchy
        for i in range(10):
            task = task_manager.create_task(
                project_id=project_id,
                title=f"Level {i}",
                parent_task_id=parent_id,
                validation_criteria="Test task completion is observable.",
            )
            tasks.append(task)
            parent_id = task.id

        # Verify paths are correctly computed for deep hierarchy
        assert tasks[0].path_cache == "1"
        assert tasks[1].path_cache == "1.2"
        assert tasks[9].path_cache == "1.2.3.4.5.6.7.8.9.10"

    def test_to_dict_and_to_brief_include_all_fields(self, task_manager, project_id) -> None:
        """Test that to_dict and to_brief include ID, seq_num, and path_cache."""
        parent = task_manager.create_task(
            project_id=project_id,
            title="Parent",
            validation_criteria="Test task completion is observable.",
        )
        child = task_manager.create_task(
            project_id=project_id,
            title="Child",
            parent_task_id=parent.id,
            validation_criteria="Test task completion is observable.",
        )

        # to_dict
        dict_data = child.to_dict()
        assert "id" in dict_data
        assert "-" in dict_data["id"]  # UUID format
        assert "seq_num" in dict_data
        assert dict_data["seq_num"] == 2
        assert "path_cache" in dict_data
        assert dict_data["path_cache"] == "1.2"

        # to_brief
        brief_data = child.to_brief()
        assert "id" in brief_data
        assert "-" in brief_data["id"]  # UUID format
        assert "seq_num" in brief_data
        assert brief_data["seq_num"] == 2
        assert "path_cache" in brief_data
        assert brief_data["path_cache"] == "1.2"

    def test_many_tasks_sequential_ids(self, task_manager, project_id) -> None:
        """Test creating many tasks with sequential IDs."""
        tasks = []
        for i in range(50):
            task = task_manager.create_task(
                project_id=project_id,
                title=f"Task {i + 1}",
                validation_criteria="Test task completion is observable.",
            )
            tasks.append(task)

        # Verify all seq_nums are sequential
        for i, task in enumerate(tasks):
            assert task.seq_num == i + 1
            assert task.path_cache == str(i + 1)

        # Verify all IDs are unique UUIDs
        ids = {task.id for task in tasks}
        assert len(ids) == 50

    def test_uuid_format_valid(self, task_manager, project_id) -> None:
        """Test that generated task IDs are valid UUIDs."""
        import uuid

        task = task_manager.create_task(
            project_id=project_id,
            title="UUID Test",
            validation_criteria="Test task completion is observable.",
        )

        # Should be parseable as UUID
        parsed = uuid.UUID(task.id)
        assert str(parsed) == task.id

        # Should be UUID version 4
        assert parsed.version == 4
