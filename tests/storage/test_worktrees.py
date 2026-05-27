"""Tests for local worktree storage manager."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from gobby.storage.worktrees import LocalWorktreeManager, Worktree, WorktreeStatus

pytestmark = pytest.mark.unit


class TestWorktreeStatus:
    """Tests for WorktreeStatus enum."""

    def test_values(self) -> None:
        """WorktreeStatus has expected values."""
        assert WorktreeStatus.ACTIVE.value == "active"
        assert WorktreeStatus.STALE.value == "stale"
        assert WorktreeStatus.MERGED.value == "merged"
        assert WorktreeStatus.ABANDONED.value == "abandoned"

    def test_is_string_enum(self) -> None:
        """WorktreeStatus values are strings."""
        for status in WorktreeStatus:
            assert isinstance(status.value, str)


class TestWorktree:
    """Tests for Worktree dataclass."""

    def test_from_row(self) -> None:
        """from_row creates Worktree from database row."""
        row = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": "gt-task123",
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "agent_session_id": "sess-xyz",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

        worktree = Worktree.from_row(row)

        assert worktree.id == "wt-123456"
        assert worktree.project_id == "proj-abc"
        assert worktree.task_id == "gt-task123"
        assert worktree.branch_name == "feature/test"
        assert worktree.worktree_path == "/path/to/worktree"
        assert worktree.base_branch == "main"
        assert worktree.agent_session_id == "sess-xyz"
        assert worktree.status == "active"
        assert worktree.merged_at is None

    def test_to_dict(self) -> None:
        """to_dict converts Worktree to dictionary."""
        worktree = Worktree(
            id="wt-123456",
            project_id="proj-abc",
            task_id="gt-task123",
            branch_name="feature/test",
            worktree_path="/path/to/worktree",
            base_branch="main",
            agent_session_id="sess-xyz",
            status="active",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
            merged_at=None,
        )

        result = worktree.to_dict()

        assert result["id"] == "wt-123456"
        assert result["project_id"] == "proj-abc"
        assert result["task_id"] == "gt-task123"
        assert result["branch_name"] == "feature/test"
        assert result["worktree_path"] == "/path/to/worktree"
        assert result["base_branch"] == "main"
        assert result["agent_session_id"] == "sess-xyz"
        assert result["status"] == "active"
        assert result["created_at"] == "2025-01-01T00:00:00+00:00"
        assert result["updated_at"] == "2025-01-01T00:00:00+00:00"
        assert result["merged_at"] is None


class TestLocalWorktreeManagerInit:
    """Tests for LocalWorktreeManager initialization."""

    def test_init_stores_db(self) -> None:
        """Manager stores database reference."""
        mock_db = MagicMock()

        manager = LocalWorktreeManager(db=mock_db)

        assert manager.db is mock_db


class TestLocalWorktreeManagerCreate:
    """Tests for LocalWorktreeManager.create method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_create_minimal(self, manager, mock_db) -> None:
        """Create worktree with minimal required fields."""
        worktree = manager.create(
            project_id="proj-abc",
            branch_name="feature/test",
            worktree_path="/path/to/worktree",
        )

        assert worktree.project_id == "proj-abc"
        assert worktree.branch_name == "feature/test"
        assert worktree.worktree_path == "/path/to/worktree"
        assert worktree.base_branch == "main"
        assert worktree.task_id is None
        assert worktree.agent_session_id is None
        assert worktree.status == "active"
        assert worktree.id.startswith("wt-")
        mock_db.execute.assert_called_once()

    def test_create_with_all_fields(self, manager, mock_db) -> None:
        """Create worktree with all optional fields."""
        worktree = manager.create(
            project_id="proj-abc",
            branch_name="feature/test",
            worktree_path="/path/to/worktree",
            base_branch="develop",
            task_id="gt-task123",
            agent_session_id="sess-xyz",
        )

        assert worktree.base_branch == "develop"
        assert worktree.task_id == "gt-task123"
        assert worktree.agent_session_id == "sess-xyz"

    def test_create_generates_unique_id(self, manager, mock_db) -> None:
        """Create generates unique worktree ID."""
        worktree1 = manager.create(
            project_id="proj-abc",
            branch_name="feature/one",
            worktree_path="/path/one",
        )
        worktree2 = manager.create(
            project_id="proj-abc",
            branch_name="feature/two",
            worktree_path="/path/two",
        )

        assert worktree1.id != worktree2.id
        assert worktree1.id.startswith("wt-")
        assert worktree2.id.startswith("wt-")

    def test_create_sets_timestamps(self, manager, mock_db) -> None:
        """Create sets created_at and updated_at timestamps."""
        worktree = manager.create(
            project_id="proj-abc",
            branch_name="feature/test",
            worktree_path="/path/to/worktree",
        )

        # Timestamps should be recent ISO format
        assert worktree.created_at is not None
        assert worktree.updated_at is not None
        assert worktree.created_at == worktree.updated_at


