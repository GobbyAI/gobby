"""Integration tests for worktree lifecycle.

These tests verify the full worktree lifecycle with real database operations,
including creation, status transitions, and cleanup.
"""

from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager, Worktree, WorktreeStatus


class WorktreeSetup(TypedDict):
    worktrees: list[Worktree]
    project2: Project


# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture
def temp_db(hub_db: HubDatabase) -> HubDatabase:
    """Create a temporary database for testing."""
    return hub_db


@pytest.fixture
def project_manager(temp_db: HubDatabase) -> LocalProjectManager:
    """Create a project manager."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def session_manager(temp_db: HubDatabase) -> SessionManager:
    """Create a session manager."""
    return SessionManager(temp_db)


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    """Create a task manager."""
    return LocalTaskManager(temp_db)


@pytest.fixture
def worktree_manager(temp_db: HubDatabase) -> LocalWorktreeManager:
    """Create a worktree manager."""
    return LocalWorktreeManager(temp_db)


@pytest.fixture
def project(project_manager: LocalProjectManager) -> Project:
    """Create a test project."""
    return project_manager.create(
        name="test-project",
        repo_path="/tmp/test-repo",
        github_url="https://github.com/test/test-project",
    )


@pytest.fixture
def session(session_manager: SessionManager, project: Project) -> Session:
    """Create a test session."""
    return session_manager.register(
        machine_id="test-machine",
        source="claude",
        project_id=project.id,
        external_id="ext-test-session",
        title="Test Session",
    )


@pytest.fixture
def task(task_manager: LocalTaskManager, project: Project) -> Task:
    """Create a test task."""
    return task_manager.create_task(
        project_id=project.id,
        title="Test Task",
        description="A test task for worktree tests",
    )


class TestWorktreeCreation:
    """Integration tests for worktree creation."""

    def test_create_minimal_worktree(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Create a worktree with minimal required fields."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/test",
            worktree_path="/tmp/worktrees/test",
        )

        assert worktree.id.startswith("wt-")
        assert worktree.project_id == project.id
        assert worktree.branch_name == "feature/test"
        assert worktree.worktree_path == "/tmp/worktrees/test"
        assert worktree.base_branch == "main"
        assert worktree.status == WorktreeStatus.ACTIVE.value
        assert worktree.task_id is None
        assert worktree.agent_session_id is None

    def test_create_worktree_with_all_fields(
        self, worktree_manager: LocalWorktreeManager, project: Project, session: Session, task: Task
    ) -> None:
        """Create a worktree with all optional fields."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/full",
            worktree_path="/tmp/worktrees/full",
            base_branch="develop",
            task_id=task.id,
            agent_session_id=session.id,
        )

        assert worktree.base_branch == "develop"
        assert worktree.task_id == task.id
        assert worktree.agent_session_id == session.id

    def test_create_multiple_worktrees(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Create multiple worktrees for the same project."""
        worktree1 = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/one",
            worktree_path="/tmp/worktrees/one",
        )
        worktree2 = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/two",
            worktree_path="/tmp/worktrees/two",
        )
        worktree3 = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/three",
            worktree_path="/tmp/worktrees/three",
        )

        assert len({worktree1.id, worktree2.id, worktree3.id}) == 3

        # All should be retrievable
        all_worktrees = worktree_manager.list_worktrees(project_id=project.id)
        assert len(all_worktrees) == 3


class TestWorktreeRetrieval:
    """Integration tests for worktree retrieval."""

    def test_get_by_id(self, worktree_manager: LocalWorktreeManager, project: Project) -> None:
        """Retrieve worktree by ID."""
        created = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/get-by-id",
            worktree_path="/tmp/worktrees/get-by-id",
        )

        retrieved = worktree_manager.get(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.branch_name == created.branch_name

    def test_get_nonexistent(self, worktree_manager: LocalWorktreeManager) -> None:
        """Get returns None for nonexistent worktree."""
        result = worktree_manager.get("wt-nonexistent")
        assert result is None

    def test_get_by_path(self, worktree_manager: LocalWorktreeManager, project: Project) -> None:
        """Retrieve worktree by path."""
        created = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/by-path",
            worktree_path="/tmp/worktrees/by-path",
        )

        retrieved = worktree_manager.get_by_path("/tmp/worktrees/by-path")

        assert retrieved is not None
        assert retrieved.id == created.id

    def test_get_by_branch(self, worktree_manager: LocalWorktreeManager, project: Project) -> None:
        """Retrieve worktree by project and branch."""
        created = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/by-branch",
            worktree_path="/tmp/worktrees/by-branch",
        )

        retrieved = worktree_manager.get_by_branch(project.id, "feature/by-branch")

        assert retrieved is not None
        assert retrieved.id == created.id

    def test_get_by_task(
        self, worktree_manager: LocalWorktreeManager, project: Project, task: Task
    ) -> None:
        """Retrieve worktree by task ID."""
        created = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/by-task",
            worktree_path="/tmp/worktrees/by-task",
            task_id=task.id,
        )

        retrieved = worktree_manager.get_by_task(task.id)

        assert retrieved is not None
        assert retrieved.id == created.id


class TestWorktreeListing:
    """Integration tests for worktree listing with filters."""

    @pytest.fixture
    def setup_worktrees(
        self,
        worktree_manager: LocalWorktreeManager,
        project_manager: LocalProjectManager,
        project: Project,
        session: Session,
    ) -> WorktreeSetup:
        """Create a variety of worktrees for listing tests."""
        # Create another project
        project2 = project_manager.create(
            name="other-project",
            repo_path="/tmp/other-project",
        )

        worktrees = []

        # Project 1 worktrees
        worktrees.append(
            worktree_manager.create(
                project_id=project.id,
                branch_name="feature/active1",
                worktree_path="/tmp/wt/active1",
                agent_session_id=session.id,
            )
        )
        worktrees.append(
            worktree_manager.create(
                project_id=project.id,
                branch_name="feature/active2",
                worktree_path="/tmp/wt/active2",
            )
        )

        # Project 2 worktree
        worktrees.append(
            worktree_manager.create(
                project_id=project2.id,
                branch_name="feature/other",
                worktree_path="/tmp/wt/other",
            )
        )

        return {"worktrees": worktrees, "project2": project2}

    def test_list_all(
        self, worktree_manager: LocalWorktreeManager, setup_worktrees: WorktreeSetup
    ) -> None:
        """List all worktrees without filters."""
        worktrees = worktree_manager.list_worktrees()
        assert len(worktrees) == 3

    def test_list_by_project(
        self,
        worktree_manager: LocalWorktreeManager,
        project: Project,
        setup_worktrees: WorktreeSetup,
    ) -> None:
        """List worktrees filtered by project."""
        worktrees = worktree_manager.list_worktrees(project_id=project.id)
        assert len(worktrees) == 2
        for wt in worktrees:
            assert wt.project_id == project.id

    def test_list_by_status(
        self,
        worktree_manager: LocalWorktreeManager,
        project: Project,
        setup_worktrees: WorktreeSetup,
    ) -> None:
        """List worktrees filtered by status."""
        # Mark one as stale
        wt = setup_worktrees["worktrees"][0]
        worktree_manager.mark_stale(wt.id)

        active = worktree_manager.list_worktrees(status=WorktreeStatus.ACTIVE.value)
        stale = worktree_manager.list_worktrees(status=WorktreeStatus.STALE.value)

        assert len(active) == 2
        assert len(stale) == 1
        assert stale[0].id == wt.id

    def test_list_by_session(
        self,
        worktree_manager: LocalWorktreeManager,
        session: Session,
        setup_worktrees: WorktreeSetup,
    ) -> None:
        """List worktrees filtered by agent session."""
        worktrees = worktree_manager.list_worktrees(agent_session_id=session.id)
        assert len(worktrees) == 1
        assert worktrees[0].agent_session_id == session.id

    def test_list_with_limit(
        self, worktree_manager: LocalWorktreeManager, setup_worktrees: WorktreeSetup
    ) -> None:
        """List worktrees with limit."""
        worktrees = worktree_manager.list_worktrees(limit=2)
        assert len(worktrees) == 2

    def test_list_combined_filters(
        self,
        worktree_manager: LocalWorktreeManager,
        project: Project,
        session: Session,
        setup_worktrees: WorktreeSetup,
    ) -> None:
        """List worktrees with multiple filters."""
        worktrees = worktree_manager.list_worktrees(
            project_id=project.id,
            status=WorktreeStatus.ACTIVE.value,
            agent_session_id=session.id,
        )
        assert len(worktrees) == 1


class TestWorktreeStatusTransitions:
    """Integration tests for worktree status transitions."""

    def test_claim_and_release(
        self,
        worktree_manager: LocalWorktreeManager,
        project: Project,
        session_manager: SessionManager,
        project_manager: LocalProjectManager,
    ) -> None:
        """Test claiming and releasing a worktree."""
        # Create a fresh project and session for this test
        proj = project_manager.create(
            name="claim-test-project",
            repo_path="/tmp/claim-test",
        )
        sess = session_manager.register(
            machine_id="test-machine",
            source="claude",
            project_id=proj.id,
            external_id="ext-claim-session",
            title="Claim Session",
        )

        worktree = worktree_manager.create(
            project_id=proj.id,
            branch_name="feature/claim-test",
            worktree_path="/tmp/worktrees/claim-test",
        )

        # Initially no session
        assert worktree.agent_session_id is None

        # Claim
        claimed = worktree_manager.claim(worktree.id, sess.id)
        assert claimed is not None
        assert claimed.agent_session_id == sess.id

        # Verify persistence
        retrieved = worktree_manager.get(worktree.id)
        assert retrieved.agent_session_id == sess.id

        # Release
        released = worktree_manager.release(worktree.id)
        assert released is not None
        assert released.agent_session_id is None

        # Verify persistence
        retrieved = worktree_manager.get(worktree.id)
        assert retrieved.agent_session_id is None

    def test_mark_stale(self, worktree_manager: LocalWorktreeManager, project: Project) -> None:
        """Test marking a worktree as stale."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/mark-stale",
            worktree_path="/tmp/worktrees/mark-stale",
        )

        assert worktree.status == WorktreeStatus.ACTIVE.value

        stale = worktree_manager.mark_stale(worktree.id)
        assert stale is not None
        assert stale.status == WorktreeStatus.STALE.value

        # Verify persistence
        retrieved = worktree_manager.get(worktree.id)
        assert retrieved.status == WorktreeStatus.STALE.value

    def test_mark_merged(self, worktree_manager: LocalWorktreeManager, project: Project) -> None:
        """Test marking a worktree as merged."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/mark-merged",
            worktree_path="/tmp/worktrees/mark-merged",
        )

        merged = worktree_manager.mark_merged(worktree.id)
        assert merged is not None
        assert merged.status == WorktreeStatus.MERGED.value
        assert merged.merged_at is not None

        # Verify persistence
        retrieved = worktree_manager.get(worktree.id)
        assert retrieved.status == WorktreeStatus.MERGED.value
        assert retrieved.merged_at is not None

    def test_mark_abandoned(self, worktree_manager: LocalWorktreeManager, project: Project) -> None:
        """Test marking a worktree as abandoned."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/mark-abandoned",
            worktree_path="/tmp/worktrees/mark-abandoned",
        )

        abandoned = worktree_manager.mark_abandoned(worktree.id)
        assert abandoned is not None
        assert abandoned.status == WorktreeStatus.ABANDONED.value

        # Verify persistence
        retrieved = worktree_manager.get(worktree.id)
        assert retrieved.status == WorktreeStatus.ABANDONED.value

    def test_full_lifecycle(
        self,
        worktree_manager: LocalWorktreeManager,
        project: Project,
        session_manager: SessionManager,
        project_manager: LocalProjectManager,
    ) -> None:
        """Test complete worktree lifecycle: active → claimed → released → merged."""
        # Create a fresh project and session for this test
        proj = project_manager.create(
            name="lifecycle-project",
            repo_path="/tmp/lifecycle",
        )
        sess = session_manager.register(
            machine_id="test-machine",
            source="claude",
            project_id=proj.id,
            external_id="ext-lifecycle-session",
            title="Lifecycle Session",
        )

        # Create (active)
        worktree = worktree_manager.create(
            project_id=proj.id,
            branch_name="feature/lifecycle",
            worktree_path="/tmp/worktrees/lifecycle",
        )
        assert worktree.status == WorktreeStatus.ACTIVE.value

        # Claim
        worktree = worktree_manager.claim(worktree.id, sess.id)
        assert worktree.agent_session_id == sess.id

        # Work complete - release
        worktree = worktree_manager.release(worktree.id)
        assert worktree.agent_session_id is None

        # Mark merged
        worktree = worktree_manager.mark_merged(worktree.id)
        assert worktree.status == WorktreeStatus.MERGED.value
        assert worktree.merged_at is not None


