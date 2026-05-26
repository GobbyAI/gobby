"""Tests for seq_num auto-increment - Phase 2 of task renumbering.

These tests verify that new tasks receive sequential seq_num values
that auto-increment per project.
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
class TestSeqNumAutoIncrement:
    """Test seq_num auto-increment for new tasks."""

    def test_first_task_gets_seq_num_1(self, task_manager, project_id) -> None:
        """Test that the first task in a project gets seq_num = 1."""
        task = task_manager.create_task(
            project_id=project_id,
            title="First Task",
        )

        assert task.seq_num == 1

    def test_sequential_tasks_get_incrementing_seq_nums(self, task_manager, project_id) -> None:
        """Test that sequential tasks get incrementing seq_num values."""
        task1 = task_manager.create_task(project_id=project_id, title="Task 1")
        task2 = task_manager.create_task(project_id=project_id, title="Task 2")
        task3 = task_manager.create_task(project_id=project_id, title="Task 3")

        assert task1.seq_num == 1
        assert task2.seq_num == 2
        assert task3.seq_num == 3

    def test_child_task_gets_next_seq_num(self, task_manager, project_id) -> None:
        """Test that child tasks also get incrementing seq_nums."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id,
            title="Child",
            parent_task_id=parent.id,
        )

        assert parent.seq_num == 1
        assert child.seq_num == 2

    def test_seq_num_unique_per_project(self, task_manager, temp_db) -> None:
        """Test that each project maintains its own seq_num sequence."""
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
        task_b2 = task_manager.create_task(project_id="proj-b", title="B2")

        # Each project should have independent sequence
        assert task_a1.seq_num == 1
        assert task_a2.seq_num == 2
        assert task_b1.seq_num == 1  # Independent from project A
        assert task_b2.seq_num == 2

    def test_seq_num_gaps_after_deletion(self, task_manager, project_id) -> None:
        """Test that seq_nums have gaps after deletion (stable references)."""
        task1 = task_manager.create_task(project_id=project_id, title="Task 1")
        task2 = task_manager.create_task(project_id=project_id, title="Task 2")
        task3 = task_manager.create_task(project_id=project_id, title="Task 3")

        # Delete task 2
        task_manager.delete_task(task2.id)

        # Create a new task
        task4 = task_manager.create_task(project_id=project_id, title="Task 4")

        # seq_num 2 should be skipped (gap preserved)
        assert task1.seq_num == 1
        assert task3.seq_num == 3
        assert task4.seq_num == 4  # Not 2

    def test_seq_num_continues_after_gap(self, task_manager, project_id, temp_db) -> None:
        """Test that seq_num continues from max even with gaps."""
        # Create tasks with existing seq_nums (simulating partial migration)
        task_manager.create_task(project_id=project_id, title="Task 1")
        task2 = task_manager.create_task(project_id=project_id, title="Task 2")

        # Manually create a gap by setting a high seq_num
        temp_db.execute(
            "UPDATE tasks SET seq_num = ? WHERE id = ?",
            (100, task2.id),
        )

        # New task should continue from max
        task3 = task_manager.create_task(project_id=project_id, title="Task 3")
        assert task3.seq_num == 101

    def test_seq_num_stored_and_retrieved(self, task_manager, project_id) -> None:
        """Test that seq_num is properly stored and retrieved."""
        task = task_manager.create_task(project_id=project_id, title="Test Task")
        original_seq = task.seq_num

        # Retrieve the task
        retrieved = task_manager.get_task(task.id)
        assert retrieved.seq_num == original_seq

    def test_seq_num_in_to_dict(self, task_manager, project_id) -> None:
        """Test that seq_num is included in to_dict() output."""
        task = task_manager.create_task(project_id=project_id, title="Test Task")
        data = task.to_dict()

        assert "seq_num" in data
        assert data["seq_num"] == task.seq_num

    def test_seq_num_in_to_brief(self, task_manager, project_id) -> None:
        """Test that seq_num is included in to_brief() output."""
        task = task_manager.create_task(project_id=project_id, title="Test Task")
        brief = task.to_brief()

        assert "seq_num" in brief
        assert brief["seq_num"] == task.seq_num

    def test_many_tasks_sequential_seq_nums(self, task_manager, project_id) -> None:
        """Test seq_num assignment for many tasks."""
        tasks = []
        for i in range(20):
            task = task_manager.create_task(
                project_id=project_id,
                title=f"Task {i + 1}",
            )
            tasks.append(task)

        # All should have sequential seq_nums
        for i, task in enumerate(tasks):
            assert task.seq_num == i + 1, f"Task {i + 1} has wrong seq_num"
