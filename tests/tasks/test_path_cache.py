"""Tests for path_cache auto-computation on task insert and reparent.

These tests verify that:
1. When a new task is created, its path_cache is computed and stored automatically
2. When a task is reparented, its path_cache and all descendant paths are updated
3. The path_cache correctly reflects the task's hierarchy
"""

import pytest

from gobby.storage.tasks import LocalTaskManager

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
class TestPathCacheOnInsert:
    """Test path_cache is computed and stored when tasks are inserted."""

    def test_root_task_gets_path_cache_on_insert(self, task_manager, project_id) -> None:
        """Test that a root task gets path_cache computed immediately on insert."""
        task = task_manager.create_task(project_id=project_id, title="Root Task")

        # Path should be the task's seq_num (which is 1 for first task)
        assert task.path_cache == "1"

    def test_second_root_task_gets_correct_path(self, task_manager, project_id) -> None:
        """Test second root task gets its seq_num as path."""
        task1 = task_manager.create_task(project_id=project_id, title="Task 1")
        task2 = task_manager.create_task(project_id=project_id, title="Task 2")

        assert task1.path_cache == "1"
        assert task2.path_cache == "2"

    def test_child_task_gets_hierarchical_path(self, task_manager, project_id) -> None:
        """Test that a child task gets parent.child path on insert."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )

        # Parent is seq 1, child is seq 2
        assert parent.path_cache == "1"
        assert child.path_cache == "1.2"

    def test_grandchild_task_gets_deep_path(self, task_manager, project_id) -> None:
        """Test path computation for deeply nested tasks."""
        root = task_manager.create_task(project_id=project_id, title="Root")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=root.id
        )
        grandchild = task_manager.create_task(
            project_id=project_id, title="Grandchild", parent_task_id=child.id
        )

        assert root.path_cache == "1"
        assert child.path_cache == "1.2"
        assert grandchild.path_cache == "1.2.3"

    def test_sibling_tasks_have_distinct_paths(self, task_manager, project_id) -> None:
        """Test that sibling tasks have distinct paths."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child1 = task_manager.create_task(
            project_id=project_id, title="Child 1", parent_task_id=parent.id
        )
        child2 = task_manager.create_task(
            project_id=project_id, title="Child 2", parent_task_id=parent.id
        )

        assert parent.path_cache == "1"
        assert child1.path_cache == "1.2"
        assert child2.path_cache == "1.3"

    def test_path_cache_preserved_in_database(self, task_manager, project_id, temp_db) -> None:
        """Test that path_cache is stored in database and retrievable."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        task_id = task.id

        # Retrieve directly from database
        row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (task_id,))
        assert row["path_cache"] == "1"

        # Retrieve via get_task
        retrieved = task_manager.get_task(task_id)
        assert retrieved.path_cache == "1"

    def test_path_cache_in_to_dict(self, task_manager, project_id) -> None:
        """Test that path_cache is included in to_dict() output."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        data = task.to_dict()

        assert "path_cache" in data
        assert data["path_cache"] == "1"

    def test_path_cache_in_to_brief(self, task_manager, project_id) -> None:
        """Test that path_cache is included in to_brief() output."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        brief = task.to_brief()

        assert "path_cache" in brief
        assert brief["path_cache"] == "1"

    def test_complex_hierarchy_paths(self, task_manager, project_id) -> None:
        """Test path computation in a complex hierarchy."""
        # Create:
        #   task1 (seq 1)
        #   task2 (seq 2)
        #     task2a (seq 3)
        #     task2b (seq 4)
        #       task2b1 (seq 5)
        #   task3 (seq 6)

        task1 = task_manager.create_task(project_id=project_id, title="Task 1")
        task2 = task_manager.create_task(project_id=project_id, title="Task 2")
        task2a = task_manager.create_task(
            project_id=project_id, title="Task 2a", parent_task_id=task2.id
        )
        task2b = task_manager.create_task(
            project_id=project_id, title="Task 2b", parent_task_id=task2.id
        )
        task2b1 = task_manager.create_task(
            project_id=project_id, title="Task 2b1", parent_task_id=task2b.id
        )
        task3 = task_manager.create_task(project_id=project_id, title="Task 3")

        assert task1.path_cache == "1"
        assert task2.path_cache == "2"
        assert task2a.path_cache == "2.3"
        assert task2b.path_cache == "2.4"
        assert task2b1.path_cache == "2.4.5"
        assert task3.path_cache == "6"

    def test_multiple_projects_independent_paths(self, task_manager, temp_db) -> None:
        """Test that each project has independent path sequences."""
        # Create two projects
        temp_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, NOW(), NOW())",
            ("proj-a", "Project A"),
        )
        temp_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, NOW(), NOW())",
            ("proj-b", "Project B"),
        )

        # Create tasks in each project
        task_a1 = task_manager.create_task(project_id="proj-a", title="A1")
        task_a2 = task_manager.create_task(project_id="proj-a", title="A2")
        task_b1 = task_manager.create_task(project_id="proj-b", title="B1")

        # Each project starts fresh with seq 1
        assert task_a1.path_cache == "1"
        assert task_a2.path_cache == "2"
        assert task_b1.path_cache == "1"  # Independent from project A


@pytest.mark.integration
class TestPathCacheOnReparent:
    """Test path_cache updates when tasks are reparented."""

    def test_reparent_root_to_child(self, task_manager, project_id) -> None:
        """Test moving a root task to become a child of another task."""
        # Create two root tasks
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        orphan = task_manager.create_task(project_id=project_id, title="Orphan")

        # Initially both are root tasks
        assert parent.path_cache == "1"
        assert orphan.path_cache == "2"

        # Reparent orphan under parent
        task_manager.update_task(orphan.id, parent_task_id=parent.id)
        updated_orphan = task_manager.get_task(orphan.id)

        # Orphan should now have parent's path prefix
        assert updated_orphan.path_cache == "1.2"

    def test_reparent_child_to_root(self, task_manager, project_id) -> None:
        """Test moving a child task to become a root task."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )

        assert child.path_cache == "1.2"

        # Make child a root task
        task_manager.update_task(child.id, parent_task_id=None)
        updated_child = task_manager.get_task(child.id)

        # Child should now be a root-level path
        assert updated_child.path_cache == "2"

    def test_reparent_child_to_different_parent(self, task_manager, project_id) -> None:
        """Test moving a child from one parent to another."""
        parent1 = task_manager.create_task(project_id=project_id, title="Parent 1")
        parent2 = task_manager.create_task(project_id=project_id, title="Parent 2")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent1.id
        )

        assert child.path_cache == "1.3"

        # Move child to parent2
        task_manager.update_task(child.id, parent_task_id=parent2.id)
        updated_child = task_manager.get_task(child.id)

        # Child should now have parent2's prefix
        assert updated_child.path_cache == "2.3"

    def test_reparent_updates_descendants(self, task_manager, project_id) -> None:
        """Test that reparenting updates all descendant paths."""
        # Create hierarchy: parent1 -> child -> grandchild
        parent1 = task_manager.create_task(project_id=project_id, title="Parent 1")
        parent2 = task_manager.create_task(project_id=project_id, title="Parent 2")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent1.id
        )
        grandchild = task_manager.create_task(
            project_id=project_id, title="Grandchild", parent_task_id=child.id
        )

        # Initial paths
        assert child.path_cache == "1.3"
        assert grandchild.path_cache == "1.3.4"

        # Move child (with grandchild) to parent2
        task_manager.update_task(child.id, parent_task_id=parent2.id)

        # Both child and grandchild paths should be updated
        updated_child = task_manager.get_task(child.id)
        updated_grandchild = task_manager.get_task(grandchild.id)

        assert updated_child.path_cache == "2.3"
        assert updated_grandchild.path_cache == "2.3.4"

    def test_reparent_deep_subtree(self, task_manager, project_id) -> None:
        """Test reparenting a subtree with multiple levels."""
        # Create: root1 -> level1 -> level2 -> level3
        #         root2
        root1 = task_manager.create_task(project_id=project_id, title="Root 1")
        root2 = task_manager.create_task(project_id=project_id, title="Root 2")
        level1 = task_manager.create_task(
            project_id=project_id, title="Level 1", parent_task_id=root1.id
        )
        level2 = task_manager.create_task(
            project_id=project_id, title="Level 2", parent_task_id=level1.id
        )
        level3 = task_manager.create_task(
            project_id=project_id, title="Level 3", parent_task_id=level2.id
        )

        # Initial paths
        assert level1.path_cache == "1.3"
        assert level2.path_cache == "1.3.4"
        assert level3.path_cache == "1.3.4.5"

        # Move entire subtree (level1) under root2
        task_manager.update_task(level1.id, parent_task_id=root2.id)

        # All paths in subtree should be updated
        updated_level1 = task_manager.get_task(level1.id)
        updated_level2 = task_manager.get_task(level2.id)
        updated_level3 = task_manager.get_task(level3.id)

        assert updated_level1.path_cache == "2.3"
        assert updated_level2.path_cache == "2.3.4"
        assert updated_level3.path_cache == "2.3.4.5"

    def test_reparent_with_multiple_children(self, task_manager, project_id) -> None:
        """Test reparenting when the moved task has multiple children."""
        parent1 = task_manager.create_task(project_id=project_id, title="Parent 1")
        parent2 = task_manager.create_task(project_id=project_id, title="Parent 2")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent1.id
        )
        grandchild1 = task_manager.create_task(
            project_id=project_id, title="Grandchild 1", parent_task_id=child.id
        )
        grandchild2 = task_manager.create_task(
            project_id=project_id, title="Grandchild 2", parent_task_id=child.id
        )

        # Move child under parent2
        task_manager.update_task(child.id, parent_task_id=parent2.id)

        # All paths should be updated
        updated_child = task_manager.get_task(child.id)
        updated_gc1 = task_manager.get_task(grandchild1.id)
        updated_gc2 = task_manager.get_task(grandchild2.id)

        assert updated_child.path_cache == "2.3"
        assert updated_gc1.path_cache == "2.3.4"
        assert updated_gc2.path_cache == "2.3.5"

    def test_reparent_preserves_seq_num(self, task_manager, project_id) -> None:
        """Test that reparenting preserves the task's seq_num."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )

        original_seq = child.seq_num
        assert original_seq == 2

        # Reparent to root
        task_manager.update_task(child.id, parent_task_id=None)
        updated_child = task_manager.get_task(child.id)

        # seq_num should be unchanged
        assert updated_child.seq_num == original_seq
        # But path should just be the seq_num
        assert updated_child.path_cache == "2"

    def test_reparent_path_stored_in_database(self, task_manager, project_id, temp_db) -> None:
        """Test that reparent path changes are persisted to database."""
        parent1 = task_manager.create_task(project_id=project_id, title="Parent 1")
        parent2 = task_manager.create_task(project_id=project_id, title="Parent 2")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent1.id
        )

        # Verify initial path in DB
        row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (child.id,))
        assert row["path_cache"] == "1.3"

        # Reparent
        task_manager.update_task(child.id, parent_task_id=parent2.id)

        # Verify new path in DB
        row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (child.id,))
        assert row["path_cache"] == "2.3"
