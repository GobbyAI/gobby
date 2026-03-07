from unittest.mock import patch

import pytest

from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager, TaskIDCollisionError

pytestmark = pytest.mark.unit


@pytest.fixture
def dep_manager(temp_db):
    return TaskDependencyManager(temp_db)


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project):
    return sample_project["id"]


@pytest.mark.integration
class TestLocalTaskManager:
    def test_create_task(self, task_manager, project_id) -> None:
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
        # Task IDs are now UUIDs (8-4-4-4-12 hex chars with dashes)
        assert "-" in task.id and len(task.id.split("-")) == 5
        assert task.created_at is not None
        assert task.updated_at is not None

    def test_get_task(self, task_manager, project_id) -> None:
        created = task_manager.create_task(project_id=project_id, title="Find me")
        fetched = task_manager.get_task(created.id)
        assert fetched == created

    def test_update_task(self, task_manager, project_id) -> None:
        task = task_manager.create_task(project_id=project_id, title="Original Title")
        updated = task_manager.update_task(task.id, title="New Title", status="in_progress")
        assert updated.title == "New Title"
        assert updated.status == "in_progress"
        assert updated.updated_at > task.updated_at

    def test_close_task(self, task_manager, project_id) -> None:
        task = task_manager.create_task(project_id=project_id, title="To Close")
        closed = task_manager.close_task(task.id, reason="Done")
        assert closed.status == "closed"
        assert closed.closed_reason == "Done"

    def test_delete_task(self, task_manager, project_id) -> None:
        task = task_manager.create_task(project_id=project_id, title="To Delete")
        task_manager.delete_task(task.id)

        with pytest.raises(ValueError, match="not found"):
            task_manager.get_task(task.id)

    def test_list_tasks(self, task_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id=project_id, title="Task 1", priority=1)
        _ = task_manager.create_task(project_id=project_id, title="Task 2", priority=2)

        tasks = task_manager.list_tasks(project_id=project_id)
        assert len(tasks) == 2

        # Test filtering
        tasks_p1 = task_manager.list_tasks(project_id=project_id, priority=1)
        assert len(tasks_p1) == 1
        assert tasks_p1[0].id == t1.id

    def test_id_collision_retry(self, task_manager, project_id) -> None:
        # Create a task to occupy an ID
        existing_task = task_manager.create_task(project_id=project_id, title="Existing")

        # Mock generate_task_id to return existing ID once, then a new one
        # Patch where it's used (_crud.py), not where it's re-exported
        with patch(
            "gobby.storage.tasks._crud.generate_task_id",
            side_effect=[existing_task.id, "gt-newunique"],
        ) as mock_gen:
            new_task = task_manager.create_task(project_id=project_id, title="New Task")
            assert new_task.id == "gt-newunique"
            # Should have called it twice (initial attempt + retry)
            # Actually create_task calls generate_task_id in a loop, passing salt.
            # Side_effect replaces the return value of ALL calls.
            # We assume logic calls generate_task_id.
            assert mock_gen.call_count == 2

    def test_id_collision_failure(self, task_manager, project_id) -> None:
        existing_task = task_manager.create_task(project_id=project_id, title="Existing")

        # Mock to always return existing ID
        # Patch where it's used (_crud.py), not where it's re-exported
        with patch("gobby.storage.tasks._crud.generate_task_id", return_value=existing_task.id):
            with pytest.raises(TaskIDCollisionError):
                task_manager.create_task(project_id=project_id, title="Doom")

    def test_delete_with_children_fails_without_cascade(self, task_manager, project_id) -> None:
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        _ = task_manager.create_task(project_id=project_id, title="Child", parent_task_id=parent.id)

        with pytest.raises(ValueError, match="has children"):
            task_manager.delete_task(parent.id)

    def test_delete_with_cascade(self, task_manager, project_id) -> None:
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )

        task_manager.delete_task(parent.id, cascade=True)

        with pytest.raises(ValueError):
            task_manager.get_task(parent.id)
        with pytest.raises(ValueError):
            task_manager.get_task(child.id)

    def test_delete_with_dependents_no_flags_raises(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Test that deleting a task with dependents raises error without cascade/unlink."""
        blocker = task_manager.create_task(project_id=project_id, title="Blocker")
        dependent = task_manager.create_task(project_id=project_id, title="Dependent")
        dep_manager.add_dependency(dependent.id, blocker.id, "blocks")

        with pytest.raises(ValueError, match="dependent task"):
            task_manager.delete_task(blocker.id)

    def test_delete_with_dependents_cascade_deletes_all(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Test that cascade=True deletes the task AND its dependents."""
        blocker = task_manager.create_task(project_id=project_id, title="Blocker")
        dependent = task_manager.create_task(project_id=project_id, title="Dependent")
        dep_manager.add_dependency(dependent.id, blocker.id, "blocks")

        task_manager.delete_task(blocker.id, cascade=True)

        with pytest.raises(ValueError):
            task_manager.get_task(blocker.id)
        with pytest.raises(ValueError):
            task_manager.get_task(dependent.id)

    def test_delete_cascade_with_circular_parent_child_dependency(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Test that cascade delete handles parent depending on children without infinite recursion.

        This tests the scenario where:
        - Parent has children (parent_task_id relationship)
        - Parent also depends on children (blocked_by dependency)

        This could cause infinite recursion if not handled properly:
        1. Delete parent -> deletes child (parent_task_id)
        2. Child has dependent (parent) -> tries to delete parent
        3. Infinite loop
        """
        parent = task_manager.create_task(project_id=project_id, title="Parent Epic")
        child1 = task_manager.create_task(
            project_id=project_id, title="Child 1", parent_task_id=parent.id
        )
        child2 = task_manager.create_task(
            project_id=project_id, title="Child 2", parent_task_id=parent.id
        )

        # Parent depends on (is blocked by) its children - common pattern for epics
        dep_manager.add_dependency(parent.id, child1.id, "blocks")
        dep_manager.add_dependency(parent.id, child2.id, "blocks")

        # This should NOT cause infinite recursion
        task_manager.delete_task(parent.id, cascade=True)

        # All tasks should be deleted
        with pytest.raises(ValueError):
            task_manager.get_task(parent.id)
        with pytest.raises(ValueError):
            task_manager.get_task(child1.id)
        with pytest.raises(ValueError):
            task_manager.get_task(child2.id)

    def test_delete_with_dependents_unlink_preserves(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Test that unlink=True deletes task but preserves dependents."""
        blocker = task_manager.create_task(project_id=project_id, title="Blocker")
        dependent = task_manager.create_task(project_id=project_id, title="Dependent")
        dep_manager.add_dependency(dependent.id, blocker.id, "blocks")

        task_manager.delete_task(blocker.id, unlink=True)

        # Blocker should be gone
        with pytest.raises(ValueError):
            task_manager.get_task(blocker.id)

        # Dependent should still exist
        preserved = task_manager.get_task(dependent.id)
        assert preserved is not None
        assert preserved.title == "Dependent"

        # Dependency should be cleaned up by ON DELETE CASCADE
        blockers = dep_manager.get_blockers(dependent.id)
        assert len(blockers) == 0

    def test_delete_error_includes_task_refs(self, task_manager, dep_manager, project_id) -> None:
        """Test that error message includes human-readable task refs."""
        blocker = task_manager.create_task(project_id=project_id, title="Blocker")
        dep1 = task_manager.create_task(project_id=project_id, title="Dep1")
        dep2 = task_manager.create_task(project_id=project_id, title="Dep2")
        dep_manager.add_dependency(dep1.id, blocker.id, "blocks")
        dep_manager.add_dependency(dep2.id, blocker.id, "blocks")

        with pytest.raises(ValueError) as exc:
            task_manager.delete_task(blocker.id)

        error = str(exc.value)
        assert "2 dependent task(s)" in error
        assert "#" in error  # Should include seq_num refs

    def test_list_ready_tasks(self, task_manager, dep_manager, project_id) -> None:
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

    def test_list_blocked_tasks(self, task_manager, dep_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        t2 = task_manager.create_task(project_id, "T2")

        dep_manager.add_dependency(t1.id, t2.id, "blocks")

        blocked = task_manager.list_blocked_tasks(project_id=project_id)
        assert len(blocked) == 1
        assert blocked[0].id == t1.id

        task_manager.close_task(t2.id)
        blocked = task_manager.list_blocked_tasks(project_id=project_id)
        # T1 is no longer blocked by OPEN task

    def test_parent_blocked_by_children_is_still_ready(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Parent tasks blocked by their own children should still be considered 'ready'.

        This is because 'blocked by children' means 'cannot close until children done',
        not 'cannot start working'. The parent should still appear in list_ready_tasks.
        """
        # Create parent and child
        parent = task_manager.create_task(project_id, "Parent Epic")
        child = task_manager.create_task(project_id, "Child Task", parent_task_id=parent.id)

        # Create the dependency: parent depends_on child with type "blocks"
        # This means child blocks parent (parent can't close until child is done)
        dep_manager.add_dependency(parent.id, child.id, "blocks")

        # Both parent and child should be ready (the child->parent block is a completion block)
        ready = task_manager.list_ready_tasks(project_id=project_id)
        ready_ids = {t.id for t in ready}
        assert parent.id in ready_ids, "Parent should be ready despite being blocked by its child"
        assert child.id in ready_ids, "Child should be ready (no blockers)"

        # Parent should NOT appear in list_blocked_tasks
        blocked = task_manager.list_blocked_tasks(project_id=project_id)
        blocked_ids = {t.id for t in blocked}
        assert parent.id not in blocked_ids, "Parent should not be 'blocked' by its own child"

        # Now add an external blocker (a task that is NOT a child)
        external_blocker = task_manager.create_task(project_id, "External Blocker")
        dep_manager.add_dependency(parent.id, external_blocker.id, "blocks")

        # Now parent should be blocked by the external task
        ready = task_manager.list_ready_tasks(project_id=project_id)
        ready_ids = {t.id for t in ready}
        assert parent.id not in ready_ids, "Parent should NOT be ready (blocked by external task)"

        blocked = task_manager.list_blocked_tasks(project_id=project_id)
        blocked_ids = {t.id for t in blocked}
        assert parent.id in blocked_ids, "Parent should be blocked by external task"

        # Close the external blocker
        task_manager.close_task(external_blocker.id)

        # Parent should be ready again (only blocked by its own child)
        ready = task_manager.list_ready_tasks(project_id=project_id)
        ready_ids = {t.id for t in ready}
        assert parent.id in ready_ids, "Parent should be ready again"

    def test_labels_management(self, task_manager, project_id) -> None:
        task = task_manager.create_task(project_id, "Label Task", labels=["a"])

        # Add label
        task = task_manager.add_label(task.id, "b")
        assert set(task.labels) == {"a", "b"}

        # Add existing label (no-op)
        task = task_manager.add_label(task.id, "a")
        assert set(task.labels) == {"a", "b"}

        # Remove label
        task = task_manager.remove_label(task.id, "a")
        assert task.labels == ["b"]

        # Remove non-existent label (no-op)
        task = task_manager.remove_label(task.id, "c")
        assert task.labels == ["b"]

    def test_find_by_prefix(self, task_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "Find Me")
        # ID is like gt-123456

        # Test exact match
        found = task_manager.find_task_by_prefix(t1.id)
        assert found.id == t1.id

        # Test prefix match
        prefix = t1.id[:6]  # gt-123
        found = task_manager.find_task_by_prefix(prefix)
        assert found.id == t1.id

        # Test multiple matches returns None
        # We need another task with same prefix. Hard to force with random hash.
        # But we can mock or just test the logic that calls fetchall.
        # Actually generate_task_id uses timestamp + random, so very unlikely to clash prefix unless specifically crafted.

        # Test no match
        assert task_manager.find_task_by_prefix("gt-nomatch") is None

    def test_find_tasks_by_prefix(self, task_manager, project_id) -> None:
        t1 = task_manager.create_task(project_id, "T1")
        prefix = t1.id[:5]  # gt-12

        tasks = task_manager.find_tasks_by_prefix(prefix)
        assert len(tasks) >= 1
        assert t1.id in [t.id for t in tasks]

    def test_hierarchical_ordering(self, task_manager, project_id) -> None:
        # Root 1
        r1 = task_manager.create_task(project_id, "R1", priority=1)
        # Root 2
        r2 = task_manager.create_task(project_id, "R2", priority=2)

        # Children of R1
        c1_1 = task_manager.create_task(project_id, "C1.1", parent_task_id=r1.id, priority=2)
        c1_2 = task_manager.create_task(project_id, "C1.2", parent_task_id=r1.id, priority=1)

        # Child of C1.2
        c1_2_1 = task_manager.create_task(project_id, "C1.2.1", parent_task_id=c1_2.id)

        tasks = task_manager.list_tasks(project_id)
        ids = [t.id for t in tasks]

        # Expected: R1 -> C1.2 (prio 1) -> C1.2.1 -> C1.1 (prio 2) -> R2
        current_indices = {tid: idx for idx, tid in enumerate(ids)}

        # Verify relative ordering
        assert current_indices[r1.id] < current_indices[r2.id]
        assert current_indices[r1.id] < current_indices[c1_2.id]
        assert current_indices[c1_2.id] < current_indices[c1_2_1.id]
        assert current_indices[c1_2.id] < current_indices[c1_1.id]  # Priority 1 vs 2

    def test_update_all_fields(self, task_manager, project_id) -> None:
        task = task_manager.create_task(project_id, "T1")

        updated = task_manager.update_task(
            task.id,
            description="desc",
            priority=5,
            task_type="chore",
            assignee="me",
            labels=["l1"],
            category="strat",
            complexity_score=10,
            estimated_subtasks=5,
            expansion_context="ctx",
            validation_criteria="crit",
            use_external_validator=True,
            validation_fail_count=2,
            validation_status="valid",
            validation_feedback="good",
        )

        assert updated.description == "desc"
        assert updated.priority == 5
        assert updated.task_type == "chore"
        assert updated.assignee == "me"
        assert updated.labels == ["l1"]
        assert updated.category == "strat"
        assert updated.complexity_score == 10
        assert updated.estimated_subtasks == 5
        assert updated.expansion_context == "ctx"
        assert updated.validation_criteria == "crit"
        assert updated.use_external_validator is True
        assert updated.validation_fail_count == 2
        assert updated.validation_status == "valid"
        assert updated.validation_feedback == "good"

    def test_clear_parent_task(self, task_manager, project_id) -> None:
        parent = task_manager.create_task(project_id, "P")
        child = task_manager.create_task(project_id, "C", parent_task_id=parent.id)

        assert child.parent_task_id == parent.id

        # Explicit None should clear it
        updated = task_manager.update_task(child.id, parent_task_id=None)
        assert updated.parent_task_id is None

    def test_close_task_with_many_children(self, task_manager, project_id) -> None:
        parent = task_manager.create_task(project_id, "P")
        for i in range(5):
            task_manager.create_task(project_id, f"C{i}", parent_task_id=parent.id)

        with pytest.raises(ValueError) as exc:
            task_manager.close_task(parent.id)

        msg = str(exc.value)
        assert "has 5 open child task(s)" in msg
        assert "and 2 more" in msg

    # =========================================================================
    # Commit Linking Tests
    # =========================================================================

    def test_link_commit_adds_sha_to_empty_task(self, task_manager, project_id) -> None:
        """Test linking a commit to a task with no commits."""
        task = task_manager.create_task(project_id, "Task with commits")
        assert task.commits is None or task.commits == []

        # Mock normalize_commit_sha to return the input (simulating valid SHA)
        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "abc123d"  # Normalized short form
            updated = task_manager.link_commit(task.id, "abc123def456")

        assert updated.commits == ["abc123d"]

    def test_link_commit_appends_to_existing(self, task_manager, project_id) -> None:
        """Test linking adds to existing commits array."""
        task = task_manager.create_task(project_id, "Task with commits")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "commit1"
            task_manager.link_commit(task.id, "commit1")
            mock_normalize.return_value = "commit2"
            updated = task_manager.link_commit(task.id, "commit2")

        assert "commit1" in updated.commits
        assert "commit2" in updated.commits
        assert len(updated.commits) == 2

    def test_link_commit_ignores_duplicate(self, task_manager, project_id) -> None:
        """Test linking same commit twice doesn't duplicate."""
        task = task_manager.create_task(project_id, "Task with commits")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "abc1234"
            task_manager.link_commit(task.id, "abc123")
            updated = task_manager.link_commit(task.id, "abc123")

        assert updated.commits == ["abc1234"]

    def test_link_commit_invalid_task(self, task_manager) -> None:
        """Test linking commit to non-existent task raises error."""
        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "abc1234"
            with pytest.raises(ValueError, match="not found"):
                task_manager.link_commit("gt-nonexistent", "abc123")

    def test_link_commit_invalid_sha(self, task_manager, project_id) -> None:
        """Test linking invalid SHA raises error."""
        task = task_manager.create_task(project_id, "Task with commits")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = None  # SHA can't be resolved
            with pytest.raises(ValueError, match="Invalid or unresolved"):
                task_manager.link_commit(task.id, "invalidsha")

    def test_unlink_commit_removes_sha(self, task_manager, project_id) -> None:
        """Test unlinking removes commit from array."""
        task = task_manager.create_task(project_id, "Task with commits")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "commit1"
            task_manager.link_commit(task.id, "commit1")
            mock_normalize.return_value = "commit2"
            task_manager.link_commit(task.id, "commit2")
            mock_normalize.return_value = "commit1"
            updated = task_manager.unlink_commit(task.id, "commit1")

        assert updated.commits == ["commit2"]

    def test_unlink_commit_handles_nonexistent(self, task_manager, project_id) -> None:
        """Test unlinking non-existent commit is a no-op."""
        task = task_manager.create_task(project_id, "Task with commits")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "commit1"
            task_manager.link_commit(task.id, "commit1")
            mock_normalize.return_value = "nonexist"  # Different normalized value
            # Should not raise, just return unchanged
            updated = task_manager.unlink_commit(task.id, "nonexistent")

        assert updated.commits == ["commit1"]

    def test_unlink_commit_requires_normalized_sha(self, task_manager, project_id) -> None:
        """Test unlinking requires successful SHA normalization."""
        task = task_manager.create_task(project_id, "Task with commits")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "abc1234"
            task_manager.link_commit(task.id, "abc1234")

            # Simulate normalize failing - should NOT remove anything
            mock_normalize.return_value = None
            updated = task_manager.unlink_commit(task.id, "abc1234")

        # Commit should still be present since normalize returned None
        assert updated.commits == ["abc1234"]

    def test_unlink_commit_from_empty_task(self, task_manager, project_id) -> None:
        """Test unlinking from task with no commits is a no-op."""
        task = task_manager.create_task(project_id, "Empty task")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "abc1234"
            updated = task_manager.unlink_commit(task.id, "abc123")

        assert updated.commits is None or updated.commits == []

    def test_unlink_commit_invalid_task(self, task_manager) -> None:
        """Test unlinking from non-existent task raises error."""
        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "abc1234"
            with pytest.raises(ValueError, match="not found"):
                task_manager.unlink_commit("gt-nonexistent", "abc123")

    def test_commits_persist_after_update(self, task_manager, project_id) -> None:
        """Test that commits array persists through other updates."""
        task = task_manager.create_task(project_id, "Task")

        with patch("gobby.utils.git.normalize_commit_sha") as mock_normalize:
            mock_normalize.return_value = "commit1"
            task_manager.link_commit(task.id, "commit1")

        # Update another field
        updated = task_manager.update_task(task.id, title="Updated Title")

        assert updated.commits == ["commit1"]
        assert updated.title == "Updated Title"

    # =========================================================================
    # Reopen Task Tests
    # =========================================================================

    def test_reopen_task_basic(self, task_manager, project_id) -> None:
        """Test reopening a closed task."""
        task = task_manager.create_task(project_id, "To Reopen")
        task_manager.close_task(task.id, reason="Done")

        reopened = task_manager.reopen_task(task.id)

        assert reopened.status == "open"
        assert reopened.closed_reason is None
        assert reopened.closed_at is None
        assert reopened.closed_in_session_id is None
        assert reopened.closed_commit_sha is None

    def test_reopen_task_with_reason(self, task_manager, project_id) -> None:
        """Test reopening a task with a reason adds note to description."""
        task = task_manager.create_task(project_id, "To Reopen", description="Original description")
        task_manager.close_task(task.id)

        reopened = task_manager.reopen_task(task.id, reason="Bug found")

        assert reopened.status == "open"
        assert "Original description" in reopened.description
        assert "[Reopened: Bug found]" in reopened.description

    def test_reopen_task_already_open_raises(self, task_manager, project_id) -> None:
        """Test reopening an already open task raises error."""
        task = task_manager.create_task(project_id, "Open Task")

        with pytest.raises(ValueError, match="is already open"):
            task_manager.reopen_task(task.id)

    def test_reopen_task_from_in_progress(self, task_manager, project_id) -> None:
        """Test reopening an in_progress task succeeds."""
        task = task_manager.create_task(project_id, "In Progress")
        task_manager.update_task(task.id, status="in_progress", assignee="test-agent")

        reopened = task_manager.reopen_task(task.id)

        assert reopened.status == "open"
        assert reopened.assignee is None

    # =========================================================================
    # Close Task Additional Tests
    # =========================================================================

    def test_close_task_force_with_open_children(self, task_manager, project_id) -> None:
        """Test force closing a task with open children."""
        parent = task_manager.create_task(project_id, "Parent")
        task_manager.create_task(project_id, "Child", parent_task_id=parent.id)

        # Normal close should fail
        with pytest.raises(ValueError, match="open child task"):
            task_manager.close_task(parent.id)

        # Force close should succeed
        closed = task_manager.close_task(parent.id, force=True)
        assert closed.status == "closed"

    def test_close_task_with_session_and_commit(
        self, task_manager, project_id, session_manager
    ) -> None:
        """Test closing task records session ID and commit SHA."""
        # Create a session first (foreign key constraint)
        session = session_manager.register(
            external_id="test-ext-id",
            machine_id="test-machine",
            source="claude",
            project_id=project_id,
        )

        task = task_manager.create_task(project_id, "Task")

        closed = task_manager.close_task(
            task.id,
            reason="Done",
            closed_in_session_id=session.id,
            closed_commit_sha="abc123def",
        )

        assert closed.closed_in_session_id == session.id
        assert closed.closed_commit_sha == "abc123def"

    def test_close_task_with_validation_override(self, task_manager, project_id) -> None:
        """Test closing task with validation override reason."""
        task = task_manager.create_task(project_id, "Task")

        closed = task_manager.close_task(
            task.id, validation_override_reason="User approved manually"
        )

        assert closed.validation_override_reason == "User approved manually"

    def test_close_task_not_found_raises(self, task_manager) -> None:
        """Test closing non-existent task raises error."""
        with pytest.raises(ValueError, match="not found"):
            task_manager.close_task("gt-nonexistent")

    # =========================================================================
    # Update Task Additional Tests
    # =========================================================================

    def test_update_task_workflow_fields(self, task_manager, project_id) -> None:
        """Test updating workflow-related fields."""
        task = task_manager.create_task(project_id, "Task")

        updated = task_manager.update_task(
            task.id,
            workflow_name="test-workflow",
            verification="Test passes",
            sequence_order=5,
        )

        assert updated.workflow_name == "test-workflow"
        assert updated.verification == "Test passes"
        assert updated.sequence_order == 5

    def test_update_task_escalation_fields(self, task_manager, project_id) -> None:
        """Test updating escalation-related fields."""
        task = task_manager.create_task(project_id, "Task")

        updated = task_manager.update_task(
            task.id,
            escalated_at="2024-01-01T00:00:00Z",
            escalation_reason="Blocked on external dependency",
        )

        assert updated.escalated_at == "2024-01-01T00:00:00Z"
        assert updated.escalation_reason == "Blocked on external dependency"

    def test_update_task_labels_to_none(self, task_manager, project_id) -> None:
        """Test setting labels to None converts to empty JSON array."""
        task = task_manager.create_task(project_id, "Task", labels=["a", "b"])

        updated = task_manager.update_task(task.id, labels=None)

        # Labels should be empty list, not None (due to JSON storage)
        assert updated.labels == []

    def test_update_task_no_changes(self, task_manager, project_id) -> None:
        """Test update with no changes returns current task."""
        task = task_manager.create_task(project_id, "Task")

        updated = task_manager.update_task(task.id)

        assert updated.id == task.id
        # updated_at should not change when no fields are updated
        # Actually it does change based on the code - let's verify the task is returned
        assert updated.title == task.title

    def test_update_task_not_found_raises(self, task_manager) -> None:
        """Test updating non-existent task raises error."""
        with pytest.raises(ValueError, match="not found"):
            task_manager.update_task("gt-nonexistent", title="New")

    # =========================================================================
    # Delete Task Tests
    # =========================================================================

    def test_delete_nonexistent_task_returns_false(self, task_manager) -> None:
        """Test deleting non-existent task returns False."""
        result = task_manager.delete_task("gt-nonexistent")
        assert result is False

    # =========================================================================
    # List Tasks Additional Filter Tests
    # =========================================================================

    def test_list_tasks_with_status_list(self, task_manager, project_id) -> None:
        """Test filtering tasks by multiple statuses."""
        t1 = task_manager.create_task(project_id, "Open Task")
        t2 = task_manager.create_task(project_id, "In Progress")
        task_manager.update_task(t2.id, status="in_progress")
        t3 = task_manager.create_task(project_id, "Closed")
        task_manager.close_task(t3.id)

        # Filter by list of statuses
        tasks = task_manager.list_tasks(project_id=project_id, status=["open", "in_progress"])

        task_ids = {t.id for t in tasks}
        assert t1.id in task_ids
        assert t2.id in task_ids
        assert t3.id not in task_ids

    def test_list_tasks_with_title_like(self, task_manager, project_id) -> None:
        """Test filtering tasks by title pattern."""
        task_manager.create_task(project_id, "Fix bug in auth")
        task_manager.create_task(project_id, "Add feature X")
        task_manager.create_task(project_id, "Fix bug in API")

        tasks = task_manager.list_tasks(project_id=project_id, title_like="Fix bug")

        assert len(tasks) == 2
        for t in tasks:
            assert "Fix bug" in t.title

    def test_list_tasks_with_label_filter(self, task_manager, project_id) -> None:
        """Test filtering tasks by label."""
        task_manager.create_task(project_id, "Task 1", labels=["urgent", "backend"])
        task_manager.create_task(project_id, "Task 2", labels=["frontend"])
        task_manager.create_task(project_id, "Task 3", labels=["urgent", "frontend"])

        tasks = task_manager.list_tasks(project_id=project_id, label="urgent")

        assert len(tasks) == 2
        for t in tasks:
            assert "urgent" in t.labels

    def test_list_tasks_with_assignee_filter(self, task_manager, project_id) -> None:
        """Test filtering tasks by assignee."""
        task_manager.create_task(project_id, "Task 1", assignee="alice")
        task_manager.create_task(project_id, "Task 2", assignee="bob")

        tasks = task_manager.list_tasks(project_id=project_id, assignee="alice")

        assert len(tasks) == 1
        assert tasks[0].assignee == "alice"

    def test_list_tasks_with_task_type_filter(self, task_manager, project_id) -> None:
        """Test filtering tasks by type."""
        task_manager.create_task(project_id, "Bug 1", task_type="bug")
        task_manager.create_task(project_id, "Feature 1", task_type="feature")

        tasks = task_manager.list_tasks(project_id=project_id, task_type="bug")

        assert len(tasks) == 1
        assert tasks[0].task_type == "bug"

    # =========================================================================
    # List Ready Tasks Filter Tests
    # =========================================================================

    def test_list_ready_tasks_with_task_type_filter(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Test filtering ready tasks by type."""
        task_manager.create_task(project_id, "Bug 1", task_type="bug")
        task_manager.create_task(project_id, "Feature 1", task_type="feature")

        tasks = task_manager.list_ready_tasks(project_id=project_id, task_type="bug")

        assert len(tasks) == 1
        assert tasks[0].task_type == "bug"

    def test_list_ready_tasks_with_assignee_filter(self, task_manager, project_id) -> None:
        """Test filtering ready tasks by assignee."""
        task_manager.create_task(project_id, "Task 1", assignee="alice")
        task_manager.create_task(project_id, "Task 2", assignee="bob")

        tasks = task_manager.list_ready_tasks(project_id=project_id, assignee="alice")

        assert len(tasks) == 1
        assert tasks[0].assignee == "alice"

    def test_list_ready_tasks_with_priority_filter(self, task_manager, project_id) -> None:
        """Test filtering ready tasks by priority."""
        task_manager.create_task(project_id, "High Priority", priority=1)
        task_manager.create_task(project_id, "Low Priority", priority=3)

        tasks = task_manager.list_ready_tasks(project_id=project_id, priority=1)

        assert len(tasks) == 1
        assert tasks[0].priority == 1

    def test_list_ready_tasks_with_parent_filter(self, task_manager, project_id) -> None:
        """Test filtering ready tasks by parent."""
        parent = task_manager.create_task(project_id, "Parent")
        task_manager.create_task(project_id, "Child 1", parent_task_id=parent.id)
        task_manager.create_task(project_id, "Child 2", parent_task_id=parent.id)
        task_manager.create_task(project_id, "Orphan")

        tasks = task_manager.list_ready_tasks(project_id=project_id, parent_task_id=parent.id)

        assert len(tasks) == 2
        for t in tasks:
            assert t.parent_task_id == parent.id

    def test_list_ready_tasks_with_limit_offset(self, task_manager, project_id) -> None:
        """Test pagination in ready tasks."""
        for i in range(5):
            task_manager.create_task(project_id, f"Task {i}")

        tasks = task_manager.list_ready_tasks(project_id=project_id, limit=2, offset=1)

        assert len(tasks) == 2

    # =========================================================================
    # List Blocked Tasks Filter Tests
    # =========================================================================

    def test_list_blocked_tasks_with_parent_filter(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Test filtering blocked tasks by parent."""
        parent = task_manager.create_task(project_id, "Parent")
        child1 = task_manager.create_task(project_id, "Child 1", parent_task_id=parent.id)
        blocker = task_manager.create_task(project_id, "Blocker")

        dep_manager.add_dependency(child1.id, blocker.id, "blocks")

        blocked = task_manager.list_blocked_tasks(project_id=project_id, parent_task_id=parent.id)

        assert len(blocked) == 1
        assert blocked[0].id == child1.id

    def test_list_blocked_tasks_with_limit_offset(
        self, task_manager, dep_manager, project_id
    ) -> None:
        """Test pagination in blocked tasks."""
        blocker = task_manager.create_task(project_id, "Blocker")
        for i in range(5):
            task = task_manager.create_task(project_id, f"Blocked {i}")
            dep_manager.add_dependency(task.id, blocker.id, "blocks")

        blocked = task_manager.list_blocked_tasks(project_id=project_id, limit=2, offset=1)

        assert len(blocked) == 2

    # =========================================================================
    # Workflow Tasks Tests
    # =========================================================================

    def test_list_workflow_tasks(self, task_manager, project_id) -> None:
        """Test listing tasks by workflow name."""
        task_manager.create_task(
            project_id, "Task 1", workflow_name="test-workflow", sequence_order=1
        )
        task_manager.create_task(
            project_id, "Task 2", workflow_name="test-workflow", sequence_order=0
        )
        task_manager.create_task(
            project_id, "Task 3", workflow_name="other-workflow", sequence_order=0
        )

        tasks = task_manager.list_workflow_tasks("test-workflow", project_id=project_id)

        assert len(tasks) == 2
        # Should be ordered by sequence_order
        assert tasks[0].sequence_order == 0
        assert tasks[1].sequence_order == 1

    def test_list_workflow_tasks_with_status_filter(self, task_manager, project_id) -> None:
        """Test filtering workflow tasks by status."""
        task_manager.create_task(project_id, "Open", workflow_name="wf")
        t2 = task_manager.create_task(project_id, "Closed", workflow_name="wf")
        task_manager.close_task(t2.id)

        tasks = task_manager.list_workflow_tasks("wf", project_id=project_id, status="open")

        assert len(tasks) == 1
        assert tasks[0].status == "open"

    def test_list_workflow_tasks_without_project_filter(self, task_manager, project_id) -> None:
        """Test listing workflow tasks without project filter."""
        task_manager.create_task(project_id, "Task", workflow_name="global-wf")

        tasks = task_manager.list_workflow_tasks("global-wf")

        assert len(tasks) == 1

    # =========================================================================
    # Count Tasks Tests
    # =========================================================================

    def test_count_tasks_all(self, task_manager, project_id) -> None:
        """Test counting all tasks."""
        for i in range(3):
            task_manager.create_task(project_id, f"Task {i}")

        count = task_manager.count_tasks(project_id=project_id)
        assert count == 3

    def test_count_tasks_by_status(self, task_manager, project_id) -> None:
        """Test counting tasks by status."""
        task_manager.create_task(project_id, "Open")
        t2 = task_manager.create_task(project_id, "Closed")
        task_manager.close_task(t2.id)

        assert task_manager.count_tasks(project_id=project_id, status="open") == 1
        assert task_manager.count_tasks(project_id=project_id, status="closed") == 1

    def test_count_tasks_empty(self, task_manager, project_id) -> None:
        """Test counting when no tasks exist."""
        count = task_manager.count_tasks(project_id=project_id)
        assert count == 0

    def test_count_by_status(self, task_manager, project_id) -> None:
        """Test grouping task counts by status."""
        task_manager.create_task(project_id, "Open 1")
        task_manager.create_task(project_id, "Open 2")
        t3 = task_manager.create_task(project_id, "Closed")
        task_manager.close_task(t3.id)

        counts = task_manager.count_by_status(project_id=project_id)

        assert counts.get("open") == 2
        assert counts.get("closed") == 1

    def test_count_by_status_all_projects(self, task_manager, project_id) -> None:
        """Test counting by status without project filter."""
        task_manager.create_task(project_id, "Task")

        counts = task_manager.count_by_status()

        assert counts.get("open", 0) >= 1

    def test_count_ready_tasks(self, task_manager, dep_manager, project_id) -> None:
        """Test counting ready tasks."""
        task_manager.create_task(project_id, "Ready 1")
        task_manager.create_task(project_id, "Ready 2")
        blocked = task_manager.create_task(project_id, "Blocked")
        blocker = task_manager.create_task(project_id, "Blocker")
        dep_manager.add_dependency(blocked.id, blocker.id, "blocks")

        count = task_manager.count_ready_tasks(project_id=project_id)

        # Ready 1, Ready 2, and Blocker are ready; Blocked is blocked
        assert count == 3

    def test_count_blocked_tasks(self, task_manager, dep_manager, project_id) -> None:
        """Test counting blocked tasks."""
        blocked = task_manager.create_task(project_id, "Blocked")
        blocker = task_manager.create_task(project_id, "Blocker")
        dep_manager.add_dependency(blocked.id, blocker.id, "blocks")

        count = task_manager.count_blocked_tasks(project_id=project_id)

        assert count == 1

    # =========================================================================
    # Task.to_brief Tests
    # =========================================================================

    def test_task_to_brief(self, task_manager, project_id) -> None:
        """Test Task.to_brief returns minimal fields."""
        task = task_manager.create_task(
            project_id,
            "Full Task",
            description="Long description",
            priority=1,
            task_type="bug",
            labels=["urgent"],
            assignee="alice",
        )

        brief = task.to_brief()

        # Should include these fields
        assert brief["id"] == task.id
        assert brief["title"] == "Full Task"
        assert brief["status"] == "open"
        assert brief["priority"] == 1
        assert brief["task_type"] == "bug"
        assert brief["parent_task_id"] is None
        assert "created_at" in brief
        assert "updated_at" in brief

        # Should NOT include these fields
        assert "description" not in brief
        assert "labels" not in brief

    # =========================================================================
    # Change Listener Tests
    # =========================================================================

    def test_change_listener_called_on_create(self, task_manager, project_id) -> None:
        """Test change listener is called when creating a task."""
        listener_called = []

        def listener():
            listener_called.append(True)

        task_manager.add_change_listener(listener)
        task_manager.create_task(project_id, "Task")

        assert len(listener_called) == 1

    def test_change_listener_called_on_update(self, task_manager, project_id) -> None:
        """Test change listener is called when updating a task."""
        task = task_manager.create_task(project_id, "Task")

        listener_called = []

        def listener():
            listener_called.append(True)

        task_manager.add_change_listener(listener)
        task_manager.update_task(task.id, title="Updated")

        assert len(listener_called) == 1

    def test_change_listener_called_on_delete(self, task_manager, project_id) -> None:
        """Test change listener is called when deleting a task."""
        task = task_manager.create_task(project_id, "Task")

        listener_called = []

        def listener():
            listener_called.append(True)

        task_manager.add_change_listener(listener)
        task_manager.delete_task(task.id)

        assert len(listener_called) == 1

    def test_change_listener_error_does_not_break_operation(self, task_manager, project_id) -> None:
        """Test that listener errors don't break task operations."""

        def failing_listener():
            raise RuntimeError("Listener failed!")

        task_manager.add_change_listener(failing_listener)

        # Should not raise, operation should succeed
        task = task_manager.create_task(project_id, "Task")
        assert task.id is not None

    # =========================================================================
    # Create Task with All Fields Tests
    # =========================================================================

    def test_create_task_with_all_fields(self, task_manager, project_id, session_manager) -> None:
        """Test creating task with all possible fields."""
        # Create a session first (foreign key constraint)
        session = session_manager.register(
            external_id="test-ext-id",
            machine_id="test-machine",
            source="claude",
            project_id=project_id,
        )

        task = task_manager.create_task(
            project_id=project_id,
            title="Complete Task",
            description="Full description",
            parent_task_id=None,
            created_in_session_id=session.id,
            priority=1,
            task_type="feature",
            assignee="developer",
            labels=["important"],
            category="Unit tests",
            complexity_score=5,
            estimated_subtasks=3,
            expansion_context="More context",
            validation_criteria="All tests pass",
            use_external_validator=True,
            workflow_name="dev-workflow",
            verification="npm test passes",
            sequence_order=1,
        )

        assert task.title == "Complete Task"
        assert task.description == "Full description"
        assert task.created_in_session_id == session.id
        assert task.priority == 1
        assert task.task_type == "feature"
        assert task.assignee == "developer"
        assert task.labels == ["important"]
        assert task.category == "Unit tests"
        assert task.complexity_score == 5
        assert task.estimated_subtasks == 3
        assert task.expansion_context == "More context"
        assert task.validation_criteria == "All tests pass"
        assert task.use_external_validator is True
        assert task.workflow_name == "dev-workflow"
        assert task.verification == "npm test passes"
        assert task.sequence_order == 1
        # Validation status should be pending when criteria is set
        assert task.validation_status == "pending"


@pytest.mark.integration
class TestNormalizePriority:
    """Test the normalize_priority helper function."""

    def test_normalize_priority_none(self) -> None:
        """Test None priority returns 999."""
        from gobby.storage.tasks import normalize_priority

        assert normalize_priority(None) == 999

    def test_normalize_priority_named_string(self) -> None:
        """Test named priority strings are converted correctly."""
        from gobby.storage.tasks import normalize_priority

        assert normalize_priority("critical") == 0
        assert normalize_priority("high") == 1
        assert normalize_priority("medium") == 2
        assert normalize_priority("low") == 3
        assert normalize_priority("CRITICAL") == 0  # Case insensitive
        assert normalize_priority("High") == 1

    def test_normalize_priority_numeric_string(self) -> None:
        """Test numeric strings are parsed."""
        from gobby.storage.tasks import normalize_priority

        assert normalize_priority("1") == 1
        assert normalize_priority("5") == 5

    def test_normalize_priority_invalid_string(self) -> None:
        """Test invalid string returns 999."""
        from gobby.storage.tasks import normalize_priority

        assert normalize_priority("invalid") == 999
        assert normalize_priority("urgent") == 999  # Not in PRIORITY_MAP

    def test_normalize_priority_integer(self) -> None:
        """Test integer values are returned as-is."""
        from gobby.storage.tasks import normalize_priority

        assert normalize_priority(1) == 1
        assert normalize_priority(5) == 5
        assert normalize_priority(0) == 0


@pytest.mark.integration
class TestOrderTasksHierarchically:
    """Test the order_tasks_hierarchically helper function."""

    def test_order_empty_list(self) -> None:
        """Test ordering empty list returns empty list."""
        from gobby.storage.tasks import order_tasks_hierarchically

        result = order_tasks_hierarchically([])
        assert result == []

    def test_order_single_task(self, task_manager, project_id) -> None:
        """Test ordering single task returns single task."""
        from gobby.storage.tasks import order_tasks_hierarchically

        task = task_manager.create_task(project_id, "Single")
        result = order_tasks_hierarchically([task])

        assert len(result) == 1
        assert result[0].id == task.id

    def test_order_orphan_parent_reference(self, task_manager, project_id) -> None:
        """Test task with parent_id not in result set is treated as root."""
        from gobby.storage.tasks import order_tasks_hierarchically

        parent = task_manager.create_task(project_id, "Parent")
        child = task_manager.create_task(project_id, "Child", parent_task_id=parent.id)

        # Only pass child, not parent - child should be treated as root
        result = order_tasks_hierarchically([child])

        assert len(result) == 1
        assert result[0].id == child.id


@pytest.mark.integration
class TestCreateTaskWithDecomposition:
    """Test create_task_with_decomposition returns task dict."""

    def test_create_simple_task(self, task_manager, project_id) -> None:
        """Test creating a simple task."""
        result = task_manager.create_task_with_decomposition(
            project_id=project_id,
            title="Simple Task",
            description="A simple description",
        )

        assert "task" in result
        assert result["task"]["title"] == "Simple Task"
        assert result["task"]["description"] == "A simple description"

    def test_create_task_with_all_fields(self, task_manager, project_id) -> None:
        """Test creating a task with all optional fields."""
        result = task_manager.create_task_with_decomposition(
            project_id=project_id,
            title="Full Task",
            description="Full description",
            priority=1,
            task_type="feature",
            labels=["backend", "urgent"],
            category="unit",
            validation_criteria="Tests must pass",
        )

        assert "task" in result
        assert result["task"]["title"] == "Full Task"
        assert result["task"]["priority"] == 1
        assert result["task"]["task_type"] == "feature"
        assert result["task"]["category"] == "unit"


@pytest.mark.integration
class TestUpdateTaskWithResult:
    """Test update_task_with_result returns task dict."""

    def test_update_description(self, task_manager, project_id) -> None:
        """Test updating task description."""
        task = task_manager.create_task(project_id, "Task")

        result = task_manager.update_task_with_result(task.id, description="Updated description")

        assert "task" in result
        assert result["task"]["description"] == "Updated description"

    def test_update_with_none_description(self, task_manager, project_id) -> None:
        """Test updating with None description clears it."""
        task = task_manager.create_task(project_id, "Task", description="Original")

        result = task_manager.update_task_with_result(task.id, description=None)

        assert "task" in result
        assert result["task"]["description"] is None


@pytest.mark.integration
class TestListTasksBranchCoverage:
    """Additional tests for branch coverage in list_tasks."""

    def test_list_tasks_with_single_status(self, task_manager, project_id) -> None:
        """Test filtering with a single status string (not a list)."""
        task_manager.create_task(project_id, "Open Task")

        tasks = task_manager.list_tasks(project_id=project_id, status="open")

        assert len(tasks) == 1
        assert tasks[0].status == "open"

    def test_list_tasks_with_parent_filter(self, task_manager, project_id) -> None:
        """Test filtering tasks by parent_task_id."""
        parent = task_manager.create_task(project_id, "Parent")
        task_manager.create_task(project_id, "Child 1", parent_task_id=parent.id)
        task_manager.create_task(project_id, "Child 2", parent_task_id=parent.id)
        task_manager.create_task(project_id, "Orphan")

        tasks = task_manager.list_tasks(project_id=project_id, parent_task_id=parent.id)

        assert len(tasks) == 2
        for t in tasks:
            assert t.parent_task_id == parent.id


@pytest.mark.integration
class TestCreateTaskWithDecompositionParentTask:
    """Test create_task_with_decomposition with parent task."""

    def test_create_with_parent(self, task_manager, project_id) -> None:
        """Test creating a task with a parent."""
        parent = task_manager.create_task(project_id, "Parent")
        result = task_manager.create_task_with_decomposition(
            project_id=project_id,
            title="Child Task",
            parent_task_id=parent.id,
        )

        assert "task" in result
        assert result["task"]["parent_task_id"] == parent.id


@pytest.mark.integration
class TestPathCacheComputation:
    """Test path_cache computation functions for task renumbering.

    Note: create_task now auto-assigns seq_num and path_cache, so these tests
    verify the compute_path_cache and update_descendant_paths functions work
    correctly with auto-assigned values.
    """

    def test_compute_path_cache_root_task(self, task_manager, project_id) -> None:
        """Test path computation for a root task (no parent)."""
        task = task_manager.create_task(project_id=project_id, title="Root Task")
        # seq_num is auto-assigned to 1 for first task
        path = task_manager.compute_path_cache(task.id)
        assert path == "1"

    def test_compute_path_cache_child_task(self, task_manager, project_id) -> None:
        """Test path computation for a child task."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )
        # seq_nums auto-assigned: parent=1, child=2
        path = task_manager.compute_path_cache(child.id)
        assert path == "1.2"

    def test_compute_path_cache_deep_hierarchy(self, task_manager, project_id) -> None:
        """Test path computation for deeply nested tasks."""
        root = task_manager.create_task(project_id=project_id, title="Root")
        level1 = task_manager.create_task(
            project_id=project_id, title="Level 1", parent_task_id=root.id
        )
        level2 = task_manager.create_task(
            project_id=project_id, title="Level 2", parent_task_id=level1.id
        )
        level3 = task_manager.create_task(
            project_id=project_id, title="Level 3", parent_task_id=level2.id
        )

        # seq_nums auto-assigned: root=1, level1=2, level2=3, level3=4
        path = task_manager.compute_path_cache(level3.id)
        assert path == "1.2.3.4"

    def test_compute_path_cache_task_not_found(self, task_manager) -> None:
        """Test path computation for non-existent task."""
        path = task_manager.compute_path_cache("nonexistent-id")
        assert path is None

    def test_compute_path_cache_handles_null_seq_num(
        self, task_manager, project_id, temp_db
    ) -> None:
        """Test path computation returns None when seq_num is NULL (legacy data)."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        # Simulate legacy data by clearing the seq_num
        temp_db.execute("UPDATE tasks SET seq_num = NULL WHERE id = ?", (task.id,))

        path = task_manager.compute_path_cache(task.id)
        assert path is None

    def test_compute_path_cache_parent_null_seq_num(
        self, task_manager, project_id, temp_db
    ) -> None:
        """Test path computation returns None when parent has NULL seq_num."""
        parent = task_manager.create_task(project_id=project_id, title="Parent")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=parent.id
        )
        # Simulate legacy data - parent has NULL seq_num
        temp_db.execute("UPDATE tasks SET seq_num = NULL WHERE id = ?", (parent.id,))

        path = task_manager.compute_path_cache(child.id)
        assert path is None

    def test_update_path_cache(self, task_manager, project_id, temp_db) -> None:
        """Test update_path_cache stores the computed path."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        # Clear the path_cache to test update_path_cache works
        temp_db.execute("UPDATE tasks SET path_cache = NULL WHERE id = ?", (task.id,))

        path = task_manager.update_path_cache(task.id)
        assert path == "1"

        # Verify it was stored in the database
        row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (task.id,))
        assert row["path_cache"] == "1"

    def test_update_path_cache_with_null_seq_num(self, task_manager, project_id, temp_db) -> None:
        """Test update_path_cache when seq_num is NULL returns None."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        # Simulate legacy data
        temp_db.execute("UPDATE tasks SET seq_num = NULL WHERE id = ?", (task.id,))

        path = task_manager.update_path_cache(task.id)
        assert path is None

    def test_update_descendant_paths(self, task_manager, project_id, temp_db) -> None:
        """Test update_descendant_paths updates entire subtree."""
        root = task_manager.create_task(project_id=project_id, title="Root")
        child1 = task_manager.create_task(
            project_id=project_id, title="Child 1", parent_task_id=root.id
        )
        child2 = task_manager.create_task(
            project_id=project_id, title="Child 2", parent_task_id=root.id
        )
        grandchild = task_manager.create_task(
            project_id=project_id, title="Grandchild", parent_task_id=child1.id
        )

        # Clear path_cache values to test update_descendant_paths
        temp_db.execute("UPDATE tasks SET path_cache = NULL WHERE project_id = ?", (project_id,))

        # Update all paths starting from root
        count = task_manager.update_descendant_paths(root.id)
        assert count == 4

        # Verify all paths - seq_nums are auto-assigned: root=1, child1=2, child2=3, gc=4
        root_row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (root.id,))
        assert root_row["path_cache"] == "1"

        child1_row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (child1.id,))
        assert child1_row["path_cache"] == "1.2"

        child2_row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (child2.id,))
        assert child2_row["path_cache"] == "1.3"

        grandchild_row = temp_db.fetchone(
            "SELECT path_cache FROM tasks WHERE id = ?", (grandchild.id,)
        )
        assert grandchild_row["path_cache"] == "1.2.4"

    def test_update_descendant_paths_with_null_seq_num(
        self, task_manager, project_id, temp_db
    ) -> None:
        """Test update_descendant_paths skips tasks with NULL seq_num."""
        root = task_manager.create_task(project_id=project_id, title="Root")
        child = task_manager.create_task(
            project_id=project_id, title="Child", parent_task_id=root.id
        )

        # Clear path_cache and simulate legacy data - child has NULL seq_num
        temp_db.execute("UPDATE tasks SET path_cache = NULL WHERE project_id = ?", (project_id,))
        temp_db.execute("UPDATE tasks SET seq_num = NULL WHERE id = ?", (child.id,))

        count = task_manager.update_descendant_paths(root.id)
        # Root succeeds, child fails (no seq_num)
        assert count == 1

        root_row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (root.id,))
        assert root_row["path_cache"] == "1"

        child_row = temp_db.fetchone("SELECT path_cache FROM tasks WHERE id = ?", (child.id,))
        assert child_row["path_cache"] is None

    def test_to_dict_includes_seq_num_and_path_cache(self, task_manager, project_id) -> None:
        """Test that to_dict() includes seq_num and path_cache fields."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        data = task.to_dict()

        # First task gets seq_num=1 and path_cache="1"
        assert "seq_num" in data
        assert data["seq_num"] == 1
        assert "path_cache" in data
        assert data["path_cache"] == "1"

    def test_to_brief_includes_seq_num_and_path_cache(self, task_manager, project_id) -> None:
        """Test that to_brief() includes seq_num and path_cache fields."""
        task = task_manager.create_task(project_id=project_id, title="Task")
        brief = task.to_brief()

        # First task gets seq_num=1 and path_cache="1"
        assert "seq_num" in brief
        assert brief["seq_num"] == 1
        assert "path_cache" in brief
        assert brief["path_cache"] == "1"
