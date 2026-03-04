import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit


@pytest.fixture
def sync_manager(temp_db, tmp_path):
    export_path = tmp_path / ".gobby" / "tasks.jsonl"
    task_manager = LocalTaskManager(temp_db)
    manager = TaskSyncManager(task_manager, str(export_path))
    yield manager
    manager.stop()


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


class TestTaskSyncManager:
    @pytest.mark.integration
    @pytest.mark.slow
    def test_export_to_jsonl(self, sync_manager, task_manager, sample_project) -> None:
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

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_trigger_export_debounced(self, sync_manager):
        """Test that multiple rapid trigger_export calls result in single debounced export."""
        # Reduce interval for test
        sync_manager._debounce_interval = 0.1

        with patch.object(sync_manager, "export_to_jsonl") as mock_export:
            # Trigger multiple times in quick succession
            sync_manager.trigger_export()
            sync_manager.trigger_export()
            sync_manager.trigger_export()

            # With async debounce, the task should be pending
            assert sync_manager._export_task is not None

            # Wait for debounce + execution
            await asyncio.sleep(0.3)

            # Should have been called exactly once (debounced)
            assert mock_export.call_count == 1

    @pytest.mark.integration
    def test_mutation_triggers_export(self, task_manager, tmp_path, sample_project) -> None:
        """Test that task mutations trigger export."""
        export_path = tmp_path / "tasks.jsonl"
        sync_manager = TaskSyncManager(task_manager, str(export_path))

        try:
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
        finally:
            sync_manager.stop()

    @pytest.mark.integration
    def test_import_from_jsonl(self, sync_manager, task_manager, sample_project) -> None:
        """Test importing tasks from JSONL."""
        # Create JSONL file content
        now = "2023-01-02T00:00:00+00:00"
        later = "2023-01-03T00:00:00+00:00"

        tasks_data = [
            {
                "id": "task-imported-1",
                "title": "Imported Task",
                "description": "Desc",
                "status": "todo",
                "created_at": now,
                "updated_at": now,
                "project_id": sample_project["id"],
                "parent_id": None,
                "deps_on": [],
            },
            {
                "id": "task-imported-2",
                "title": "Imported Task with Dep",
                "description": "Desc",
                "status": "todo",
                "created_at": now,
                "updated_at": later,
                "project_id": sample_project["id"],
                "parent_id": None,
                "deps_on": ["task-imported-1"],
            },
        ]

        # Write export file
        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            for task in tasks_data:
                f.write(json.dumps(task) + "\n")

        # Run import
        sync_manager.import_from_jsonl()

        # Verify tasks in DB
        t1 = task_manager.get_task("task-imported-1")
        assert t1 is not None
        assert t1.title == "Imported Task"

        t2 = task_manager.get_task("task-imported-2")
        assert t2 is not None
        assert t2.title == "Imported Task with Dep"

        # Verify Dependency
        deps = task_manager.db.fetchall(
            "SELECT * FROM task_dependencies WHERE task_id = ?", (t2.id,)
        )
        assert len(deps) == 1
        assert deps[0]["depends_on"] == t1.id

    @pytest.mark.integration
    def test_import_conflict_resolution(self, sync_manager, task_manager, sample_project) -> None:
        """Test LWW conflict resolution during import."""
        # 1. Local Task is NEWER (should keep local)
        t1 = task_manager.create_task(sample_project["id"], "Local Newer")
        # Force updated_at to future
        future = "2025-01-01T00:00:00+00:00"
        task_manager.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (future, t1.id))

        # File has older version
        past = "2020-01-01T00:00:00+00:00"
        file_data = {
            "id": t1.id,
            "title": "File Older",
            "description": "",
            "status": "todo",
            "created_at": past,
            "updated_at": past,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
        }

        # Write file
        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write(json.dumps(file_data) + "\n")

        sync_manager.import_from_jsonl()

        # Verify DB unchanged
        t1_fresh = task_manager.get_task(t1.id)
        assert t1_fresh.title == "Local Newer"

        # 2. File is NEWER (should overwrite local)
        t2 = task_manager.create_task(sample_project["id"], "Local Older")
        task_manager.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (past, t2.id))

        file_data_2 = {
            "id": t2.id,
            "title": "File Newer",
            "description": "",
            "status": "todo",
            "created_at": past,
            "updated_at": future,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
        }

        # Append to file
        with open(sync_manager.export_path, "w") as f:
            f.write(json.dumps(file_data_2) + "\n")

        sync_manager.import_from_jsonl()

        # Verify DB updated
        t2_fresh = task_manager.get_task(t2.id)
        assert t2_fresh.title == "File Newer"

    @pytest.mark.integration
    def test_export_always_writes_fresh_content(
        self, sync_manager, task_manager, sample_project
    ) -> None:
        """Test that export always writes correct content, even if file was externally modified."""
        task_manager.create_task(sample_project["id"], "Task 1")
        sync_manager.export_to_jsonl()

        # Read correct content
        correct_content = sync_manager.export_path.read_text()
        assert "Task 1" in correct_content

        # Externally overwrite the file (simulates git checkout/merge)
        sync_manager.export_path.write_text('{"id": "stale", "title": "Stale data"}\n')

        # Export again — should restore correct content
        sync_manager.export_to_jsonl()
        restored_content = sync_manager.export_path.read_text()
        assert restored_content == correct_content


