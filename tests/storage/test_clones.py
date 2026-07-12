"""Tests for local clone storage manager."""

import threading
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.storage.clones import Clone, CloneStatus, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

CLONE_CREATED_AT = "2026-01-22T00:00:00+00:00"
CLONE_UPDATED_AT = "2026-01-22T00:00:00+00:00"


def _clone_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "clone-123",
        "project_id": "proj-abc",
        "branch_name": "feature/test",
        "clone_path": "/tmp/clones/test",
        "base_branch": "main",
        "task_id": None,
        "agent_session_id": None,
        "status": "active",
        "remote_url": None,
        "last_sync_at": None,
        "cleanup_after": None,
        "created_at": CLONE_CREATED_AT,
        "updated_at": CLONE_UPDATED_AT,
    }
    row.update(overrides)
    return row


class TestCloneStatus:
    """Tests for CloneStatus enum."""

    def test_values(self) -> None:
        """CloneStatus has expected values."""
        assert CloneStatus.ACTIVE.value == "active"
        assert CloneStatus.SYNCING.value == "syncing"
        assert CloneStatus.STALE.value == "stale"
        assert CloneStatus.CLEANUP.value == "cleanup"

    def test_is_string_enum(self) -> None:
        """CloneStatus values are strings."""
        for status in CloneStatus:
            assert isinstance(status.value, str)