class TestLocalWorktreeManagerGet:
    """Tests for LocalWorktreeManager.get method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_get_existing(self, manager, mock_db) -> None:
        """Get returns Worktree for existing ID."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "agent_session_id": None,
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

        worktree = manager.get("wt-123456")

        assert worktree is not None
        assert worktree.id == "wt-123456"
        mock_db.fetchone.assert_called_once()

    def test_get_not_found(self, manager, mock_db) -> None:
        """Get returns None for non-existent ID."""
        mock_db.fetchone.return_value = None

        worktree = manager.get("wt-nonexistent")

        assert worktree is None


class TestLocalWorktreeManagerGetBy:
    """Tests for LocalWorktreeManager get_by_* methods."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    @pytest.fixture
    def mock_row(self):
        """Create mock database row."""
        return {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": "gt-task123",
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "agent_session_id": "sess-xyz",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

    def test_get_by_path_found(self, manager, mock_db, mock_row) -> None:
        """get_by_path returns worktree for existing path."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.get_by_path("/path/to/worktree")

        assert worktree is not None
        assert worktree.worktree_path == "/path/to/worktree"

    def test_get_by_path_not_found(self, manager, mock_db) -> None:
        """get_by_path returns None for non-existent path."""
        mock_db.fetchone.return_value = None

        worktree = manager.get_by_path("/nonexistent/path")

        assert worktree is None

    def test_get_by_branch_found(self, manager, mock_db, mock_row) -> None:
        """get_by_branch returns worktree for project/branch."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.get_by_branch("proj-abc", "feature/test")

        assert worktree is not None
        assert worktree.branch_name == "feature/test"

    def test_get_by_branch_not_found(self, manager, mock_db) -> None:
        """get_by_branch returns None for non-existent branch."""
        mock_db.fetchone.return_value = None

        worktree = manager.get_by_branch("proj-abc", "nonexistent")

        assert worktree is None

    def test_get_by_task_found(self, manager, mock_db, mock_row) -> None:
        """get_by_task returns worktree for task ID."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.get_by_task("gt-task123")

        assert worktree is not None
        assert worktree.task_id == "gt-task123"

    def test_get_by_task_not_found(self, manager, mock_db) -> None:
        """get_by_task returns None for non-existent task."""
        mock_db.fetchone.return_value = None

        worktree = manager.get_by_task("gt-nonexistent")

        assert worktree is None


class TestLocalWorktreeManagerList:
    """Tests for LocalWorktreeManager.list method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_list_no_filters(self, manager, mock_db) -> None:
        """List returns all worktrees without filters."""
        mock_db.fetchall.return_value = [
            {
                "id": "wt-1",
                "project_id": "proj-abc",
                "task_id": None,
                "branch_name": "feature/one",
                "worktree_path": "/path/one",
                "base_branch": "main",
                "agent_session_id": None,
                "status": "active",
                "created_at": "2025-01-01T00:00:00+00:00",
                "updated_at": "2025-01-01T00:00:00+00:00",
                "merged_at": None,
            },
            {
                "id": "wt-2",
                "project_id": "proj-xyz",
                "task_id": None,
                "branch_name": "feature/two",
                "worktree_path": "/path/two",
                "base_branch": "main",
                "agent_session_id": None,
                "status": "stale",
                "created_at": "2025-01-01T00:00:00+00:00",
                "updated_at": "2025-01-01T00:00:00+00:00",
                "merged_at": None,
            },
        ]

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 2
        assert worktrees[0].id == "wt-1"
        assert worktrees[1].id == "wt-2"

    def test_list_filter_by_project(self, manager, mock_db) -> None:
        """List filters by project_id."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(project_id="proj-abc")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "project_id = %s" in query
        assert "proj-abc" in params

    def test_list_filter_by_status(self, manager, mock_db) -> None:
        """List filters by status."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(status="active")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "status = %s" in query
        assert "active" in params

    def test_list_filter_by_session(self, manager, mock_db) -> None:
        """List filters by agent_session_id."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(agent_session_id="sess-xyz")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "agent_session_id = %s" in query
        assert "sess-xyz" in params

    def test_list_with_limit(self, manager, mock_db) -> None:
        """List respects limit parameter."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(limit=10)

        call_args = mock_db.fetchall.call_args
        params = call_args[0][1]
        assert params[-1] == 10  # Limit is always last param

    def test_list_combines_filters(self, manager, mock_db) -> None:
        """List combines multiple filters."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(project_id="proj-abc", status="active", limit=5)

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "project_id = %s" in query
        assert "status = %s" in query
        assert "proj-abc" in params
        assert "active" in params