class TestGetSyncStatus:
    """Tests for the get_sync_status method."""

    @pytest.mark.integration
    def test_get_sync_status_no_file(self, sync_manager) -> None:
        """Test sync status when export file doesn't exist."""
        result = sync_manager.get_sync_status()

        assert result["status"] == "no_file"
        assert result["synced"] is False

    @pytest.mark.integration
    def test_get_sync_status_available(self, sync_manager, task_manager, sample_project) -> None:
        """Test sync status when export file exists."""
        # Create and export a task
        task_manager.create_task(sample_project["id"], "Test Task")
        sync_manager.export_to_jsonl()

        result = sync_manager.get_sync_status()

        assert result["status"] == "available"
        assert result["synced"] is True


class TestImportEdgeCases:
    """Tests for import edge cases and error handling."""

    @pytest.mark.integration
    def test_import_no_file_exists(self, sync_manager) -> None:
        """Test import when file doesn't exist - should just return."""
        # Ensure file doesn't exist
        assert not sync_manager.export_path.exists()

        # Should not raise
        sync_manager.import_from_jsonl()

    @pytest.mark.integration
    def test_import_with_empty_lines(self, sync_manager, task_manager, sample_project) -> None:
        """Test import handles empty lines in JSONL file."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": "task-empty-lines",
            "title": "Test Task",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
        }

        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write("\n")  # Empty line at start
            f.write(json.dumps(tasks_data) + "\n")
            f.write("\n")  # Empty line in middle
            f.write("   \n")  # Whitespace-only line

        sync_manager.import_from_jsonl()

        task = task_manager.get_task("task-empty-lines")
        assert task is not None
        assert task.title == "Test Task"

    @pytest.mark.integration
    def test_import_with_validation_data(self, sync_manager, task_manager, sample_project) -> None:
        """Test import handles validation object."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": "task-validation",
            "title": "Task with Validation",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": {
                "status": "valid",  # Must be 'pending', 'valid', or 'invalid'
                "feedback": "All tests passed",
                "fail_count": 0,
                "criteria": "Must pass unit tests",
                "override_reason": None,
            },
        }

        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        sync_manager.import_from_jsonl()

        task = task_manager.get_task("task-validation")
        assert task is not None
        assert task.validation_status == "valid"
        assert task.validation_feedback == "All tests passed"
        assert task.validation_criteria == "Must pass unit tests"

    @pytest.mark.integration
    def test_import_with_commits(self, sync_manager, task_manager, sample_project) -> None:
        """Test import handles commits array."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": "task-commits",
            "title": "Task with Commits",
            "description": "Desc",
            "status": "completed",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "commits": ["abc123", "def456"],
        }

        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        sync_manager.import_from_jsonl()

        task = task_manager.get_task("task-commits")
        assert task is not None
        assert task.commits == ["abc123", "def456"]

    @pytest.mark.integration
    def test_import_with_escalation_data(self, sync_manager, task_manager, sample_project) -> None:
        """Test import handles escalation fields."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": "task-escalated",
            "title": "Escalated Task",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "escalated_at": now,
            "escalation_reason": "Blocked by external dependency",
        }

        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        sync_manager.import_from_jsonl()

        task = task_manager.get_task("task-escalated")
        assert task is not None
        assert task.escalated_at == now
        assert task.escalation_reason == "Blocked by external dependency"

    @pytest.mark.integration
    def test_import_with_null_validation(self, sync_manager, task_manager, sample_project) -> None:
        """Test import handles null validation object."""
        now = "2023-01-02T00:00:00+00:00"

        tasks_data = {
            "id": "task-null-validation",
            "title": "Task without Validation",
            "description": "Desc",
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "validation": None,
        }

        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write(json.dumps(tasks_data) + "\n")

        sync_manager.import_from_jsonl()

        task = task_manager.get_task("task-null-validation")
        assert task is not None
        assert task.validation_status is None

    @pytest.mark.integration
    def test_import_error_handling(self, sync_manager, task_manager, sample_project) -> None:
        """Test import raises exception on invalid JSON."""
        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write("invalid json {{{")

        with pytest.raises(json.JSONDecodeError):
            sync_manager.import_from_jsonl()