class TestWorktreeUpdate:
    """Integration tests for worktree updates."""

    def test_update_single_field(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Update a single field."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/update-single",
            worktree_path="/tmp/worktrees/update-single",
        )

        updated = worktree_manager.update(worktree.id, status=WorktreeStatus.STALE.value)
        assert updated is not None
        assert updated.status == WorktreeStatus.STALE.value

        # Verify persistence
        retrieved = worktree_manager.get(worktree.id)
        assert retrieved.status == WorktreeStatus.STALE.value

    def test_update_multiple_fields(
        self, worktree_manager: LocalWorktreeManager, project: Project, session: Session, task: Task
    ) -> None:
        """Update multiple fields at once."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/update-multi",
            worktree_path="/tmp/worktrees/update-multi",
        )

        updated = worktree_manager.update(
            worktree.id,
            task_id=task.id,
            agent_session_id=session.id,
            status=WorktreeStatus.STALE.value,
        )

        assert updated.task_id == task.id
        assert updated.agent_session_id == session.id
        assert updated.status == WorktreeStatus.STALE.value

    def test_update_nonexistent(self, worktree_manager: LocalWorktreeManager) -> None:
        """Update returns None for nonexistent worktree."""
        result = worktree_manager.update("wt-nonexistent", status="stale")
        assert result is None

    def test_update_updates_timestamp(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Update modifies updated_at timestamp."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/update-ts",
            worktree_path="/tmp/worktrees/update-ts",
        )
        original_updated_at = worktree.updated_at

        updated = worktree_manager.update(worktree.id, status=WorktreeStatus.STALE.value)
        assert updated.updated_at != original_updated_at


class TestWorktreeDeletion:
    """Integration tests for worktree deletion."""

    def test_delete_existing(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Delete an existing worktree."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/delete-me",
            worktree_path="/tmp/worktrees/delete-me",
        )

        result = worktree_manager.delete(worktree.id)
        assert result is True

        # Verify deletion
        retrieved = worktree_manager.get(worktree.id)
        assert retrieved is None

    def test_delete_nonexistent(self, worktree_manager: LocalWorktreeManager) -> None:
        """Delete returns False for nonexistent worktree."""
        result = worktree_manager.delete("wt-nonexistent")
        assert result is False


class TestStaleWorktreeDetection:
    """Integration tests for stale worktree detection and cleanup."""

    def test_find_stale_worktrees(
        self, temp_db: HubDatabase, project_manager: LocalProjectManager
    ) -> None:
        """Find worktrees that haven't been updated recently."""
        # Need fresh managers to manipulate timestamps
        wm = LocalWorktreeManager(temp_db)
        proj = project_manager.create(
            name="stale-project",
            repo_path="/tmp/stale",
        )

        # Create worktrees
        wm.create(
            project_id=proj.id,
            branch_name="feature/recent",
            worktree_path="/tmp/wt/recent",
        )

        old = wm.create(
            project_id=proj.id,
            branch_name="feature/old",
            worktree_path="/tmp/wt/old",
        )

        # Manually set old worktree's updated_at to 48 hours ago
        old_time = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        temp_db.execute(
            "UPDATE worktrees SET updated_at = ? WHERE id = ?",
            (old_time, old.id),
        )

        # Find stale worktrees (default 24 hours)
        stale = wm.find_stale(proj.id, hours=24)

        assert len(stale) == 1
        assert stale[0].id == old.id

    def test_find_stale_custom_hours(
        self, temp_db: HubDatabase, project_manager: LocalProjectManager
    ) -> None:
        """Find stale worktrees with custom hours threshold."""
        wm = LocalWorktreeManager(temp_db)
        proj = project_manager.create(
            name="stale-custom-project",
            repo_path="/tmp/stale-custom",
        )

        worktree = wm.create(
            project_id=proj.id,
            branch_name="feature/custom",
            worktree_path="/tmp/wt/custom",
        )

        # Set updated_at to 12 hours ago
        old_time = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
        temp_db.execute(
            "UPDATE worktrees SET updated_at = ? WHERE id = ?",
            (old_time, worktree.id),
        )

        # Should not be stale at 24 hours
        stale_24 = wm.find_stale(proj.id, hours=24)
        assert len(stale_24) == 0

        # Should be stale at 6 hours
        stale_6 = wm.find_stale(proj.id, hours=6)
        assert len(stale_6) == 1

    def test_cleanup_stale_dry_run(
        self, temp_db: HubDatabase, project_manager: LocalProjectManager
    ) -> None:
        """Cleanup stale in dry run mode doesn't modify worktrees."""
        wm = LocalWorktreeManager(temp_db)
        proj = project_manager.create(
            name="cleanup-dry-project",
            repo_path="/tmp/cleanup-dry",
        )

        worktree = wm.create(
            project_id=proj.id,
            branch_name="feature/cleanup-dry",
            worktree_path="/tmp/wt/cleanup-dry",
        )

        # Make it stale
        old_time = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        temp_db.execute(
            "UPDATE worktrees SET updated_at = ? WHERE id = ?",
            (old_time, worktree.id),
        )

        # Cleanup with dry_run=True (default)
        stale = wm.cleanup_stale(proj.id, hours=24, dry_run=True)
        assert len(stale) == 1

        # Status should not have changed
        retrieved = wm.get(worktree.id)
        assert retrieved is not None
        assert retrieved.status == WorktreeStatus.ACTIVE.value

    def test_cleanup_stale_marks_abandoned(
        self, temp_db: HubDatabase, project_manager: LocalProjectManager
    ) -> None:
        """Cleanup stale marks worktrees as abandoned."""
        wm = LocalWorktreeManager(temp_db)
        proj = project_manager.create(
            name="cleanup-abandon-project",
            repo_path="/tmp/cleanup-abandon",
        )

        worktree = wm.create(
            project_id=proj.id,
            branch_name="feature/cleanup-abandon",
            worktree_path="/tmp/wt/cleanup-abandon",
        )

        # Make it stale
        old_time = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        temp_db.execute(
            "UPDATE worktrees SET updated_at = ? WHERE id = ?",
            (old_time, worktree.id),
        )

        # Cleanup with dry_run=False
        stale = wm.cleanup_stale(proj.id, hours=24, dry_run=False)
        assert len(stale) == 1

        # Status should be abandoned
        retrieved = wm.get(worktree.id)
        assert retrieved is not None
        assert retrieved.status == WorktreeStatus.ABANDONED.value


