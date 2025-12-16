import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gobby.sync.tasks import TaskSyncManager
from gobby.storage.tasks import LocalTaskManager


@pytest.fixture
def sync_manager(temp_db, tmp_path):
    export_path = tmp_path / ".gobby" / "tasks.jsonl"
    return TaskSyncManager(temp_db, str(export_path))


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


class TestTaskSyncManager:
    def test_export_to_jsonl(self, sync_manager, task_manager, sample_project):
        # Create tasks
        t1 = task_manager.create_task(sample_project["id"], "Task 1")
        t2 = task_manager.create_task(sample_project["id"], "Task 2")

        # Add dependency: Task 2 depends on Task 1
        # task_id = t2.id (the one with dependency), depends_on = t1.id (the dependency)
        # Note: In schema, unique constraint includes dep_type
        now = "2023-01-01T00:00:00"
        sync_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (t2.id, t1.id, "blocking", now),
        )

        sync_manager.export_to_jsonl()

        assert sync_manager.export_path.exists()

        lines = sync_manager.export_path.read_text().strip().split("\n")
        assert len(lines) == 2

        data = [json.loads(line) for line in lines]

        # Verify Task 1
        task1_data = next(d for d in data if d["id"] == t1.id)
        assert task1_data["title"] == "Task 1"
        assert task1_data["deps_on"] == []

        # Verify Task 2
        task2_data = next(d for d in data if d["id"] == t2.id)
        assert task2_data["title"] == "Task 2"
        assert task2_data["deps_on"] == [t1.id]

    def test_trigger_export_debounced(self, sync_manager):
        # Mock export_to_jsonl
        # We need to patch the method on the instance or class
        # Using a safer approach with a mock side_effect check in a real scenario would be better,
        # but for threading, we just want to ensure it runs eventually.

        # Reduce interval for test
        sync_manager._debounce_interval = 0.1

        with patch.object(sync_manager, "export_to_jsonl") as mock_export:
            sync_manager.trigger_export()
            sync_manager.trigger_export()
            sync_manager.trigger_export()

            assert mock_export.call_count == 0

            time.sleep(0.2)

            assert mock_export.call_count == 1

        sync_manager.stop()

    def test_mutation_triggers_export(self, task_manager, tmp_path, sample_project):
        """Test that task mutations trigger export."""
        export_path = tmp_path / "tasks.jsonl"
        sync_manager = TaskSyncManager(task_manager.db, str(export_path))

        # Mock trigger_export to verify call
        sync_manager.trigger_export = MagicMock()

        # Wire up listener
        task_manager.add_change_listener(sync_manager.trigger_export)

        # Create task -> should trigger
        task = task_manager.create_task(sample_project["id"], "Task 1")
        assert sync_manager.trigger_export.call_count == 1

        # Update task -> should trigger
        task_manager.update_task(task.id, title="Updated Task 1")
        assert sync_manager.trigger_export.call_count == 2

        # Close task -> should trigger
        task_manager.close_task(task.id)
        assert sync_manager.trigger_export.call_count == 3

        # Delete task -> should trigger
        task_manager.delete_task(task.id)
        assert sync_manager.trigger_export.call_count == 4