class TestClosedStateRoundTrip:
    """Tests that closed task metadata survives export → import round-trip."""

    @pytest.mark.integration
    def test_closed_task_round_trip_preserves_all_fields(
        self, sync_manager, task_manager, sample_project
    ) -> None:
        """Test that a closed task with full metadata survives export → import."""
        task = task_manager.create_task(sample_project["id"], "Task to close")

        # Simulate a fully closed task with all metadata
        sync_manager.db.execute(
            """UPDATE tasks SET
                status = 'closed',
                closed_at = '2026-01-15T10:00:00+00:00',
                closed_reason = 'completed',
                closed_commit_sha = 'abc123def456',
                labels = '["bug", "p0"]',
                category = 'code',
                agent_name = 'fix-agent',
                accepted_by_user = 1,
                requires_user_review = 1,
                is_expanded = 1,
                expansion_status = 'completed',
                complexity_score = 3,
                estimated_subtasks = 5,
                expansion_context = 'expanded from epic',
                use_external_validator = 1,
                reference_doc = 'docs/spec.md',
                github_issue_number = 42,
                github_pr_number = 99,
                github_repo = 'owner/repo',
                linear_issue_id = 'LIN-123',
                linear_team_id = 'TEAM-1',
                start_date = '2026-01-10',
                due_date = '2026-01-20',
                workflow_name = 'tdd',
                verification = 'tests pass',
                sequence_order = 3
            WHERE id = ?""",
            (task.id,),
        )

        # Export
        sync_manager.export_to_jsonl()

        # Verify JSONL has the closed fields
        lines = sync_manager.export_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["status"] == "closed"
        assert data["closed_at"] is not None
        assert data["closed_reason"] == "completed"
        assert data["closed_commit_sha"] == "abc123def456"
        assert data["labels"] == ["bug", "p0"]
        assert data["category"] == "code"
        assert data["agent_name"] == "fix-agent"
        assert data["accepted_by_user"] is True
        assert data["requires_user_review"] is True
        assert data["is_expanded"] is True
        assert data["expansion_status"] == "completed"
        assert data["github_issue_number"] == 42
        assert data["github_pr_number"] == 99
        assert data["github_repo"] == "owner/repo"
        assert data["linear_issue_id"] == "LIN-123"
        assert data["linear_team_id"] == "TEAM-1"
        assert data["start_date"] == "2026-01-10"
        assert data["due_date"] == "2026-01-20"
        assert data["workflow_name"] == "tdd"
        assert data["verification"] == "tests pass"
        assert data["sequence_order"] == 3
        assert data["reference_doc"] == "docs/spec.md"
        assert data["complexity_score"] == 3
        assert data["estimated_subtasks"] == 5

        # Delete task from DB to simulate fresh import
        sync_manager.db.execute("PRAGMA foreign_keys = OFF")
        sync_manager.db.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
        sync_manager.db.execute("PRAGMA foreign_keys = ON")
        row = sync_manager.db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (task.id,))
        assert row is None

        # Import from JSONL
        sync_manager.import_from_jsonl()

        # Verify all closed state fields survived
        reimported = task_manager.get_task(task.id)
        assert reimported is not None
        assert reimported.status == "closed"
        # closed_at is normalized with microsecond precision during export
        assert reimported.closed_at == "2026-01-15T10:00:00.000000+00:00"
        assert reimported.closed_reason == "completed"
        assert reimported.closed_commit_sha == "abc123def456"
        assert reimported.labels == ["bug", "p0"]
        assert reimported.category == "code"
        assert reimported.agent_name == "fix-agent"
        assert reimported.accepted_by_user is True
        assert reimported.requires_user_review is True
        assert reimported.is_expanded is True
        assert reimported.expansion_status == "completed"
        assert reimported.github_issue_number == 42
        assert reimported.github_pr_number == 99
        assert reimported.github_repo == "owner/repo"
        assert reimported.linear_issue_id == "LIN-123"
        assert reimported.linear_team_id == "TEAM-1"
        assert reimported.start_date == "2026-01-10"
        assert reimported.due_date == "2026-01-20"
        assert reimported.workflow_name == "tdd"
        assert reimported.verification == "tests pass"
        assert reimported.sequence_order == 3
        assert reimported.reference_doc == "docs/spec.md"
        assert reimported.complexity_score == 3
        assert reimported.estimated_subtasks == 5

    @pytest.mark.integration
    def test_update_path_preserves_session_local_fields(
        self, sync_manager, task_manager, sample_project
    ) -> None:
        """Test that UPDATE import path preserves session-local columns."""
        task = task_manager.create_task(sample_project["id"], "Session task")

        # Set session-local fields that should NOT be wiped by import
        # Disable FK checks since session IDs reference sessions table
        sync_manager.db.execute("PRAGMA foreign_keys = OFF")
        sync_manager.db.execute(
            """UPDATE tasks SET
                assignee = 'session-uuid-123',
                created_in_session_id = 'session-aaa',
                closed_in_session_id = 'session-bbb',
                compacted_at = '2026-01-10T00:00:00+00:00',
                summary = 'Compaction summary text',
                updated_at = '2020-01-01T00:00:00+00:00'
            WHERE id = ?""",
            (task.id,),
        )
        sync_manager.db.execute("PRAGMA foreign_keys = ON")

        # Create JSONL with newer timestamp to trigger UPDATE path
        jsonl_data = {
            "id": task.id,
            "title": "Updated title from JSONL",
            "description": "Updated desc",
            "status": "closed",
            "closed_at": "2026-02-01T00:00:00+00:00",
            "closed_reason": "done",
            "created_at": task.created_at,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "project_id": sample_project["id"],
            "parent_id": None,
            "deps_on": [],
            "priority": 2,
            "task_type": "task",
        }

        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sync_manager.export_path, "w") as f:
            f.write(json.dumps(jsonl_data) + "\n")

        sync_manager.import_from_jsonl()

        # Verify synced fields were updated
        updated = task_manager.get_task(task.id)
        assert updated.title == "Updated title from JSONL"
        assert updated.status == "closed"
        assert updated.closed_at == "2026-02-01T00:00:00+00:00"
        assert updated.closed_reason == "done"

        # Verify session-local fields were PRESERVED (not wiped to NULL)
        row = sync_manager.db.fetchone(
            "SELECT assignee, created_in_session_id, closed_in_session_id, "
            "compacted_at, summary FROM tasks WHERE id = ?",
            (task.id,),
        )
        assert row["assignee"] == "session-uuid-123"
        assert row["created_in_session_id"] == "session-aaa"
        assert row["closed_in_session_id"] == "session-bbb"
        assert row["compacted_at"] == "2026-01-10T00:00:00+00:00"
        assert row["summary"] == "Compaction summary text"

    @pytest.mark.integration
    def test_export_includes_priority_and_task_type(
        self, sync_manager, task_manager, sample_project
    ) -> None:
        """Test that export includes priority and task_type fields."""
        task = task_manager.create_task(sample_project["id"], "Typed task")
        sync_manager.db.execute(
            "UPDATE tasks SET priority = 1, task_type = 'bug' WHERE id = ?",
            (task.id,),
        )

        sync_manager.export_to_jsonl()

        lines = sync_manager.export_path.read_text().strip().split("\n")
        data = json.loads(lines[0])
        assert data["priority"] == 1
        assert data["task_type"] == "bug"