class TestWorktreeStatistics:
    """Integration tests for worktree statistics."""

    def test_count_by_status_empty(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Count by status returns empty dict for no worktrees."""
        counts = worktree_manager.count_by_status(project.id)
        assert counts == {}

    def test_count_by_status_with_data(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Count by status returns correct counts."""
        # Create worktrees with different statuses
        worktree_manager.create(
            project_id=project.id,
            branch_name="feature/count1",
            worktree_path="/tmp/wt/count1",
        )
        wt2 = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/count2",
            worktree_path="/tmp/wt/count2",
        )
        wt3 = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/count3",
            worktree_path="/tmp/wt/count3",
        )

        # Change some statuses
        worktree_manager.mark_stale(wt2.id)
        worktree_manager.mark_merged(wt3.id)

        counts = worktree_manager.count_by_status(project.id)

        assert counts["active"] == 1
        assert counts["stale"] == 1
        assert counts["merged"] == 1


class TestWorktreeDataIntegrity:
    """Integration tests for worktree data integrity."""

    def test_worktree_to_dict(
        self, worktree_manager: LocalWorktreeManager, project: Project, session: Session, task: Task
    ) -> None:
        """Worktree.to_dict returns complete data."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/to-dict",
            worktree_path="/tmp/worktrees/to-dict",
            base_branch="develop",
            task_id=task.id,
            agent_session_id=session.id,
        )

        data = worktree.to_dict()

        assert data["id"] == worktree.id
        assert data["project_id"] == project.id
        assert data["branch_name"] == "feature/to-dict"
        assert data["worktree_path"] == "/tmp/worktrees/to-dict"
        assert data["base_branch"] == "develop"
        assert data["task_id"] == task.id
        assert data["agent_session_id"] == session.id
        assert data["status"] == WorktreeStatus.ACTIVE.value
        assert data["created_at"] is not None
        assert data["updated_at"] is not None
        assert data["merged_at"] is None

    def test_worktree_timestamps_are_strings(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """Worktree timestamps are stored as ISO strings."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/timestamps",
            worktree_path="/tmp/worktrees/timestamps",
        )

        assert isinstance(worktree.created_at, str)
        assert isinstance(worktree.updated_at, str)

        # Should be parseable as ISO datetime
        from datetime import datetime

        datetime.fromisoformat(worktree.created_at.replace("Z", "+00:00"))
        datetime.fromisoformat(worktree.updated_at.replace("Z", "+00:00"))

    def test_merged_at_only_set_on_merge(
        self, worktree_manager: LocalWorktreeManager, project: Project
    ) -> None:
        """merged_at is only set when marking as merged."""
        worktree = worktree_manager.create(
            project_id=project.id,
            branch_name="feature/merged-at",
            worktree_path="/tmp/worktrees/merged-at",
        )

        assert worktree.merged_at is None

        # Mark stale - merged_at stays None
        worktree_manager.mark_stale(worktree.id)
        stale = worktree_manager.get(worktree.id)
        assert stale.merged_at is None

        # Mark merged - merged_at is set
        worktree_manager.mark_merged(worktree.id)
        merged = worktree_manager.get(worktree.id)
        assert merged.merged_at is not None
