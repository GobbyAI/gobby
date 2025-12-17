from unittest.mock import patch

import pytest

from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, TaskIDCollisionError


@pytest.fixture
def dep_manager(temp_db):
    return TaskDependencyManager(temp_db)


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    return sample_project["id"]


class TestLocalTaskManager:
    def test_create_task(self, task_manager, project_id):
        task = task_manager.create_task(
            project_id=project_id,
            title="Fix bug",
            description="Fix the critical bug",
            priority=1,
            task_type="bug",
            labels=["urgent", "backend"],
        )

        assert task.title == "Fix bug"
        assert task.project_id == project_id
        assert task.status == "open"
        assert task.labels == ["urgent", "backend"]
        assert task.priority == 1
        assert task.task_type == "bug"
        assert task.id.startswith("gt-")
        assert task.created_at is not None
        assert task.updated_at is not None

    def test_get_task(self, task_manager, project_id):
        created = task_manager.create_task(project_id=project_id, title="Find me")
        fetched = task_manager.get_task(created.id)
        assert fetched == created

    def test_update_task(self, task_manager, project_id):
        task = task_manager.create_task(project_id=project_id, title="Original Title")
        updated = task_manager.update_task(task.id, title="New Title", status="in_progress")
        assert updated.title == "New Title"
        assert updated.status == "in_progress"
        assert updated.updated_at > task.updated_at

    def test_close_task(self, task_manager, project_id):
        task = task_manager.create_task(project_id=project_id, title="To Close")
        closed = task_manager.close_task(task.id, reason="Done")
        assert closed.status == "closed"
        assert closed.closed_reason == "Done"

    def test_delete_task(self, task_manager, project_id):
        task = task_manager.create_task(project_id=project_id, title="To Delete")
        task_manager.delete_task(task.id)

        with pytest.raises(ValueError, match="not found"):
            task_manager.get_task(task.id)

    def test_list_tasks(self, task_manager, project_id):
        t1 = task_manager.create_task(project_id=project_id, title="Task 1", priority=1)
        _ = task_manager.create_task(project_id=project_id, title="Task 2", priority=2)

        tasks = task_manager.list_tasks(project_id=project_id)
        assert len(tasks) == 2

        # Test filtering
        tasks_p1 = task_manager.list_tasks(project_id=project_id, priority=1)
        assert len(tasks_p1) == 1
        assert tasks_p1[0].id == t1.id

    def test_id_collision_retry(self, task_manager, project_id):
        # Create a task to occupy an ID
        existing_task = task_manager.create_task(project_id=project_id, title="Existing")

        # Mock generate_task_id to return existing ID once, then a new one
        with patch(
            "gobby.storage.tasks.generate_task_id", side_effect=[existing_task.id, "gt-newunique"]
        ) as mock_gen:
            new_task = task_manager.create_task(project_id=project_id, title="New Task")
            assert new_task.id == "gt-newunique"
            # Should have called it twice (initial attempt + retry)
            # Actually create_task calls generate_task_id in a loop, passing salt.
            # Side_effect replaces the return value of ALL calls.
            # We assume logic calls generate_task_id.
            assert mock_gen.call_count == 2

    def test_id_collision_failure(self, task_manager, project_id):
        existing_task = task_manager.create_task(project_id=project_id, title="Existing")

        # Mock to always return existing ID
        with patch("gobby.storage.tasks.generate_task_id", return_value=existing_task.id):
            with pytest.raises(TaskIDCollisionError):
                task_manager.create_task(project_id=project_id, title="Doom")

    def test_delete_with_children_fails_without_cascade(self, task_manager, project_id):
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        _ = task_manager.create_task(project_id=project_id, title="Child", parent_task_id=parent.id)

        with pytest.raises(ValueError, match="has children"):
            task_manager.delete_task(parent.id)

    def test_delete_with_cascade(self, task_manager, project_id):
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )

        task_manager.delete_task(parent.id, cascade=True)

        with pytest.raises(ValueError):
            task_manager.get_task(parent.id)
        with pytest.raises(ValueError):
            task_manager.get_task(child.id)

    def test_list_ready_tasks(self, task_manager, dep_manager, project_id):
        # T1 -> T2 (blocks)
        t1 = task_manager.create_task(project_id, "T1", priority=2)
        t2 = task_manager.create_task(project_id, "T2", priority=1)

        dep_manager.add_dependency(t1.id, t2.id, "blocks")

        # T3, independent
        t3 = task_manager.create_task(project_id, "T3", priority=2)

        # ready tasks: T2, T3. T1 is blocked.
        ready = task_manager.list_ready_tasks(project_id=project_id)
        ids = {t.id for t in ready}
        assert len(ready) == 2
        assert t2.id in ids
        assert t3.id in ids
        assert t1.id not in ids

        # Check sorting: priority ASC (1 before 2). So T2 first (priority 1)
        # Note: t3 is priority 2.
        assert ready[0].id == t2.id

        # Close T2. Now T1 should be ready?
        task_manager.close_task(t2.id)
        ready = task_manager.list_ready_tasks(project_id=project_id)
        ids = {t.id for t in ready}
        assert len(ready) == 2  # T1 and T3
        assert t1.id in ids
        assert t3.id in ids

    def test_list_blocked_tasks(self, task_manager, dep_manager, project_id):
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")

        dep_manager.add_dependency(t1.id, t2.id, "blocks")

        blocked = task_manager.list_blocked_tasks(project_id=project_id)
        assert len(blocked) == 1
        assert blocked[0].id == t1.id

        task_manager.close_task(t2.id)
        blocked = task_manager.list_blocked_tasks(project_id=project_id)
        # T1 is no longer blocked by OPEN task
        assert len(blocked) == 0
