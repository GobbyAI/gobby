"""Tests for local clone storage manager."""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.storage.clones import Clone, CloneStatus, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id.get_machine_id", return_value=MACHINE_ID):
        yield


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
        "machine_id": MACHINE_ID,
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
            "machine_id": MACHINE_ID,
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
            "machine_id": MACHINE_ID,
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
            machine_id=MACHINE_ID,
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
            machine_id=MACHINE_ID,
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
            machine_id=MACHINE_ID,
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
            machine_id=MACHINE_ID,
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
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = {
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        return db

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_create_minimal(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
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

    def test_create_detached(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        clone = manager.create(
            project_id="proj-abc",
            branch_name=None,
            clone_path="/tmp/clones/detached",
        )

        assert clone.branch_name is None
        assert mock_db.execute.call_args.args[1][3] is None

    def test_create_with_all_fields(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Create clone with all optional fields."""
        mock_db.fetchone.return_value = {"machine_id": MACHINE_ID}
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

    def test_create_generates_unique_id(
        self, manager: LocalCloneManager, mock_db: MagicMock
    ) -> None:
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
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_get_existing(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Get returns Clone for existing ID."""
        mock_db.fetchone.return_value = {
            "id": "clone-123456",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "machine_id": MACHINE_ID,
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

    def test_get_nonexistent(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Get returns None for nonexistent ID."""
        mock_db.fetchone.return_value = None

        clone = manager.get("clone-nonexistent")

        assert clone is None

    def test_get_hides_terminal_cleanup_record(
        self, manager: LocalCloneManager, mock_db: MagicMock
    ) -> None:
        """Terminal cleanup rows behave as removed from normal lookups."""
        mock_db.fetchone.return_value = _clone_row(status=CloneStatus.CLEANUP.value)

        clone = manager.get("clone-123456")

        assert clone is None


class TestLocalCloneManagerGetByTask:
    """Tests for LocalCloneManager.get_by_task method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_get_by_task_existing(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Get clone linked to task."""
        mock_db.fetchone.return_value = {
            "id": "clone-123456",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": "gt-task123",
            "machine_id": MACHINE_ID,
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

    def test_get_by_task_nonexistent(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Returns None if no clone linked to task."""
        mock_db.fetchone.return_value = None

        clone = manager.get_by_task("gt-nonexistent")

        assert clone is None

    def test_get_by_task_prefers_status_then_recency(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
    ) -> None:
        """Real storage prefers the newest active clone over newer dead clones."""
        task = LocalTaskManager(temp_db).create_task(
            project_id=str(sample_project["id"]),
            title="Clone ordering",
            validation_criteria="Storage fixture task; behavior asserted by the test.",
        )
        manager = LocalCloneManager(temp_db)
        older_active = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/older-active",
            clone_path="/tmp/gobby-older-active",
            task_id=task.id,
        )
        newer_active = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/newer-active",
            clone_path="/tmp/gobby-newer-active",
            task_id=task.id,
        )
        stale = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/stale",
            clone_path="/tmp/gobby-stale",
            task_id=task.id,
        )
        manager.mark_stale(stale.id)
        temp_db.execute(
            "UPDATE clones SET updated_at = %s WHERE id = %s",
            (datetime(2026, 1, 1, tzinfo=UTC), older_active.id),
        )
        temp_db.execute(
            "UPDATE clones SET updated_at = %s WHERE id = %s",
            (datetime(2026, 1, 2, tzinfo=UTC), newer_active.id),
        )
        temp_db.execute(
            "UPDATE clones SET updated_at = %s WHERE id = %s",
            (datetime(2026, 1, 3, tzinfo=UTC), stale.id),
        )

        clone = manager.get_by_task(task.id)

        assert clone is not None
        assert clone.id == newer_active.id


class TestLocalCloneManagerList:
    """Tests for LocalCloneManager.list_clones method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_list_all(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """List returns all clones."""
        mock_db.fetchall.return_value = [
            {
                "id": "clone-1",
                "project_id": "proj-abc",
                "branch_name": "feature/one",
                "clone_path": "/tmp/clones/one",
                "base_branch": "main",
                "task_id": None,
                "machine_id": MACHINE_ID,
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
                "machine_id": MACHINE_ID,
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

    def test_list_with_filters(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """List with project_id and status filters."""
        mock_db.fetchall.return_value = []

        manager.list_clones(project_id="proj-abc", status="active")

        # Verify query includes filters
        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        assert "project_id = %s" in query
        assert "status = %s" in query

    def test_list_excludes_terminal_cleanup_records(
        self, manager: LocalCloneManager, mock_db: MagicMock
    ) -> None:
        """Normal clone listings never expose terminal cleanup rows."""
        mock_db.fetchall.return_value = []

        manager.list_clones()

        query, params = mock_db.fetchall.call_args.args
        assert "status != %s" in query
        assert params[0] == CloneStatus.CLEANUP.value


class TestLocalCloneManagerUpdate:
    """Tests for LocalCloneManager.update method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_update_status(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Update clone status."""
        mock_db.fetchone.return_value = _clone_row(status="stale")

        manager.update("clone-123", status="stale")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "UPDATE clones SET" in query
        assert "status = %s" in query

    def test_update_agent_session(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Update clone agent session."""
        mock_db.fetchone.return_value = _clone_row(agent_session_id="sess-new")

        manager.update("clone-123", agent_session_id="sess-new")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "agent_session_id = %s" in query

    def test_update_last_sync(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Update clone last_sync_at."""
        mock_db.fetchone.return_value = _clone_row(last_sync_at="2026-01-22T12:00:00+00:00")

        manager.update("clone-123", last_sync_at="2026-01-22T12:00:00+00:00")

        mock_db.execute.assert_called_once()
        assert mock_db.execute.call_count == 1
        assert mock_db.execute.call_args is not None


class TestLocalCloneManagerDelete:
    """Tests for LocalCloneManager.delete method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_delete(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
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
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_mark_syncing(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """mark_syncing updates status to syncing."""
        mock_db.fetchone.return_value = _clone_row(status="syncing")

        manager.mark_syncing("clone-123")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert "syncing" in params

    def test_mark_stale(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """mark_stale updates status to stale."""
        mock_db.fetchone.return_value = _clone_row(status="stale")

        manager.mark_stale("clone-123")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert "stale" in params

    def test_mark_cleanup(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """mark_cleanup updates status to cleanup."""
        mock_db.fetchone.return_value = _clone_row(status="cleanup")

        manager.mark_cleanup("clone-123")

        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert "cleanup" in params

    def test_record_sync(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """record_sync updates status to active and sets last_sync_at."""
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "machine_id": MACHINE_ID,
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

    def test_claim(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """claim sets agent_session_id."""
        mock_db.execute.return_value.rowcount = 1
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "machine_id": MACHINE_ID,
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
            require_machine_id(),
            "sess-1",
            CloneStatus.CLEANUP.value,
        )

    def test_claim_returns_none_when_owned_by_another_session(
        self, manager: LocalCloneManager, mock_db: MagicMock
    ) -> None:
        """claim reports a conditional update that matched no rows."""
        mock_db.execute.return_value.rowcount = 0
        mock_db.fetchone.return_value = {"machine_id": MACHINE_ID}

        result = manager.claim("clone-123", "sess-1")

        assert result is None


class TestLocalCloneManagerRegisterAdopted:
    """Tests for idempotent clone registration during adoption."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        db = MagicMock(spec=HubDatabase)
        db.execute.return_value.fetchone.return_value = {
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        return db

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        return LocalCloneManager(mock_db)

    def test_get_by_path_any_status_includes_cleanup(
        self,
        manager: LocalCloneManager,
        mock_db: MagicMock,
    ) -> None:
        mock_db.fetchone.return_value = _clone_row(status=CloneStatus.CLEANUP.value)

        clone = manager.get_by_path_any_status("/tmp/clones/test")

        assert clone is not None
        assert clone.status == CloneStatus.CLEANUP.value
        assert mock_db.fetchone.call_args.args[1] == ("/tmp/clones/test", MACHINE_ID)

    @pytest.mark.parametrize("branch_name", ["feature/adopted", None])
    def test_registers_inspected_branch_or_detached_clone(
        self,
        manager: LocalCloneManager,
        mock_db: MagicMock,
        branch_name: str | None,
    ) -> None:
        mock_db.fetchone.return_value = None

        clone, registered = manager.register_adopted(
            project_id="proj-abc",
            branch_name=branch_name,
            clone_path="/tmp/clones/adopted",
            base_branch="main",
            remote_url="file:///tmp/source",
        )

        assert registered is True
        assert clone.branch_name == branch_name
        assert clone.remote_url == "file:///tmp/source"
        insert_values = mock_db.execute.call_args.args[1]
        assert insert_values[1] == "proj-abc"
        assert insert_values[3:6] == (branch_name, "/tmp/clones/adopted", "main")
        assert insert_values[9] == "file:///tmp/source"

    def test_rejects_existing_path_from_another_project(
        self,
        manager: LocalCloneManager,
        mock_db: MagicMock,
    ) -> None:
        mock_db.fetchone.return_value = _clone_row(project_id="other-project")

        with pytest.raises(ValueError, match="another project"):
            manager.register_adopted(
                project_id="proj-abc",
                branch_name="main",
                clone_path="/tmp/clones/test",
                base_branch="main",
                remote_url=None,
            )

        mock_db.execute.assert_not_called()

    def test_revives_cleanup_record_with_actual_metadata_and_cleared_ownership(
        self,
        manager: LocalCloneManager,
        mock_db: MagicMock,
    ) -> None:
        cleanup_row = _clone_row(
            branch_name="stale",
            task_id="task-1",
            agent_session_id="session-1",
            status=CloneStatus.CLEANUP.value,
            remote_url="https://stale.invalid/repo.git",
            last_sync_at=CLONE_UPDATED_AT,
            cleanup_after=CLONE_UPDATED_AT,
        )
        revived_row = _clone_row(
            branch_name=None,
            task_id=None,
            agent_session_id=None,
            status=CloneStatus.ACTIVE.value,
            remote_url="file:///tmp/source",
            last_sync_at=None,
            cleanup_after=None,
        )
        mock_db.fetchone.side_effect = [cleanup_row, revived_row]

        clone, registered = manager.register_adopted(
            project_id="proj-abc",
            branch_name=None,
            clone_path="/tmp/clones/test",
            base_branch="main",
            remote_url="file:///tmp/source",
        )

        assert registered is True
        assert clone.branch_name is None
        assert clone.status == CloneStatus.ACTIVE.value
        assert clone.task_id is None
        assert clone.agent_session_id is None
        assert clone.last_sync_at is None
        assert clone.cleanup_after is None
        update_values = mock_db.execute.call_args.args[1]
        assert update_values[:8] == (
            None,
            "main",
            "file:///tmp/source",
            None,
            None,
            CloneStatus.ACTIVE.value,
            None,
            None,
        )

    def test_preserves_deleting_record_as_retry_state(
        self,
        manager: LocalCloneManager,
        mock_db: MagicMock,
    ) -> None:
        mock_db.fetchone.return_value = _clone_row(status=CloneStatus.DELETING.value)

        clone, registered = manager.register_adopted(
            project_id="proj-abc",
            branch_name="changed",
            clone_path="/tmp/clones/test",
            base_branch="main",
            remote_url="file:///tmp/changed",
        )

        assert registered is False
        assert clone.status == CloneStatus.DELETING.value
        assert clone.branch_name == "feature/test"
        mock_db.execute.assert_not_called()

    def test_collapses_same_path_registration_race(
        self,
        manager: LocalCloneManager,
        mock_db: MagicMock,
    ) -> None:
        winner = _clone_row(branch_name="winner")
        mock_db.fetchone.side_effect = [None, winner]
        mock_db.execute.side_effect = psycopg.IntegrityError("duplicate path")

        clone, registered = manager.register_adopted(
            project_id="proj-abc",
            branch_name="loser",
            clone_path="/tmp/clones/test",
            base_branch="main",
            remote_url=None,
        )

        assert registered is False
        assert clone.branch_name == "winner"

    def test_propagates_unrelated_registration_conflict(
        self,
        manager: LocalCloneManager,
        mock_db: MagicMock,
    ) -> None:
        mock_db.fetchone.side_effect = [None, None]
        mock_db.execute.side_effect = psycopg.IntegrityError("unrelated constraint")

        with pytest.raises(psycopg.IntegrityError, match="unrelated constraint"):
            manager.register_adopted(
                project_id="proj-abc",
                branch_name="main",
                clone_path="/tmp/clones/test",
                base_branch="main",
                remote_url=None,
            )

    def test_release(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """release clears agent_session_id."""
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "machine_id": MACHINE_ID,
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
                machine_id=MACHINE_ID,
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


class TestLocalCloneManagerCleanupSafety:
    """Real-database coverage for clone cleanup eligibility and stale TTL clearing."""

    def test_find_expired_returns_only_merged_unclaimed_clones(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
        session_manager: SessionManager,
    ) -> None:
        manager = LocalCloneManager(temp_db)
        project_id = str(sample_project["id"])
        expired_at = datetime(2020, 1, 1, tzinfo=UTC)

        active = manager.create(
            project_id=project_id,
            branch_name="feature/expired-active",
            clone_path="/tmp/clones/expired-active",
            cleanup_after=expired_at,
        )
        syncing = manager.create(
            project_id=project_id,
            branch_name="feature/expired-syncing",
            clone_path="/tmp/clones/expired-syncing",
            cleanup_after=expired_at,
        )
        manager.mark_syncing(syncing.id)

        session = session_manager.register(
            external_id="expired-claimed-clone",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=project_id,
        )
        claimed = manager.create(
            project_id=project_id,
            branch_name="feature/expired-claimed",
            clone_path="/tmp/clones/expired-claimed",
        )
        manager.claim(claimed.id, session.id)
        manager.update(
            claimed.id,
            status=CloneStatus.MERGED.value,
            cleanup_after=expired_at,
        )

        merged = manager.create(
            project_id=project_id,
            branch_name="feature/expired-merged",
            clone_path="/tmp/clones/expired-merged",
        )
        manager.mark_merged(merged.id, cleanup_after=expired_at)

        expired = manager.find_expired(project_id=project_id)

        assert [clone.id for clone in expired] == [merged.id]
        assert active.id not in {clone.id for clone in expired}

    def test_record_sync_clears_cleanup_after(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
    ) -> None:
        manager = LocalCloneManager(temp_db)
        clone = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/resynced",
            clone_path="/tmp/clones/resynced",
            cleanup_after=datetime(2020, 1, 1, tzinfo=UTC),
        )

        synced = manager.record_sync(clone.id)

        assert synced is not None
        assert synced.status == CloneStatus.ACTIVE.value
        assert synced.cleanup_after is None

    def test_claim_clears_cleanup_after(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
        session_manager: SessionManager,
    ) -> None:
        project_id = str(sample_project["id"])
        session = session_manager.register(
            external_id="claim-clears-cleanup",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=project_id,
        )
        manager = LocalCloneManager(temp_db)
        clone = manager.create(
            project_id=project_id,
            branch_name="feature/reclaimed",
            clone_path="/tmp/clones/reclaimed",
            cleanup_after=datetime(2020, 1, 1, tzinfo=UTC),
        )

        claimed = manager.claim(clone.id, session.id)

        assert claimed is not None
        assert claimed.agent_session_id == session.id
        assert claimed.cleanup_after is None


class TestLocalCloneManagerCountByStatus:
    """Tests for LocalCloneManager.count_by_status method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_count_by_status(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
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
        assert params == ("proj-abc", MACHINE_ID, CloneStatus.CLEANUP.value)

    def test_count_by_status_empty(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """count_by_status returns empty dict when no clones."""
        mock_db.fetchall.return_value = []

        result = manager.count_by_status("proj-abc")

        assert result == {}


class TestLocalCloneManagerFindStale:
    """Tests for LocalCloneManager.find_stale method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_find_stale_returns_clones(
        self, manager: LocalCloneManager, mock_db: MagicMock
    ) -> None:
        """find_stale returns stale clones."""
        mock_db.fetchall.return_value = [
            {
                "id": "clone-1",
                "project_id": "proj-abc",
                "branch_name": "old-feature",
                "clone_path": "/tmp/clones/old",
                "base_branch": "main",
                "task_id": None,
                "machine_id": MACHINE_ID,
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
        assert "status IN (%s, %s)" in query
        assert "agent_session_id IS NULL" in query
        assert "updated_at < %s" in query
        assert "LIMIT %s" in query

    def test_find_stale_includes_interrupted_syncs(
        self, manager: LocalCloneManager, mock_db: MagicMock
    ) -> None:
        """find_stale includes syncing clones older than the threshold."""
        mock_db.fetchall.return_value = [_clone_row(status="syncing")]

        result = manager.find_stale("proj-abc", hours=24)

        assert result[0].status == "syncing"
        params = mock_db.fetchall.call_args.args[1]
        assert params[1] == require_machine_id()
        assert params[2:4] == ("active", "syncing")

    def test_find_stale_empty(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """find_stale returns empty list when no stale clones."""
        mock_db.fetchall.return_value = []

        result = manager.find_stale("proj-abc", hours=48, limit=10)

        assert result == []

    def test_find_stale_custom_params(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """find_stale passes custom hours and limit."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc", hours=72, limit=5)

        call_args = mock_db.fetchall.call_args
        params = call_args[0][1]
        # params: (project_id, machine_id, active status, syncing status, cutoff, limit)
        assert params[0] == "proj-abc"
        assert params[1] == require_machine_id()
        assert params[2] == "active"
        assert params[3] == "syncing"
        assert params[5] == 5


class TestLocalCloneManagerCleanupStale:
    """Tests for LocalCloneManager.cleanup_stale method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_cleanup_stale_dry_run(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """cleanup_stale in dry_run returns stale clones without updating."""
        mock_db.fetchall.return_value = [
            {
                "id": "clone-1",
                "project_id": "proj-abc",
                "branch_name": "old-feature",
                "clone_path": "/tmp/clones/old",
                "base_branch": "main",
                "task_id": None,
                "machine_id": MACHINE_ID,
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

    def test_cleanup_stale_actual(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
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
                "machine_id": MACHINE_ID,
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
            "machine_id": MACHINE_ID,
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

    def test_cleanup_stale_empty(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """cleanup_stale returns empty list when no stale clones."""
        mock_db.fetchall.return_value = []

        result = manager.cleanup_stale("proj-abc", hours=24, dry_run=False)

        assert result == []
        mock_db.execute.assert_not_called()


class TestLocalCloneManagerUpdateValidation:
    """Tests for LocalCloneManager.update field validation."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalCloneManager:
        """Create manager with mock database."""
        return LocalCloneManager(db=mock_db)

    def test_update_no_fields(self, manager: LocalCloneManager, mock_db: MagicMock) -> None:
        """Update with no fields returns existing clone."""
        mock_db.fetchone.return_value = {
            "id": "clone-123",
            "project_id": "proj-abc",
            "branch_name": "feature/test",
            "clone_path": "/tmp/clones/test",
            "base_branch": "main",
            "task_id": None,
            "machine_id": MACHINE_ID,
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

    def test_update_invalid_field_raises(self, manager: LocalCloneManager) -> None:
        """Update with invalid field name raises ValueError."""
        with pytest.raises(ValueError, match="Invalid field names"):
            manager.update("clone-123", invalid_field="value")