class TestExportEdgeCases:
    """Tests for export edge cases and error handling."""

    @pytest.mark.integration
    def test_export_multiple_dependencies(self, sync_manager, task_manager, sample_project) -> None:
        """Test export with task having multiple dependencies."""
        t1 = task_manager.create_task(sample_project["id"], "Dependency 1")
        t2 = task_manager.create_task(sample_project["id"], "Dependency 2")
        t3 = task_manager.create_task(sample_project["id"], "Task with multiple deps")

        # Add multiple dependencies to t3
        now = "2023-01-01T00:00:00"
        sync_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (t3.id, t1.id, "blocking", now),
        )
        sync_manager.db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
            (t3.id, t2.id, "blocking", now),
        )

        sync_manager.export_to_jsonl()

        lines = sync_manager.export_path.read_text().strip().split("\n")
        data = [json.loads(line) for line in lines]

        task3_data = next(d for d in data if d["id"] == t3.id)
        # deps_on should be sorted
        assert sorted(task3_data["deps_on"]) == sorted([t1.id, t2.id])

    @pytest.mark.integration
    def test_export_with_validation_data(self, sync_manager, task_manager, sample_project) -> None:
        """Test export includes validation data."""
        task = task_manager.create_task(sample_project["id"], "Task with validation")

        # Add validation data directly to DB (status must be 'pending', 'valid', or 'invalid')
        sync_manager.db.execute(
            """UPDATE tasks SET
                validation_status = ?,
                validation_feedback = ?,
                validation_fail_count = ?,
                validation_criteria = ?
            WHERE id = ?""",
            ("invalid", "Test failed", 2, "Must pass CI", task.id),
        )

        sync_manager.export_to_jsonl()

        lines = sync_manager.export_path.read_text().strip().split("\n")
        data = json.loads(lines[0])

        assert data["validation"] is not None
        assert data["validation"]["status"] == "invalid"
        assert data["validation"]["feedback"] == "Test failed"
        assert data["validation"]["fail_count"] == 2
        assert data["validation"]["criteria"] == "Must pass CI"

    @pytest.mark.integration
    def test_export_with_commits(self, sync_manager, task_manager, sample_project) -> None:
        """Test export includes commits array."""
        task = task_manager.create_task(sample_project["id"], "Task with commits")

        # Link commits
        commits_json = json.dumps(["commit1", "commit2"])
        sync_manager.db.execute(
            "UPDATE tasks SET commits = ? WHERE id = ?",
            (commits_json, task.id),
        )

        sync_manager.export_to_jsonl()

        lines = sync_manager.export_path.read_text().strip().split("\n")
        data = json.loads(lines[0])

        assert data["commits"] == ["commit1", "commit2"]

    @pytest.mark.integration
    def test_export_error_propagates(self, sync_manager, task_manager, sample_project) -> None:
        """Test that export errors are propagated."""
        task_manager.create_task(sample_project["id"], "Task 1")

        # Make the export path a directory to cause write error
        sync_manager.export_path.parent.mkdir(parents=True, exist_ok=True)
        sync_manager.export_path.mkdir()

        with pytest.raises(IsADirectoryError):
            sync_manager.export_to_jsonl()

    @pytest.mark.integration
    def test_export_empty_tasks(self, sync_manager) -> None:
        """Test export with no tasks creates empty file."""
        sync_manager.export_to_jsonl()

        assert sync_manager.export_path.exists()
        content = sync_manager.export_path.read_text()
        assert content == ""