class TestLocalWorktreeManagerUpdate:
    """Tests for LocalWorktreeManager.update method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_update_no_fields_returns_current(self, manager, mock_db) -> None:
        """Update with no fields returns current worktree."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "agent_session_id": None,
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

        worktree = manager.update("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_not_called()

    def test_update_single_field(self, manager, mock_db) -> None:
        """Update modifies specified field."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "agent_session_id": None,
            "status": "stale",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-02T00:00:00+00:00",
            "merged_at": None,
        }

        worktree = manager.update("wt-123456", status="stale")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "status = %s" in query
        assert "updated_at = %s" in query  # Should auto-update timestamp

    def test_update_multiple_fields(self, manager, mock_db) -> None:
        """Update modifies multiple fields."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": "gt-task999",
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "agent_session_id": "sess-new",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-02T00:00:00+00:00",
            "merged_at": None,
        }

        worktree = manager.update("wt-123456", task_id="gt-task999", agent_session_id="sess-new")

        assert worktree is not None
        mock_db.execute.assert_called_once()

    def test_update_not_found(self, manager, mock_db) -> None:
        """Update returns None for non-existent worktree."""
        mock_db.fetchone.return_value = None

        worktree = manager.update("wt-nonexistent", status="stale")

        assert worktree is None


class TestLocalWorktreeManagerDelete:
    """Tests for LocalWorktreeManager.delete method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_delete_existing(self, manager, mock_db) -> None:
        """Delete returns True for existing worktree."""
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_db.execute.return_value = mock_cursor

        result = manager.delete("wt-123456")

        assert result is True
        mock_db.execute.assert_called_once()

    def test_delete_not_found(self, manager, mock_db) -> None:
        """Delete returns False for non-existent worktree."""
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_db.execute.return_value = mock_cursor

        result = manager.delete("wt-nonexistent")

        assert result is False


class TestLocalWorktreeManagerStatusTransitions:
    """Tests for LocalWorktreeManager status transition methods."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    @pytest.fixture
    def mock_row(self):
        """Create mock database row."""
        return {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "agent_session_id": None,
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

    def test_claim_sets_session_id(self, manager, mock_db, mock_row) -> None:
        """claim sets agent_session_id."""
        mock_row["agent_session_id"] = "sess-new"
        mock_db.fetchone.return_value = mock_row

        worktree = manager.claim("wt-123456", "sess-new")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "agent_session_id = %s" in query

    def test_release_clears_session_id(self, manager, mock_db, mock_row) -> None:
        """release clears agent_session_id."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.release("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        # First param is None (agent_session_id), followed by updated_at
        assert None in params

    def test_mark_stale_sets_status(self, manager, mock_db, mock_row) -> None:
        """mark_stale sets status to stale."""
        mock_row["status"] = "stale"
        mock_db.fetchone.return_value = mock_row

        worktree = manager.mark_stale("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert WorktreeStatus.STALE.value in params

    def test_mark_merged_sets_status_and_timestamp(self, manager, mock_db, mock_row) -> None:
        """mark_merged sets status to merged and merged_at timestamp."""
        mock_row["status"] = "merged"
        mock_row["merged_at"] = "2025-01-02T00:00:00+00:00"
        mock_db.fetchone.return_value = mock_row

        before = datetime.now(UTC)
        worktree = manager.mark_merged("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert WorktreeStatus.MERGED.value in params
        cleanup_after = datetime.fromisoformat(params[2])
        assert before <= cleanup_after <= datetime.now(UTC) + timedelta(seconds=1)

    def test_mark_abandoned_sets_status(self, manager, mock_db, mock_row) -> None:
        """mark_abandoned sets status to abandoned."""
        mock_row["status"] = "abandoned"
        mock_db.fetchone.return_value = mock_row

        worktree = manager.mark_abandoned("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert WorktreeStatus.ABANDONED.value in params


class TestLocalWorktreeManagerFindStale:
    """Tests for LocalWorktreeManager.find_stale method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_find_stale_default_hours(self, manager, mock_db) -> None:
        """find_stale uses default 24 hours threshold."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc")

        mock_db.fetchall.assert_called_once()
        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        assert "updated_at <" in query
        assert "status = %s" in query

    def test_find_stale_custom_hours(self, manager, mock_db) -> None:
        """find_stale uses custom hours threshold."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc", hours=48)

        mock_db.fetchall.assert_called_once()
        assert mock_db.fetchall.call_count == 1
        assert mock_db.fetchall.call_args is not None

    def test_find_stale_returns_worktrees(self, manager, mock_db) -> None:
        """find_stale returns list of stale worktrees."""
        mock_db.fetchall.return_value = [
            {
                "id": "wt-stale1",
                "project_id": "proj-abc",
                "task_id": None,
                "branch_name": "feature/old",
                "worktree_path": "/path/old",
                "base_branch": "main",
                "agent_session_id": None,
                "status": "active",
                "created_at": "2024-12-01T00:00:00+00:00",
                "updated_at": "2024-12-01T00:00:00+00:00",
                "merged_at": None,
            },
        ]

        stale = manager.find_stale("proj-abc")

        assert len(stale) == 1
        assert stale[0].id == "wt-stale1"

    def test_find_stale_respects_limit(self, manager, mock_db) -> None:
        """find_stale respects limit parameter."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc", limit=5)

        call_args = mock_db.fetchall.call_args
        params = call_args[0][1]
        assert params[-1] == 5


class TestLocalWorktreeManagerCleanupStale:
    """Tests for LocalWorktreeManager.cleanup_stale method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_cleanup_stale_dry_run_default(self, manager, mock_db) -> None:
        """cleanup_stale defaults to dry run."""
        mock_db.fetchall.return_value = [
            {
                "id": "wt-stale1",
                "project_id": "proj-abc",
                "task_id": None,
                "branch_name": "feature/old",
                "worktree_path": "/path/old",
                "base_branch": "main",
                "agent_session_id": None,
                "status": "active",
                "created_at": "2024-12-01T00:00:00+00:00",
                "updated_at": "2024-12-01T00:00:00+00:00",
                "merged_at": None,
            },
        ]

        stale = manager.cleanup_stale("proj-abc")

        assert len(stale) == 1
        # Should not call execute to update (dry_run=True)
        mock_db.execute.assert_not_called()

    def test_cleanup_stale_marks_abandoned(self, manager, mock_db) -> None:
        """cleanup_stale marks worktrees as abandoned when not dry run."""
        # Setup: fetchall returns stale worktrees
        mock_db.fetchall.return_value = [
            {
                "id": "wt-stale1",
                "project_id": "proj-abc",
                "task_id": None,
                "branch_name": "feature/old",
                "worktree_path": "/path/old",
                "base_branch": "main",
                "agent_session_id": None,
                "status": "active",
                "created_at": "2024-12-01T00:00:00+00:00",
                "updated_at": "2024-12-01T00:00:00+00:00",
                "merged_at": None,
            },
        ]
        # Setup: fetchone returns updated worktree for mark_abandoned
        mock_db.fetchone.return_value = {
            "id": "wt-stale1",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/old",
            "worktree_path": "/path/old",
            "base_branch": "main",
            "agent_session_id": None,
            "status": "abandoned",
            "created_at": "2024-12-01T00:00:00+00:00",
            "updated_at": "2025-01-02T00:00:00+00:00",
            "merged_at": None,
        }

        stale = manager.cleanup_stale("proj-abc", dry_run=False)

        assert len(stale) == 1
        # Should have called execute to update status
        assert mock_db.execute.call_count >= 1


class TestLocalWorktreeManagerCountByStatus:
    """Tests for LocalWorktreeManager.count_by_status method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_count_by_status_empty(self, manager, mock_db) -> None:
        """count_by_status returns empty dict for no worktrees."""
        mock_db.fetchall.return_value = []

        counts = manager.count_by_status("proj-abc")

        assert counts == {}

    def test_count_by_status_with_data(self, manager, mock_db) -> None:
        """count_by_status returns status counts."""
        mock_db.fetchall.return_value = [
            {"status": "active", "count": 5},
            {"status": "stale", "count": 2},
            {"status": "merged", "count": 10},
        ]

        counts = manager.count_by_status("proj-abc")

        assert counts == {"active": 5, "stale": 2, "merged": 10}

    def test_count_by_status_queries_project(self, manager, mock_db) -> None:
        """count_by_status filters by project_id."""
        mock_db.fetchall.return_value = []

        manager.count_by_status("proj-abc")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "project_id = %s" in query
        assert "proj-abc" in params