class TestClone:
    """Tests for Clone dataclass."""

    def test_from_row(self) -> None:
        """from_row creates Clone from database row."""
        row = {
            "id": "clone-123456",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": "gt-task123",
            "agent_session_id": "sess-xyz",
            "status": "active",
            "remote_url": "https://github.com/user/repo.git",
            "last_sync_at": "2026-01-22T12:00:00+00:00",
            "cleanup_after": "2026-01-23T12:00:00+00:00",
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        clone = Clone.from_row(row)

        assert clone.id == "clone-123456"
        assert clone.project_id == "proj-abc"
        assert clone.branch_name == "feature/test"
        assert clone.clone_path == "/tmp/clones/test"
        assert clone.base_branch == "main"
        assert clone.task_id == "gt-task123"
        assert clone.agent_session_id == "sess-xyz"
        assert clone.status == "active"
        assert clone.remote_url == "https://github.com/user/repo.git"
        assert clone.last_sync_at == datetime(2026, 1, 22, 12, tzinfo=UTC)
        assert clone.cleanup_after == datetime(2026, 1, 23, 12, tzinfo=UTC)

    def test_from_row_with_nulls(self) -> None:
        """from_row handles NULL values correctly."""
        row = {
            "id": "clone-123456",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": None,
            "status": "active",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        clone = Clone.from_row(row)

        assert clone.task_id is None
        assert clone.agent_session_id is None
        assert clone.remote_url is None
        assert clone.last_sync_at is None
        assert clone.cleanup_after is None

    def test_to_dict(self) -> None:
        """to_dict converts Clone to dictionary."""
        clone = Clone(
            id="clone-123456",
            project_id="proj-abc",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="gt-task123",
            agent_session_id="sess-xyz",
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at="2026-01-22T12:00:00+00:00",
            cleanup_after="2026-01-23T12:00:00+00:00",
            created_at="2026-01-22T00:00:00+00:00",
            updated_at="2026-01-22T00:00:00+00:00",
        )

        result = clone.to_dict()

        assert result["id"] == "clone-123456"
        assert result["project_id"] == "proj-abc"
        assert result["branch_name"] == "feature/test"
        assert result["clone_path"] == "/tmp/clones/test"
        assert result["base_branch"] == "main"
        assert result["task_id"] == "gt-task123"
        assert result["agent_session_id"] == "sess-xyz"
        assert result["status"] == "active"
        assert result["remote_url"] == "https://github.com/user/repo.git"


class TestCloneToBrief:
    """Tests for Clone.to_brief() slim representation."""

    def test_to_brief_has_fewer_fields_than_to_dict(self) -> None:
        """to_brief returns fewer fields than to_dict."""
        clone = Clone(
            id="clone-123456",
            project_id="proj-abc",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="gt-task123",
            agent_session_id="sess-xyz",
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at="2026-01-22T12:00:00+00:00",
            cleanup_after="2026-01-23T12:00:00+00:00",
            created_at="2026-01-22T00:00:00+00:00",
            updated_at="2026-01-22T00:00:00+00:00",
        )

        brief = clone.to_brief()
        full = clone.to_dict()
        assert len(brief) < len(full)

    def test_to_brief_essential_fields_present(self) -> None:
        """to_brief includes essential fields for list operations."""
        clone = Clone(
            id="clone-brief",
            project_id="proj-abc",
            branch_name="feature/slim",
            clone_path="/tmp/clones/slim",
            base_branch="main",
            task_id="gt-task1",
            agent_session_id="sess-1",
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="2026-01-22T00:00:00+00:00",
            updated_at="2026-01-22T00:00:00+00:00",
        )

        brief = clone.to_brief()
        assert brief["id"] == "clone-brief"
        assert brief["branch_name"] == "feature/slim"
        assert brief["clone_path"] == "/tmp/clones/slim"
        assert brief["status"] == "active"
        assert brief["task_id"] == "gt-task1"
        assert brief["agent_session_id"] == "sess-1"
        assert brief["created_at"] == "2026-01-22T00:00:00+00:00"
        assert brief["updated_at"] == "2026-01-22T00:00:00+00:00"

    def test_to_brief_excludes_internal_fields(self) -> None:
        """to_brief omits fields not needed in list views."""
        clone = Clone(
            id="clone-exc",
            project_id="proj-abc",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at="2026-01-22T12:00:00+00:00",
            cleanup_after="2026-01-23T12:00:00+00:00",
            created_at="2026-01-22T00:00:00+00:00",
            updated_at="2026-01-22T00:00:00+00:00",
        )

        brief = clone.to_brief()
        assert "project_id" not in brief
        assert "remote_url" not in brief
        assert "last_sync_at" not in brief
        assert "cleanup_after" not in brief
        assert "base_branch" not in brief


class TestLocalCloneManagerInit:
    """Tests for LocalCloneManager initialization."""

    def test_init_stores_db(self) -> None:
        """Manager stores database reference."""
        mock_db = MagicMock()

        manager = LocalCloneManager(db=mock_db)

        assert manager.db is mock_db


class TestLocalCloneManagerCreate:
    """Tests for LocalCloneManager.create method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_create_minimal(self, manager, mock_db) -> None:
        """Create clone with minimal required fields."""
        clone = manager.create(
            project_id="proj-abc",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
        )

        assert clone.project_id == "proj-abc"
        assert clone.branch_name == "feature/test"
        assert clone.clone_path == "/tmp/clones/test"
        assert clone.base_branch == "main"
        assert clone.task_id is None
        assert clone.agent_session_id is None
        assert clone.status == "active"
        assert str(uuid.UUID(clone.id)) == clone.id
        mock_db.execute.assert_called_once()

    def test_create_with_all_fields(self, manager, mock_db) -> None:
        """Create clone with all optional fields."""
        clone = manager.create(
            project_id="proj-abc",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="develop",
            task_id="gt-task123",
            agent_session_id="sess-xyz",
            remote_url="https://github.com/user/repo.git",
            cleanup_after="2026-01-23T12:00:00+00:00",
        )

        assert clone.base_branch == "develop"
        assert clone.task_id == "gt-task123"
        assert clone.agent_session_id == "sess-xyz"
        assert clone.remote_url == "https://github.com/user/repo.git"
        assert clone.cleanup_after == datetime(2026, 1, 23, 12, tzinfo=UTC)

    def test_create_generates_unique_id(self, manager, mock_db) -> None:
        """Create generates unique clone ID."""
        clone1 = manager.create(
            project_id="proj-abc",
            branch_name="feature/one",
            clone_path="/tmp/clones/one",
        )
        clone2 = manager.create(
            project_id="proj-abc",
            branch_name="feature/two",
            clone_path="/tmp/clones/two",
        )

        assert clone1.id != clone2.id
        assert str(uuid.UUID(clone1.id)) == clone1.id
        assert str(uuid.UUID(clone2.id)) == clone2.id


class TestLocalCloneManagerGet:
    """Tests for LocalCloneManager.get method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_get_existing(self, manager, mock_db) -> None:
        """Get returns Clone for existing ID."""
        mock_db.fetchone.return_value = {
            "id": "clone-123456",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": None,
            "status": "active",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        clone = manager.get("clone-123456")

        assert clone is not None
        assert clone.id == "clone-123456"
        mock_db.fetchone.assert_called_once()

    def test_get_nonexistent(self, manager, mock_db) -> None:
        """Get returns None for nonexistent ID."""
        mock_db.fetchone.return_value = None

        clone = manager.get("clone-nonexistent")

        assert clone is None

    def test_get_hides_terminal_cleanup_record(self, manager, mock_db) -> None:
        """Terminal cleanup rows behave as removed from normal lookups."""
        mock_db.fetchone.return_value = _clone_row(status=CloneStatus.CLEANUP.value)

        clone = manager.get("clone-123456")

        assert clone is None


class TestLocalCloneManagerGetByTask:
    """Tests for LocalCloneManager.get_by_task method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_get_by_task_existing(self, manager, mock_db) -> None:
        """Get clone linked to task."""
        mock_db.fetchone.return_value = {
            "id": "clone-123456",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": "gt-task123",
            "agent_session_id": None,
            "status": "active",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        clone = manager.get_by_task("gt-task123")

        assert clone is not None
        assert clone.task_id == "gt-task123"

    def test_get_by_task_nonexistent(self, manager, mock_db) -> None:
        """Returns None if no clone linked to task."""
        mock_db.fetchone.return_value = None

        clone = manager.get_by_task("gt-nonexistent")

        assert clone is None


class TestLocalCloneManagerList:
    """Tests for LocalCloneManager.list_clones method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_list_all(self, manager, mock_db) -> None:
        """List returns all clones."""
        mock_db.fetchall.return_value = [
            {
                "id": "clone-1",
                "project_id": "proj-abc",
                "branch_name": "feature/one",
                "clone_path": "/tmp/clones/one",
                "base_branch": "main",
                "task_id": None,
                "agent_session_id": None,
                "status": "active",
                "remote_url": None,
                "last_sync_at": None,
                "cleanup_after": None,
                "created_at": "2026-01-22T00:00:00+00:00",
                "updated_at": "2026-01-22T00:00:00+00:00",
            },
            {
                "id": "clone-2",
                "project_id": "proj-abc",
                "branch_name": "feature/two",
                "clone_path": "/tmp/clones/two",
                "base_branch": "main",
                "task_id": None,
                "agent_session_id": None,
                "status": "stale",
                "remote_url": None,
                "last_sync_at": None,
                "cleanup_after": None,
                "created_at": "2026-01-22T00:00:00+00:00",
                "updated_at": "2026-01-22T00:00:00+00:00",
            },
        ]

        clones = manager.list_clones()

        assert len(clones) == 2
        assert clones[0].id == "clone-1"
        assert clones[1].id == "clone-2"

    def test_list_with_filters(self, manager, mock_db) -> None:
        """List with project_id and status filters."""
        mock_db.fetchall.return_value = []

        manager.list_clones(project_id="proj-abc", status="active")

        # Verify query includes filters
        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        assert "project_id = %s" in query
        assert "status = %s" in query

    def test_list_excludes_terminal_cleanup_records(self, manager, mock_db) -> None:
        """Normal clone listings never expose terminal cleanup rows."""
        mock_db.fetchall.return_value = []

        manager.list_clones()

        query, params = mock_db.fetchall.call_args.args
        assert "status != %s" in query
        assert params[0] == CloneStatus.CLEANUP.value


class TestLocalCloneManagerUpdate:
    """Tests for LocalCloneManager.update method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_update_status(self, manager, mock_db) -> None:
        """Update clone status."""
        mock_db.fetchone.return_value = _clone_row(status="stale")

        manager.update("clone-123", status="stale")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "UPDATE clones SET" in query
        assert "status = %s" in query

    def test_update_agent_session(self, manager, mock_db) -> None:
        """Update clone agent session."""
        mock_db.fetchone.return_value = _clone_row(agent_session_id="sess-new")

        manager.update("clone-123", agent_session_id="sess-new")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "agent_session_id = %s" in query

    def test_update_last_sync(self, manager, mock_db) -> None:
        """Update clone last_sync_at."""
        mock_db.fetchone.return_value = _clone_row(last_sync_at="2026-01-22T12:00:00+00:00")

        manager.update("clone-123", last_sync_at="2026-01-22T12:00:00+00:00")

        mock_db.execute.assert_called_once()
        assert mock_db.execute.call_count == 1
        assert mock_db.execute.call_args is not None


class TestLocalCloneManagerDelete:
    """Tests for LocalCloneManager.delete method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_delete(self, manager, mock_db) -> None:
        """Delete removes clone record."""
        # Mock cursor with rowcount
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_db.execute.return_value = mock_cursor

        result = manager.delete("clone-123")

        assert result is True
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "DELETE FROM clones" in query
        assert "id = %s" in query


class TestLocalCloneManagerStatusMethods:
    """Tests for LocalCloneManager status helper methods."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_mark_syncing(self, manager, mock_db) -> None:
        """mark_syncing updates status to syncing."""
        mock_db.fetchone.return_value = _clone_row(status="syncing")

        manager.mark_syncing("clone-123")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert "syncing" in params

    def test_mark_stale(self, manager, mock_db) -> None:
        """mark_stale updates status to stale."""
        mock_db.fetchone.return_value = _clone_row(status="stale")

        manager.mark_stale("clone-123")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert "stale" in params

    def test_mark_cleanup(self, manager, mock_db) -> None:
        """mark_cleanup updates status to cleanup."""
        mock_db.fetchone.return_value = _clone_row(status="cleanup")

        manager.mark_cleanup("clone-123")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert "cleanup" in params

    def test_record_sync(self, manager, mock_db) -> None:
        """record_sync updates status to active and sets last_sync_at."""
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": None,
            "status": "active",
            "remote_url": None,
            "last_sync_at": "2026-01-22T12:00:00+00:00",
            "cleanup_after": None,
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        result = manager.record_sync("clone-123")

        assert result is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "status = %s" in query
        assert "last_sync_at = %s" in query

    def test_claim(self, manager, mock_db) -> None:
        """claim sets agent_session_id."""
        mock_db.execute.return_value.rowcount = 1
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": "sess-1",
            "status": "active",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        result = manager.claim("clone-123", "sess-1")

        assert result is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "agent_session_id = %s" in query
        assert "agent_session_id IS NULL OR agent_session_id = %s" in query
        assert call_args[0][1][0] == "sess-1"
        assert call_args[0][1][2:] == (
            "clone-123",
            "sess-1",
            CloneStatus.CLEANUP.value,
        )

    def test_claim_returns_none_when_owned_by_another_session(self, manager, mock_db) -> None:
        """claim reports a conditional update that matched no rows."""
        mock_db.execute.return_value.rowcount = 0

        result = manager.claim("clone-123", "sess-1")

        assert result is None
        mock_db.fetchone.assert_not_called()

    def test_release(self, manager, mock_db) -> None:
        """release clears agent_session_id."""
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": None,
            "status": "active",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        result = manager.release("clone-123")

        assert result is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        # None should be in the params (clearing agent_session_id)
        assert None in params


class TestLocalCloneManagerClaimAtomicity:
    """Regression tests for competing clone claims."""

    def test_concurrent_claims_have_exactly_one_winning_session(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
        session_manager: SessionManager,
    ) -> None:
        """A single conditional update prevents competing owners from both winning."""
        manager = LocalCloneManager(temp_db)
        clone = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/atomic-clone-claim",
            clone_path="/tmp/clones/atomic-clone-claim",
        )
        sessions = [
            session_manager.register(
                external_id=f"atomic-clone-claim-{index}",
                machine_id="atomic-clone-claim-machine",
                source="codex",
                project_id=str(sample_project["id"]),
            )
            for index in range(2)
        ]
        barrier = threading.Barrier(2)
        winners: list[str] = []
        errors: list[BaseException] = []
        result_lock = threading.Lock()

        def claim(session_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                claimed = LocalCloneManager(temp_db).claim(clone.id, session_id)
                if claimed is not None:
                    with result_lock:
                        winners.append(session_id)
            except BaseException as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=claim, args=(session.id,)) for session in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(winners) == 1
        stored = manager.get(clone.id)
        assert stored is not None
        assert stored.agent_session_id == winners[0]


class TestLocalCloneManagerCountByStatus:
    """Tests for LocalCloneManager.count_by_status method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_count_by_status(self, manager, mock_db) -> None:
        """count_by_status returns counts grouped by status."""
        mock_db.fetchall.return_value = [
            {"status": "active", "count": 3},
            {"status": "stale", "count": 1},
            {"status": "syncing", "count": 2},
        ]

        result = manager.count_by_status("proj-abc")

        assert result == {"active": 3, "stale": 1, "syncing": 2}
        mock_db.fetchall.assert_called_once()
        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        assert "GROUP BY status" in query
        params = call_args[0][1]
        assert params == ("proj-abc",)

    def test_count_by_status_empty(self, manager, mock_db) -> None:
        """count_by_status returns empty dict when no clones."""
        mock_db.fetchall.return_value = []

        result = manager.count_by_status("proj-abc")

        assert result == {}


class TestLocalCloneManagerFindStale:
    """Tests for LocalCloneManager.find_stale method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_find_stale_returns_clones(self, manager, mock_db) -> None:
        """find_stale returns stale clones."""
        mock_db.fetchall.return_value = [
            {
                "id": "clone-1",
                "project_id": "proj-abc",
                "branch_name": "old-feature",
                "clone_path": "/tmp/clones/old",
                "base_branch": "main",
                "task_id": None,
                "agent_session_id": None,
                "status": "active",
                "remote_url": None,
                "last_sync_at": None,
                "cleanup_after": None,
                "created_at": "2026-01-20T00:00:00+00:00",
                "updated_at": "2026-01-20T00:00:00+00:00",
            },
        ]

        result = manager.find_stale("proj-abc", hours=24)

        assert len(result) == 1
        assert result[0].id == "clone-1"
        mock_db.fetchall.assert_called_once()
        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        assert "project_id = %s" in query
        assert "status = %s" in query
        assert "agent_session_id IS NULL" in query
        assert "updated_at < %s" in query
        assert "LIMIT %s" in query

    def test_find_stale_empty(self, manager, mock_db) -> None:
        """find_stale returns empty list when no stale clones."""
        mock_db.fetchall.return_value = []

        result = manager.find_stale("proj-abc", hours=48, limit=10)

        assert result == []

    def test_find_stale_custom_params(self, manager, mock_db) -> None:
        """find_stale passes custom hours and limit."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc", hours=72, limit=5)

        call_args = mock_db.fetchall.call_args
        params = call_args[0][1]
        # params: (project_id, status, cutoff, limit)
        assert params[0] == "proj-abc"
        assert params[1] == "active"
        # cutoff is an ISO timestamp (3rd param)
        assert params[3] == 5


class TestLocalCloneManagerCleanupStale:
    """Tests for LocalCloneManager.cleanup_stale method."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_cleanup_stale_dry_run(self, manager, mock_db) -> None:
        """cleanup_stale in dry_run returns stale clones without updating."""
        mock_db.fetchall.return_value = [
            {
                "id": "clone-1",
                "project_id": "proj-abc",
                "branch_name": "old-feature",
                "clone_path": "/tmp/clones/old",
                "base_branch": "main",
                "task_id": None,
                "agent_session_id": None,
                "status": "active",
                "remote_url": None,
                "last_sync_at": None,
                "cleanup_after": None,
                "created_at": "2026-01-20T00:00:00+00:00",
                "updated_at": "2026-01-20T00:00:00+00:00",
            },
        ]

        result = manager.cleanup_stale("proj-abc", hours=24, dry_run=True)

        assert len(result) == 1
        assert result[0].id == "clone-1"
        # In dry_run, only fetchall is called (from find_stale), no execute for updates
        mock_db.execute.assert_not_called()

    def test_cleanup_stale_actual(self, manager, mock_db) -> None:
        """cleanup_stale marks clones as stale when dry_run=False."""
        # find_stale fetchall
        mock_db.fetchall.return_value = [
            {
                "id": "clone-1",
                "project_id": "proj-abc",
                "branch_name": "old-feature",
                "clone_path": "/tmp/clones/old",
                "base_branch": "main",
                "task_id": None,
                "agent_session_id": None,
                "status": "active",
                "remote_url": None,
                "last_sync_at": None,
                "cleanup_after": None,
                "created_at": "2026-01-20T00:00:00+00:00",
                "updated_at": "2026-01-20T00:00:00+00:00",
            },
        ]
        # mark_stale -> update -> get (fetchone for the updated clone)
        mock_db.fetchone.return_value = {
            "id": "clone-1",
            "project_id": "proj-abc",
            "branch_name": "old-feature",
            "clone_path": "/tmp/clones/old",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": None,
            "status": "stale",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": "2026-01-20T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        result = manager.cleanup_stale("proj-abc", hours=24, dry_run=False)

        assert len(result) == 1
        assert result[0].status == "stale"
        # execute was called (from mark_stale -> update)
        mock_db.execute.assert_called()

    def test_cleanup_stale_empty(self, manager, mock_db) -> None:
        """cleanup_stale returns empty list when no stale clones."""
        mock_db.fetchall.return_value = []

        result = manager.cleanup_stale("proj-abc", hours=24, dry_run=False)

        assert result == []
        mock_db.execute.assert_not_called()


class TestLocalCloneManagerUpdateValidation:
    """Tests for LocalCloneManager.update field validation."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db):
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_update_no_fields(self, manager, mock_db) -> None:
        """Update with no fields returns existing clone."""
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": None,
            "status": "active",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": "2026-01-22T00:00:00+00:00",
            "updated_at": "2026-01-22T00:00:00+00:00",
        }

        result = manager.update("clone-123")

        assert result is not None
        mock_db.execute.assert_not_called()

    def test_update_invalid_field_raises(self, manager) -> None:
        """Update with invalid field name raises ValueError."""
        with pytest.raises(ValueError, match="Invalid field names"):
            manager.update("clone-123", invalid_field="value")
