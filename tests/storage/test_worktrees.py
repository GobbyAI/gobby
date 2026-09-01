"""Tests for local worktree storage manager."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import gobby.storage.worktrees as worktrees_module
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.storage.worktrees import LocalWorktreeManager, Worktree, WorktreeStatus
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.isolated_checkout import (
    install_isolated_checkout_project,
    patch_local_machine_id,
)
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the cache and both imported require_machine_id bindings to MACHINE_ID."""
    patch_local_machine_id(monkeypatch, MACHINE_ID)


@pytest.fixture
def sample_project(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The root fixture pins a random machine; this module's sessions live on MACHINE_ID."""
    isolated = install_isolated_checkout_project(
        temp_db,
        tmp_path / "isolated-checkout",
        machine_id=MACHINE_ID,
        monkeypatch=monkeypatch,
    )
    return isolated.project.to_dict()


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
            "machine_id": MACHINE_ID,
            "agent_session_id": "sess-xyz",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "last_activity_at": "2025-01-01T00:00:00+00:00",
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
        assert worktree.last_activity_at == datetime(2025, 1, 1, tzinfo=UTC)
        assert worktree.merged_at is None

    def test_to_dict(self) -> None:
        """to_dict converts Worktree to dictionary."""
        worktree = Worktree(
            id="wt-123456",
            project_id="proj-abc",
            machine_id=MACHINE_ID,
            task_id="gt-task123",
            branch_name="feature/test",
            worktree_path="/path/to/worktree",
            base_branch="main",
            agent_session_id="sess-xyz",
            status="active",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
            last_activity_at="2025-01-01T00:00:00+00:00",
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
        assert result["last_activity_at"] == "2025-01-01T00:00:00+00:00"
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
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        db = MagicMock()
        now = datetime.now(UTC)
        db.execute.return_value.fetchone.return_value = {
            "created_at": now,
            "updated_at": now,
        }
        return db

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_create_minimal(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
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
        assert str(uuid.UUID(worktree.id)) == worktree.id
        mock_db.execute.assert_called_once()

    def test_create_detached(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        worktree = manager.create(
            project_id="proj-abc",
            branch_name=None,
            worktree_path="/path/to/detached-worktree",
        )

        assert worktree.branch_name is None
        assert mock_db.execute.call_args.args[1][4] is None

    def test_create_with_all_fields(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Create worktree with all optional fields."""
        mock_db.fetchone.return_value = {"machine_id": MACHINE_ID}
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

    def test_create_generates_unique_id(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
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
        assert str(uuid.UUID(worktree1.id)) == worktree1.id
        assert str(uuid.UUID(worktree2.id)) == worktree2.id

    def test_create_sets_timestamps(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
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


def test_worktree_uniqueness_is_machine_scoped(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical branch and path values can coexist on distinct machines."""
    machine_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for machine_id in machine_ids:
        temp_db.execute(
            "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (machine_id, f"host-{machine_id}", TEST_USER_ID),
        )

    owners = iter(machine_ids)
    monkeypatch.setattr(
        worktrees_module,
        "require_machine_id",
        lambda: next(owners),
    )
    manager = LocalWorktreeManager(temp_db)
    created = [
        manager.create(
            project_id=sample_project["id"],
            branch_name="feature/shared-name",
            worktree_path="/same/path/on/two/machines",
        )
        for _ in machine_ids
    ]

    assert [worktree.machine_id for worktree in created] == machine_ids
    rows = temp_db.fetchall(
        """
        SELECT machine_id
          FROM worktrees
         WHERE branch_name = %s
         ORDER BY machine_id
        """,
        ("feature/shared-name",),
    )
    assert {str(row["machine_id"]) for row in rows} == set(machine_ids)


def test_cleanup_scoped_to_local_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale discovery, cleanup, and deletion refuse remote worktree rows."""
    local_machine_id = MACHINE_ID
    remote_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, remote_machine_id):
        temp_db.execute(
            "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (machine_id, f"host-{machine_id}", TEST_USER_ID),
        )

    owners = iter((local_machine_id, remote_machine_id))
    monkeypatch.setattr(
        worktrees_module,
        "require_machine_id",
        lambda: next(owners),
    )
    manager = LocalWorktreeManager(temp_db)
    local = manager.create(
        project_id=sample_project["id"],
        branch_name="task/local-stale",
        worktree_path="/tmp/local-stale",
    )
    remote = manager.create(
        project_id=sample_project["id"],
        branch_name="task/remote-stale",
        worktree_path="/tmp/remote-stale",
    )
    assert local.machine_id == local_machine_id
    assert remote.machine_id == remote_machine_id
    monkeypatch.setattr(worktrees_module, "require_machine_id", lambda: local_machine_id)
    stale_at = datetime.now(UTC) - timedelta(hours=48)
    temp_db.execute(
        "UPDATE worktrees SET last_activity_at = %s, updated_at = %s WHERE id IN (%s, %s)",
        (stale_at, stale_at, local.id, remote.id),
    )

    stale = manager.find_stale(sample_project["id"], hours=24)
    cleaned = manager.cleanup_stale(sample_project["id"], hours=24, dry_run=False)

    assert [worktree.id for worktree in stale] == [local.id]
    assert [worktree.id for worktree in cleaned] == [local.id]
    with pytest.raises(MachineOwnershipMismatchError):
        manager.delete(remote.id)
    stored_remote = temp_db.fetchone("SELECT status FROM worktrees WHERE id = %s", (remote.id,))
    assert stored_remote is not None
    assert stored_remote["status"] == WorktreeStatus.ACTIVE.value


def test_claim_scoped_to_local_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookup and claim surfaces never reuse a remote machine's worktree."""
    local_machine_id = MACHINE_ID
    remote_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, remote_machine_id):
        temp_db.execute(
            "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (machine_id, f"host-{machine_id}", TEST_USER_ID),
        )

    owners = iter((remote_machine_id, local_machine_id))
    monkeypatch.setattr(
        worktrees_module,
        "require_machine_id",
        lambda: next(owners),
    )
    manager = LocalWorktreeManager(temp_db)
    session = session_manager.register(
        external_id=f"worktree-scope-{uuid.uuid4()}",
        machine_id=local_machine_id,
        source="codex",
        project_id=sample_project["id"],
    )
    remote = manager.create(
        project_id=sample_project["id"],
        branch_name="task/shared",
        worktree_path="/same/path",
    )
    local = manager.create(
        project_id=sample_project["id"],
        branch_name="task/shared",
        worktree_path="/same/path",
    )
    assert remote.machine_id == remote_machine_id
    assert local.machine_id == local_machine_id
    monkeypatch.setattr(worktrees_module, "require_machine_id", lambda: local_machine_id)

    assert manager.get_by_path("/same/path") == local
    assert manager.get_by_branch(sample_project["id"], "task/shared") == local
    assert manager.has_path_on_other_machine("/same/path") is True
    with pytest.raises(MachineOwnershipMismatchError):
        manager.claim(remote.id, session.id)
    with pytest.raises(MachineOwnershipMismatchError):
        manager.claim_if_available(remote.id, session.id)


class TestLocalWorktreeManagerGet:
    """Tests for LocalWorktreeManager.get method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_get_existing(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Get returns Worktree for existing ID."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "machine_id": MACHINE_ID,
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

    def test_get_not_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Get returns None for non-existent ID."""
        mock_db.fetchone.return_value = None

        worktree = manager.get("wt-nonexistent")

        assert worktree is None


class TestLocalWorktreeManagerGetBy:
    """Tests for LocalWorktreeManager get_by_* methods."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    @pytest.fixture
    def mock_row(self) -> dict[str, Any]:
        """Create mock database row."""
        return {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": "gt-task123",
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "machine_id": MACHINE_ID,
            "agent_session_id": "sess-xyz",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

    def test_get_by_path_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
        """get_by_path returns worktree for existing path."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.get_by_path("/path/to/worktree")

        assert worktree is not None
        assert worktree.worktree_path == "/path/to/worktree"

    def test_get_by_path_not_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """get_by_path returns None for non-existent path."""
        mock_db.fetchone.return_value = None

        worktree = manager.get_by_path("/nonexistent/path")

        assert worktree is None

    def test_get_by_branch_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
        """get_by_branch returns worktree for project/branch."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.get_by_branch("proj-abc", "feature/test")

        assert worktree is not None
        assert worktree.branch_name == "feature/test"

    def test_get_by_branch_not_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """get_by_branch returns None for non-existent branch."""
        mock_db.fetchone.return_value = None

        worktree = manager.get_by_branch("proj-abc", "nonexistent")

        assert worktree is None

    def test_get_by_task_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
        """get_by_task returns worktree for task ID."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.get_by_task("gt-task123")

        assert worktree is not None
        assert worktree.task_id == "gt-task123"

    def test_get_by_task_prefers_active_worktree(self, mock_row: dict[str, Any]) -> None:
        """get_by_task ranks active worktrees above stale task-linked rows."""

        class OrderingDb:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self.rows = rows
                self.query: str | None = None
                self.params: tuple[str, ...] | None = None

            def fetchone(self, query: str, params: tuple[str, ...]) -> dict[str, Any] | None:
                self.query = query
                self.params = params
                task_id, _machine_id, *statuses = params
                status_rank = {status: rank for rank, status in enumerate(statuses)}
                candidates = [row for row in self.rows if row["task_id"] == task_id]
                candidates.sort(
                    key=lambda row: (
                        status_rank.get(row["status"], len(status_rank)),
                        row["updated_at"],
                        row["created_at"],
                    )
                )
                return candidates[0] if candidates else None

        abandoned_row = {
            **mock_row,
            "id": "wt-abandoned",
            "status": WorktreeStatus.ABANDONED.value,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        active_row = {
            **mock_row,
            "id": "wt-active",
            "status": WorktreeStatus.ACTIVE.value,
            "updated_at": "2025-01-01T00:00:00+00:00",
        }
        mock_db = OrderingDb([abandoned_row, active_row])
        manager = LocalWorktreeManager(db=mock_db)

        worktree = manager.get_by_task("gt-task123")

        assert worktree is not None
        assert worktree.id == "wt-active"
        assert "CASE status" in mock_db.query
        assert mock_db.params == (
            "gt-task123",
            require_machine_id(),
            WorktreeStatus.ACTIVE.value,
            WorktreeStatus.STALE.value,
            WorktreeStatus.MERGED.value,
            WorktreeStatus.ABANDONED.value,
        )

    def test_get_by_task_returns_stale_worktree_without_active(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
        """get_by_task still returns a stale row when no active row exists."""
        stale_row = {**mock_row, "status": WorktreeStatus.STALE.value}
        mock_db.fetchone.return_value = stale_row

        worktree = manager.get_by_task("gt-task123")

        assert worktree is not None
        assert worktree.status == WorktreeStatus.STALE.value

    def test_get_by_task_not_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """get_by_task returns None for non-existent task."""
        mock_db.fetchone.return_value = None

        worktree = manager.get_by_task("gt-nonexistent")

        assert worktree is None

    @pytest.mark.integration
    def test_get_by_task_prefers_active_worktree_with_real_db(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
    ) -> None:
        """Real storage honors the ORDER BY CASE status priority."""
        task = LocalTaskManager(temp_db).create_task(
            project_id=str(sample_project["id"]),
            title="Worktree ordering",
            validation_criteria="Storage fixture task; behavior asserted by the test.",
        )
        manager = LocalWorktreeManager(temp_db)
        active = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/active",
            worktree_path="/tmp/gobby-active",
            task_id=task.id,
        )
        stale = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/stale",
            worktree_path="/tmp/gobby-stale",
            task_id=task.id,
        )
        manager.mark_stale(stale.id)

        worktree = manager.get_by_task(task.id)

        assert worktree is not None
        assert worktree.id == active.id


class TestLocalWorktreeManagerList:
    """Tests for LocalWorktreeManager.list method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_list_no_filters(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """List returns all worktrees without filters."""
        mock_db.fetchall.return_value = [
            {
                "id": "wt-1",
                "project_id": "proj-abc",
                "task_id": None,
                "branch_name": "feature/one",
                "worktree_path": "/path/one",
                "base_branch": "main",
                "machine_id": MACHINE_ID,
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
                "machine_id": MACHINE_ID,
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

    def test_list_filter_by_project(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """List filters by project_id."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(project_id="proj-abc")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "project_id = %s" in query
        assert "proj-abc" in params

    def test_list_filter_by_status(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """List filters by status."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(status="active")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "status = %s" in query
        assert "active" in params

    def test_list_filter_by_session(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """List filters by agent_session_id."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(agent_session_id="sess-xyz")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "agent_session_id = %s" in query
        assert "sess-xyz" in params

    def test_list_with_limit(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """List respects limit parameter."""
        mock_db.fetchall.return_value = []

        manager.list_worktrees(limit=10)

        call_args = mock_db.fetchall.call_args
        params = call_args[0][1]
        assert params[-1] == 10  # Limit is always last param

    def test_list_combines_filters(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
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
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_update_no_fields_returns_current(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Update with no fields returns current worktree."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "machine_id": MACHINE_ID,
            "agent_session_id": None,
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

        worktree = manager.update("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_not_called()

    def test_touch_refreshes_timestamp_and_clears_staleness(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
    ) -> None:
        manager = LocalWorktreeManager(temp_db)
        worktree = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/recently-synced",
            worktree_path="/tmp/gobby-recently-synced",
        )
        stale_timestamp = datetime.now(UTC) - timedelta(hours=48)
        temp_db.execute(
            "UPDATE worktrees SET updated_at = %s, last_activity_at = %s WHERE id = %s",
            (stale_timestamp, stale_timestamp, worktree.id),
        )
        assert [item.id for item in manager.find_stale(str(sample_project["id"]), hours=24)] == [
            worktree.id
        ]

        touched = manager.touch(worktree.id)

        assert touched is not None
        assert touched.updated_at > stale_timestamp
        assert touched.last_activity_at is not None
        assert touched.last_activity_at > stale_timestamp
        assert manager.find_stale(str(sample_project["id"]), hours=24) == []

    def test_update_single_field(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Update modifies specified field."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "machine_id": MACHINE_ID,
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

    def test_update_multiple_fields(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Update modifies multiple fields."""
        mock_db.fetchone.return_value = {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": "gt-task999",
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "machine_id": MACHINE_ID,
            "agent_session_id": "sess-new",
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-02T00:00:00+00:00",
            "merged_at": None,
        }

        worktree = manager.update("wt-123456", task_id="gt-task999", agent_session_id="sess-new")

        assert worktree is not None
        mock_db.execute.assert_called_once()

    def test_update_not_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Update returns None for non-existent worktree."""
        mock_db.fetchone.return_value = None

        worktree = manager.update("wt-nonexistent", status="stale")

        assert worktree is None


class TestLocalWorktreeManagerDelete:
    """Tests for LocalWorktreeManager.delete method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_delete_existing(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Delete returns True for existing worktree."""
        conn = MagicMock()
        delete_cursor = MagicMock()
        delete_cursor.rowcount = 1
        conn.execute.return_value = delete_cursor
        mock_db.transaction.return_value.__enter__.return_value = conn
        mock_db.transaction.return_value.__exit__.return_value = False

        with patch.object(manager, "get", return_value=None):
            result = manager.delete("wt-123456")

        assert result is True
        conn.execute.assert_called_once()

    def test_delete_not_found(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """Delete returns False for non-existent worktree."""
        conn = MagicMock()
        delete_cursor = MagicMock()
        delete_cursor.rowcount = 0
        conn.execute.return_value = delete_cursor
        mock_db.transaction.return_value.__enter__.return_value = conn
        mock_db.transaction.return_value.__exit__.return_value = False
        mock_db.fetchone.return_value = None

        with patch.object(manager, "get", return_value=None):
            result = manager.delete("wt-nonexistent")

        assert result is False

    def test_delete_tombstones_session_workspace_identity(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        current = SimpleNamespace(worktree_path="/tmp/worktrees/tombstone-workspace")
        conn = MagicMock()
        delete_cursor = MagicMock()
        delete_cursor.rowcount = 1
        conn.execute.side_effect = [MagicMock(), delete_cursor]
        mock_db.transaction.return_value.__enter__.return_value = conn
        mock_db.transaction.return_value.__exit__.return_value = False

        with patch.object(manager, "get", return_value=current):
            deleted = manager.delete("wt-123456")

        tombstone_sql = str(conn.execute.call_args_list[0].args[0])
        assert deleted is True
        assert "workspace_path = NULL" in tombstone_sql
        assert "workspace_generation = workspace_generation + 1" in tombstone_sql
        assert conn.execute.call_args_list[0].args[1] == ("/tmp/worktrees/tombstone-workspace",)


class TestLocalWorktreeManagerStatusTransitions:
    """Tests for LocalWorktreeManager status transition methods."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    @pytest.fixture
    def mock_row(self) -> dict[str, Any]:
        """Create mock database row."""
        return {
            "id": "wt-123456",
            "project_id": "proj-abc",
            "task_id": None,
            "branch_name": "feature/test",
            "worktree_path": "/path/to/worktree",
            "base_branch": "main",
            "machine_id": MACHINE_ID,
            "agent_session_id": None,
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "merged_at": None,
        }

    def test_claim_sets_session_id(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
        """claim sets agent_session_id."""
        mock_row["agent_session_id"] = "sess-new"
        mock_db.execute.return_value.rowcount = 1
        mock_db.fetchone.return_value = mock_row

        worktree = manager.claim("wt-123456", "sess-new")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        query = call_args[0][0]
        assert "agent_session_id = %s" in query
        assert "last_activity_at = %s" in query

    def test_claim_returns_none_when_conditional_update_loses(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """claim reports failure when another session already owns the worktree."""
        mock_db.execute.return_value.rowcount = 0
        mock_db.fetchone.return_value = {"machine_id": MACHINE_ID}

        worktree = manager.claim("wt-123456", "sess-new")

        assert worktree is None

    def test_concurrent_claims_have_exactly_one_winning_session(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
    ) -> None:
        """A conditional update prevents competing owners from both winning."""
        session_manager = SessionManager(temp_db)
        manager = LocalWorktreeManager(temp_db)
        worktree = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/atomic-worktree-claim",
            worktree_path="/tmp/worktrees/atomic-worktree-claim",
        )
        sessions = [
            session_manager.register(
                external_id=f"atomic-worktree-claim-{index}",
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
                claimed = LocalWorktreeManager(temp_db).claim(worktree.id, session_id)
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
        stored = manager.get(worktree.id)
        assert stored is not None
        assert stored.agent_session_id == winners[0]

    def test_release_clears_session_id(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
        """release clears agent_session_id."""
        mock_db.fetchone.return_value = mock_row

        worktree = manager.release("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        # First param is None (agent_session_id), followed by updated_at
        assert None in params

    def test_claim_if_available_rejects_unallowed_owner(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
    ) -> None:
        session_manager = SessionManager(temp_db)
        current_owner = session_manager.register(
            machine_id=MACHINE_ID,
            source="claude",
            project_id=str(sample_project["id"]),
            external_id="worktree-current-owner",
        )
        resumed_owner = session_manager.register(
            machine_id=MACHINE_ID,
            source="codex",
            project_id=str(sample_project["id"]),
            external_id="worktree-resumed-owner",
        )
        manager = LocalWorktreeManager(temp_db)
        worktree = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/resume-claim",
            worktree_path="/tmp/gobby-resume-claim",
            agent_session_id=current_owner.id,
        )

        blocked = manager.claim_if_available(
            worktree.id,
            resumed_owner.id,
            allowed_existing_session_ids={None, str(uuid.uuid4())},
        )

        assert blocked is None
        assert manager.get(worktree.id).agent_session_id == current_owner.id

        claimed = manager.claim_if_available(
            worktree.id,
            resumed_owner.id,
            allowed_existing_session_ids={current_owner.id},
        )

        assert claimed is not None
        assert claimed.agent_session_id == resumed_owner.id

    @pytest.mark.integration
    def test_is_claimed_by_live_session_checks_owner_status(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, object],
    ) -> None:
        session_manager = SessionManager(temp_db)
        owner = session_manager.register(
            machine_id=MACHINE_ID,
            source="claude",
            project_id=str(sample_project["id"]),
            external_id="worktree-live-owner",
        )
        manager = LocalWorktreeManager(temp_db)
        worktree = manager.create(
            project_id=str(sample_project["id"]),
            branch_name="feature/live-claim",
            worktree_path="/tmp/gobby-live-claim",
            agent_session_id=owner.id,
        )

        assert manager.is_claimed_by_live_session(worktree.id) is True

        session_manager.update_status(owner.id, "expired")

        assert manager.is_claimed_by_live_session(worktree.id) is False

    def test_is_claimed_by_live_session_stops_when_owned_row_is_absent(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        mock_db.fetchone.return_value = None

        assert manager.is_claimed_by_live_session("wt-missing") is False
        assert mock_db.fetchone.call_count == 2
        assert all("JOIN sessions" not in call.args[0] for call in mock_db.fetchone.call_args_list)

    def test_mark_stale_sets_status(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
        """mark_stale sets status to stale."""
        mock_row["status"] = "stale"
        mock_db.fetchone.return_value = mock_row

        worktree = manager.mark_stale("wt-123456")

        assert worktree is not None
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args
        params = call_args[0][1]
        assert WorktreeStatus.STALE.value in params

    def test_mark_merged_sets_status_and_timestamp(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
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
        cleanup_after = params[2]
        assert isinstance(cleanup_after, datetime)
        assert before <= cleanup_after <= datetime.now(UTC) + timedelta(seconds=1)

    def test_mark_abandoned_sets_status(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
        mock_row: dict[str, Any],
    ) -> None:
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
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_find_stale_default_hours(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """find_stale uses default 24 hours threshold."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc")

        mock_db.fetchall.assert_called_once()
        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        assert "COALESCE(last_activity_at, updated_at) <" in query
        assert "status = %s" in query

    def test_find_stale_custom_hours(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """find_stale uses custom hours threshold."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc", hours=48)

        mock_db.fetchall.assert_called_once()
        assert mock_db.fetchall.call_count == 1
        assert mock_db.fetchall.call_args is not None

    def test_find_stale_returns_worktrees(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """find_stale returns list of stale worktrees."""
        mock_db.fetchall.return_value = [
            {
                "id": "wt-stale1",
                "project_id": "proj-abc",
                "task_id": None,
                "branch_name": "feature/old",
                "worktree_path": "/path/old",
                "base_branch": "main",
                "machine_id": MACHINE_ID,
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

    def test_find_stale_respects_limit(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """find_stale respects limit parameter."""
        mock_db.fetchall.return_value = []

        manager.find_stale("proj-abc", limit=5)

        call_args = mock_db.fetchall.call_args
        params = call_args[0][1]
        assert params[-1] == 5


class TestLocalWorktreeManagerCleanupStale:
    """Tests for LocalWorktreeManager.cleanup_stale method."""

    @pytest.fixture
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_cleanup_stale_dry_run_default(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """cleanup_stale defaults to dry run."""
        mock_db.fetchall.return_value = [
            {
                "id": "wt-stale1",
                "project_id": "proj-abc",
                "task_id": None,
                "branch_name": "feature/old",
                "worktree_path": "/path/old",
                "base_branch": "main",
                "machine_id": MACHINE_ID,
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

    def test_cleanup_stale_marks_abandoned(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
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
                "machine_id": MACHINE_ID,
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
            "machine_id": MACHINE_ID,
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
    def mock_db(self) -> MagicMock:
        """Create mock database."""
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_db: MagicMock) -> LocalWorktreeManager:
        """Create manager with mock database."""
        return LocalWorktreeManager(db=mock_db)

    def test_count_by_status_empty(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """count_by_status returns empty dict for no worktrees."""
        mock_db.fetchall.return_value = []

        counts = manager.count_by_status("proj-abc")

        assert counts == {}

    def test_count_by_status_with_data(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """count_by_status returns status counts."""
        mock_db.fetchall.return_value = [
            {"status": "active", "count": 5},
            {"status": "stale", "count": 2},
            {"status": "merged", "count": 10},
        ]

        counts = manager.count_by_status("proj-abc")

        assert counts == {"active": 5, "stale": 2, "merged": 10}

    def test_count_by_status_queries_project(
        self,
        manager: LocalWorktreeManager,
        mock_db: MagicMock,
    ) -> None:
        """count_by_status filters by project_id."""
        mock_db.fetchall.return_value = []

        manager.count_by_status("proj-abc")

        call_args = mock_db.fetchall.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        assert "project_id = %s" in query
        assert "proj-abc" in params