class TestStopMethod:
    """Tests for the stop method."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stop_cancels_export_task(self, sync_manager):
        """Test stop cancels pending export task."""
        sync_manager._debounce_interval = 10  # Long interval

        with patch.object(sync_manager, "export_to_jsonl") as mock_export:
            sync_manager.trigger_export()
            assert sync_manager._export_task is not None

            sync_manager.stop()

            # Wait a bit to ensure task would have fired if not cancelled
            await asyncio.sleep(0.1)

            # Export should not have been called because task was cancelled
            assert mock_export.call_count == 0
            assert sync_manager._shutdown_requested is True

    @pytest.mark.integration
    def test_stop_without_export_task(self, sync_manager) -> None:
        """Test stop when no export task is running."""
        assert sync_manager._export_task is None

        # Should not raise
        sync_manager.stop()
        assert sync_manager._shutdown_requested is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_shutdown_graceful(self, sync_manager):
        """Test graceful async shutdown."""
        sync_manager._debounce_interval = 0.05

        with patch.object(sync_manager, "export_to_jsonl") as mock_export:
            sync_manager.trigger_export()

            # Graceful shutdown waits for task completion
            await sync_manager.shutdown()

            # Task should complete and export called
            assert sync_manager._export_task is None
            assert mock_export.call_count == 1


class TestImportFromGitHubIssues:
    """Tests for import_from_github_issues async method."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_invalid_github_url(self, sync_manager):
        """Test import with invalid GitHub URL."""
        result = await sync_manager.import_from_github_issues("not-a-url")

        assert result["success"] is False
        assert "Invalid GitHub URL" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_github_url_with_git_suffix(self, sync_manager):
        """Test import handles .git suffix in URL."""
        with patch("subprocess.run") as mock_run:
            # Mock gh --version check
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout="[]"),  # gh issue list
            ]

            result = await sync_manager.import_from_github_issues(
                "https://github.com/owner/repo.git"
            )

            assert result["success"] is True
            assert result["count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_gh_not_installed(self, sync_manager):
        """Test import when gh CLI is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            result = await sync_manager.import_from_github_issues("https://github.com/owner/repo")

            assert result["success"] is False
            assert "GitHub CLI (gh) not found" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_gh_command_fails(self, sync_manager):
        """Test import when gh command fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=1, stderr="auth required"),  # gh issue list
            ]

            result = await sync_manager.import_from_github_issues("https://github.com/owner/repo")

            assert result["success"] is False
            assert "gh command failed" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_no_open_issues(self, sync_manager):
        """Test import when there are no open issues."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout="[]"),  # gh issue list
            ]

            result = await sync_manager.import_from_github_issues("https://github.com/owner/repo")

            assert result["success"] is True
            assert result["count"] == 0
            assert result["imported"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_without_project_context(self, sync_manager):
        """Test import fails without project context."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body 1",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout=issues_json),  # gh issue list
            ]

            with patch("gobby.utils.project_context.get_project_context", return_value=None):
                result = await sync_manager.import_from_github_issues(
                    "https://github.com/owner/repo"
                )

        assert result["success"] is False
        assert "Could not determine project ID" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_with_project_id(self, sync_manager, sample_project):
        """Test import with explicit project_id."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body 1",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                },
                {
                    "number": 2,
                    "title": "Issue 2",
                    "body": None,
                    "labels": [{"name": "bug"}],
                    "createdAt": "2023-01-02T00:00:00Z",
                },
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # gh --version
                MagicMock(returncode=0, stdout=issues_json),  # gh issue list
            ]

            result = await sync_manager.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        assert result["count"] == 2
        assert "gh-1" in result["imported"]
        assert "gh-2" in result["imported"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_updates_existing(self, sync_manager, sample_project):
        """Test import updates existing issues."""
        # First import
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Original body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result1 = await sync_manager.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result1["count"] == 1

        # Second import with updated issue
        issues_json_updated = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Updated Title",
                    "body": "Updated body",
                    "labels": [{"name": "enhancement"}],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json_updated),
            ]

            result2 = await sync_manager.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        # Should update, not import
        assert result2["count"] == 0
        assert "gh-1" in result2["imported"]
        assert "updated 1 existing" in result2["message"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_skip_no_number(self, sync_manager, sample_project):
        """Test import skips issues without number."""
        issues_json = json.dumps(
            [
                {
                    "title": "Issue without number",
                    "body": "Body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await sync_manager.import_from_github_issues(
                "https://github.com/owner/repo",
                project_id=sample_project["id"],
            )

        assert result["success"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_json_decode_error(self, sync_manager):
        """Test import handles invalid JSON from gh."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout="not valid json"),
            ]

            result = await sync_manager.import_from_github_issues("https://github.com/owner/repo")

        assert result["success"] is False
        assert "Failed to parse GitHub response" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_finds_project_by_url(self, sync_manager, sample_project):
        """Test import finds project by matching github_url."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        # The sample_project fixture has github_url set
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            result = await sync_manager.import_from_github_issues(
                repo_url=sample_project["github_url"],
            )

        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_general_exception(self, sync_manager):
        """Test import handles general exceptions."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                Exception("Unexpected error"),
            ]

            result = await sync_manager.import_from_github_issues("https://github.com/owner/repo")

        assert result["success"] is False
        assert "Unexpected error" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_issues_with_project_context(self, sync_manager, sample_project):
        """Test import uses project context when project_id not provided."""
        issues_json = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Issue 1",
                    "body": "Body",
                    "labels": [],
                    "createdAt": "2023-01-01T00:00:00Z",
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=issues_json),
            ]

            # Mock project context to return sample project
            with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
                mock_ctx.return_value = {"id": sample_project["id"]}

                result = await sync_manager.import_from_github_issues(
                    "https://github.com/different/repo"
                )

        assert result["success"] is True
        assert result["count"] == 1
