"""Tests for merge resolution storage (TDD Red Phase).

Tests for MergeResolution and MergeConflict persistence in PostgreSQL database.
Tests should fail initially as the storage module does not exist yet.
"""

import pytest

from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit

# =============================================================================
# Import Tests
# =============================================================================


class TestMergeStorageImport:
    """Tests for merge storage module imports."""

    def test_import_merge_resolution_dataclass(self) -> None:
        """Test that MergeResolution can be imported."""
        from gobby.storage.merge_resolutions import MergeResolution

        assert MergeResolution is not None

    def test_import_merge_conflict_dataclass(self) -> None:
        """Test that MergeConflict can be imported."""
        from gobby.storage.merge_resolutions import MergeConflict

        assert MergeConflict is not None

    def test_import_merge_resolution_manager(self) -> None:
        """Test that MergeResolutionManager can be imported."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        assert MergeResolutionManager is not None

    def test_import_conflict_status_enum(self) -> None:
        """Test that ConflictStatus enum can be imported."""
        from gobby.storage.merge_resolutions import ConflictStatus

        assert ConflictStatus is not None
        assert hasattr(ConflictStatus, "PENDING")
        assert hasattr(ConflictStatus, "RESOLVED")
        assert hasattr(ConflictStatus, "FAILED")
        assert hasattr(ConflictStatus, "HUMAN_REVIEW")


# =============================================================================
# MergeResolution Table Schema Tests
# =============================================================================


class TestMergeResolutionsTableExists:
    """Test that merge_resolutions table is created."""

    def test_merge_resolutions_table_created(self, tmp_path) -> None:
        """Test that merge_resolutions table exists after migrations."""
        db_path = tmp_path / "merge.db"
        db = HubDatabase(db_path)

        run_migrations(db)

        # Check table exists
        row = db.fetchone(
            "SELECT name FROM postgres_master WHERE type='table' AND name='merge_resolutions'"
        )
        assert row is not None, "merge_resolutions table not created"


class TestMergeResolutionsSchema:
    """Test merge_resolutions table has correct columns."""

    def test_has_required_columns(self, tmp_path) -> None:
        """Test that merge_resolutions has all required columns."""
        db_path = tmp_path / "merge_schema.db"
        db = HubDatabase(db_path)

        run_migrations(db)

        # Get table info
        rows = db.fetchall("information_schema table_info(merge_resolutions)")
        columns = {row["name"] for row in rows}

        # Verify required columns exist
        expected_columns = {
            "id",
            "worktree_id",
            "source_branch",
            "target_branch",
            "status",
            "tier_used",
            "created_at",
            "updated_at",
        }
        for col in expected_columns:
            assert col in columns, f"Column {col} missing from merge_resolutions"

    def test_id_is_primary_key(self, tmp_path) -> None:
        """Test that id is the primary key."""
        db_path = tmp_path / "merge_pk.db"
        db = HubDatabase(db_path)

        run_migrations(db)

        rows = db.fetchall("information_schema table_info(merge_resolutions)")
        id_col = next((r for r in rows if r["name"] == "id"), None)
        assert id_col is not None
        assert id_col["pk"] == 1, "id column is not primary key"

    def test_worktree_id_not_null(self, tmp_path) -> None:
        """Test that worktree_id is NOT NULL."""
        db_path = tmp_path / "merge_notnull.db"
        db = HubDatabase(db_path)

        run_migrations(db)

        rows = db.fetchall("information_schema table_info(merge_resolutions)")
        worktree_col = next((r for r in rows if r["name"] == "worktree_id"), None)
        assert worktree_col is not None
        assert worktree_col["notnull"] == 1, "worktree_id should be NOT NULL"


# =============================================================================
# MergeConflicts Table Schema Tests
# =============================================================================


class TestMergeConflictsTableExists:
    """Test that merge_conflicts table is created."""

    def test_merge_conflicts_table_created(self, tmp_path) -> None:
        """Test that merge_conflicts table exists after migrations."""
        db_path = tmp_path / "conflicts.db"
        db = HubDatabase(db_path)

        run_migrations(db)

        # Check table exists
        row = db.fetchone(
            "SELECT name FROM postgres_master WHERE type='table' AND name='merge_conflicts'"
        )
        assert row is not None, "merge_conflicts table not created"


class TestMergeConflictsSchema:
    """Test merge_conflicts table has correct columns."""

    def test_has_required_columns(self, tmp_path) -> None:
        """Test that merge_conflicts has all required columns."""
        db_path = tmp_path / "conflicts_schema.db"
        db = HubDatabase(db_path)

        run_migrations(db)

        # Get table info
        rows = db.fetchall("information_schema table_info(merge_conflicts)")
        columns = {row["name"] for row in rows}

        # Verify required columns exist
        expected_columns = {
            "id",
            "resolution_id",
            "file_path",
            "status",
            "ours_content",
            "theirs_content",
            "resolved_content",
            "created_at",
            "updated_at",
        }
        for col in expected_columns:
            assert col in columns, f"Column {col} missing from merge_conflicts"

    def test_foreign_key_to_resolutions(self, tmp_path) -> None:
        """Test that merge_conflicts has foreign key to merge_resolutions."""
        db_path = tmp_path / "conflicts_fk.db"
        db = HubDatabase(db_path)

        run_migrations(db)

        # Get table SQL
        row = db.fetchone(
            "SELECT sql FROM postgres_master WHERE type='table' AND name='merge_conflicts'"
        )
        assert row is not None
        sql_lower = row["sql"].lower()

        # Check for foreign key reference to merge_resolutions
        assert "references merge_resolutions" in sql_lower or "foreign key" in sql_lower, (
            "merge_conflicts missing foreign key to merge_resolutions"
        )


# =============================================================================
# MergeResolution Dataclass Tests
# =============================================================================


class TestMergeResolutionDataclass:
    """Tests for MergeResolution dataclass."""

    def test_merge_resolution_has_required_fields(self) -> None:
        """Test that MergeResolution has all required fields."""
        from gobby.storage.merge_resolutions import MergeResolution

        resolution = MergeResolution(
            id="mr-1",
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2026-01-08T00:00:00Z",
            updated_at="2026-01-08T00:00:00Z",
        )
        assert resolution.id == "mr-1"
        assert resolution.worktree_id == "wt-1"
        assert resolution.source_branch == "feature/test"
        assert resolution.target_branch == "main"
        assert resolution.status == "pending"

    def test_merge_resolution_from_row(self, tmp_path) -> None:
        """Test MergeResolution.from_row() creates instance from database row."""
        from gobby.storage.merge_resolutions import MergeResolution

        db_path = tmp_path / "resolution_from_row.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        # Insert test data
        db.execute(
            """INSERT INTO merge_resolutions (id, worktree_id, source_branch, target_branch, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("mr-1", "wt-1", "feature/test", "main", "pending"),
        )

        row = db.fetchone("SELECT * FROM merge_resolutions WHERE id = ?", ("mr-1",))
        resolution = MergeResolution.from_row(row)

        assert resolution.id == "mr-1"
        assert resolution.worktree_id == "wt-1"
        assert resolution.source_branch == "feature/test"

    def test_merge_resolution_to_dict(self) -> None:
        """Test MergeResolution.to_dict() returns proper dictionary."""
        from gobby.storage.merge_resolutions import MergeResolution

        resolution = MergeResolution(
            id="mr-1",
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
            status="resolved",
            tier_used="conflict_only_ai",
            created_at="2026-01-08T00:00:00Z",
            updated_at="2026-01-08T00:00:00Z",
        )
        result = resolution.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == "mr-1"
        assert result["worktree_id"] == "wt-1"
        assert result["status"] == "resolved"
        assert result["tier_used"] == "conflict_only_ai"


# =============================================================================
# MergeConflict Dataclass Tests
# =============================================================================


class TestMergeConflictDataclass:
    """Tests for MergeConflict dataclass."""

    def test_merge_conflict_has_required_fields(self) -> None:
        """Test that MergeConflict has all required fields."""
        from gobby.storage.merge_resolutions import MergeConflict

        conflict = MergeConflict(
            id="mc-1",
            resolution_id="mr-1",
            file_path="src/main.py",
            status="pending",
            ours_content="our code",
            theirs_content="their code",
            resolved_content=None,
            created_at="2026-01-08T00:00:00Z",
            updated_at="2026-01-08T00:00:00Z",
        )
        assert conflict.id == "mc-1"
        assert conflict.resolution_id == "mr-1"
        assert conflict.file_path == "src/main.py"
        assert conflict.status == "pending"

    def test_merge_conflict_from_row(self, tmp_path) -> None:
        """Test MergeConflict.from_row() creates instance from database row."""
        from gobby.storage.merge_resolutions import MergeConflict

        db_path = tmp_path / "conflict_from_row.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )
        db.execute(
            """INSERT INTO merge_resolutions (id, worktree_id, source_branch, target_branch, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("mr-1", "wt-1", "feature/test", "main", "pending"),
        )
        db.execute(
            """INSERT INTO merge_conflicts (id, resolution_id, file_path, status, ours_content, theirs_content, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("mc-1", "mr-1", "src/main.py", "pending", "our code", "their code"),
        )

        row = db.fetchone("SELECT * FROM merge_conflicts WHERE id = ?", ("mc-1",))
        conflict = MergeConflict.from_row(row)

        assert conflict.id == "mc-1"
        assert conflict.resolution_id == "mr-1"
        assert conflict.file_path == "src/main.py"

    def test_merge_conflict_to_dict(self) -> None:
        """Test MergeConflict.to_dict() returns proper dictionary."""
        from gobby.storage.merge_resolutions import MergeConflict

        conflict = MergeConflict(
            id="mc-1",
            resolution_id="mr-1",
            file_path="src/main.py",
            status="resolved",
            ours_content="our code",
            theirs_content="their code",
            resolved_content="merged code",
            created_at="2026-01-08T00:00:00Z",
            updated_at="2026-01-08T00:00:00Z",
        )
        result = conflict.to_dict()

        assert isinstance(result, dict)
        assert result["id"] == "mc-1"
        assert result["file_path"] == "src/main.py"
        assert result["resolved_content"] == "merged code"


# =============================================================================
# MergeResolutionManager CRUD Tests
# =============================================================================


class TestMergeResolutionManagerCreate:
    """Tests for MergeResolutionManager.create_resolution()."""

    def test_create_resolution(self, tmp_path) -> None:
        """Test create_resolution creates a new merge resolution."""
        from gobby.storage.merge_resolutions import MergeResolution, MergeResolutionManager

        db_path = tmp_path / "create_resolution.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        assert isinstance(resolution, MergeResolution)
        assert resolution.worktree_id == "wt-1"
        assert resolution.source_branch == "feature/test"
        assert resolution.target_branch == "main"
        assert resolution.status == "pending"
        assert resolution.id is not None

    def test_create_resolution_persists_to_database(self, tmp_path) -> None:
        """Test that create_resolution saves to database."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "persist_resolution.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        # Verify in database
        row = db.fetchone("SELECT * FROM merge_resolutions WHERE id = ?", (resolution.id,))
        assert row is not None
        assert row["source_branch"] == "feature/test"


class TestMergeResolutionManagerGet:
    """Tests for MergeResolutionManager.get_resolution()."""

    def test_get_resolution_by_id(self, tmp_path) -> None:
        """Test get_resolution returns resolution by ID."""
        from gobby.storage.merge_resolutions import MergeResolution, MergeResolutionManager

        db_path = tmp_path / "get_resolution.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        created = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        retrieved = manager.get_resolution(created.id)

        assert retrieved is not None
        assert isinstance(retrieved, MergeResolution)
        assert retrieved.id == created.id

    def test_get_resolution_returns_none_for_nonexistent(self, tmp_path) -> None:
        """Test get_resolution returns None for nonexistent ID."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "get_none.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        manager = MergeResolutionManager(db)
        result = manager.get_resolution("nonexistent-id")

        assert result is None


class TestMergeResolutionManagerMergeLookup:
    """Tests for exact merge lookup and idempotent creation helpers."""

    def _manager_with_worktree(self, tmp_path, db_name: str):
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / db_name
        db = HubDatabase(db_path)
        run_migrations(db)
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )
        return MergeResolutionManager(db), db

    def test_get_resolution_for_merge_returns_newest_exact_match(self, tmp_path) -> None:
        """Exact merge lookup returns the newest worktree/source/target row."""
        manager, db = self._manager_with_worktree(tmp_path, "lookup_exact.db")
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )
        db.execute(
            """INSERT INTO merge_resolutions
               (id, worktree_id, source_branch, target_branch, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "mr-newer",
                "wt-1",
                "feature/test",
                "main",
                "resolved",
                "2099-01-01T00:00:00+00:00",
                "2099-01-01T00:00:00+00:00",
            ),
        )

        result = manager.get_resolution_for_merge(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        assert result is not None
        assert result.id == "mr-newer"

    def test_get_or_create_resolution_reuses_duplicate_retry(self, tmp_path, monkeypatch) -> None:
        """Duplicate insert races are retried as exact-match reuse."""
        manager, _db = self._manager_with_worktree(tmp_path, "get_or_create_retry.db")
        existing = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )
        original_lookup = manager.get_resolution_for_merge
        stale = True

        def stale_once(worktree_id: str, source_branch: str, target_branch: str):
            nonlocal stale
            if stale:
                stale = False
                return None
            return original_lookup(worktree_id, source_branch, target_branch)

        monkeypatch.setattr(manager, "get_resolution_for_merge", stale_once)

        result, created = manager.get_or_create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        assert created is False
        assert result.id == existing.id

    def test_different_source_or_target_is_not_compatible(self, tmp_path) -> None:
        """Exact lookup does not reuse rows for different branches."""
        manager, _db = self._manager_with_worktree(tmp_path, "lookup_incompatible.db")
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        different_target = manager.get_resolution_for_merge(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="develop",
        )
        different_source = manager.get_resolution_for_merge(
            worktree_id="wt-1",
            source_branch="feature/other",
            target_branch="main",
        )

        assert different_target is None
        assert different_source is None


class TestMergeResolutionManagerUpdate:
    """Tests for MergeResolutionManager.update_resolution()."""

    def test_update_resolution_status(self, tmp_path) -> None:
        """Test update_resolution changes status."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "update_resolution.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        updated = manager.update_resolution(
            resolution.id,
            status="resolved",
            tier_used="conflict_only_ai",
        )

        assert updated is not None
        assert updated.status == "resolved"
        assert updated.tier_used == "conflict_only_ai"

    def test_update_resolution_persists_changes(self, tmp_path) -> None:
        """Test that update_resolution saves changes to database."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "persist_update.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        manager.update_resolution(resolution.id, status="resolved")

        # Verify in database
        row = db.fetchone("SELECT * FROM merge_resolutions WHERE id = ?", (resolution.id,))
        assert row["status"] == "resolved"


class TestMergeResolutionManagerDelete:
    """Tests for MergeResolutionManager.delete_resolution()."""

    def test_delete_resolution(self, tmp_path) -> None:
        """Test delete_resolution removes resolution."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "delete_resolution.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        result = manager.delete_resolution(resolution.id)

        assert result is True
        assert manager.get_resolution(resolution.id) is None

    def test_delete_nonexistent_resolution(self, tmp_path) -> None:
        """Test delete_resolution returns False for nonexistent ID."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "delete_none.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        manager = MergeResolutionManager(db)
        result = manager.delete_resolution("nonexistent-id")

        assert result is False


# =============================================================================
# MergeConflict CRUD Tests
# =============================================================================


class TestMergeResolutionManagerCreateConflict:
    """Tests for MergeResolutionManager.create_conflict()."""

    def test_create_conflict(self, tmp_path) -> None:
        """Test create_conflict creates a new merge conflict."""
        from gobby.storage.merge_resolutions import MergeConflict, MergeResolutionManager

        db_path = tmp_path / "create_conflict.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        conflict = manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="our code",
            theirs_content="their code",
        )

        assert isinstance(conflict, MergeConflict)
        assert conflict.resolution_id == resolution.id
        assert conflict.file_path == "src/main.py"
        assert conflict.status == "pending"


class TestMergeResolutionManagerUpdateConflict:
    """Tests for MergeResolutionManager.update_conflict()."""

    def test_update_conflict_status(self, tmp_path) -> None:
        """Test update_conflict changes conflict status."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "update_conflict.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )
        conflict = manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="our code",
            theirs_content="their code",
        )

        updated = manager.update_conflict(
            conflict.id,
            status="resolved",
            resolved_content="merged code",
        )

        assert updated is not None
        assert updated.status == "resolved"
        assert updated.resolved_content == "merged code"


# =============================================================================
# Conflict State Transition Tests
# =============================================================================


class TestConflictStateTransitions:
    """Tests for conflict state transitions."""

    def test_transition_pending_to_resolved(self, tmp_path) -> None:
        """Test conflict can transition from pending to resolved."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "transition_resolved.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )
        conflict = manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="our code",
            theirs_content="their code",
        )

        assert conflict.status == "pending"

        updated = manager.update_conflict(
            conflict.id,
            status="resolved",
            resolved_content="merged code",
        )

        assert updated.status == "resolved"

    def test_transition_pending_to_failed(self, tmp_path) -> None:
        """Test conflict can transition from pending to failed."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "transition_failed.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )
        conflict = manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="our code",
            theirs_content="their code",
        )

        updated = manager.update_conflict(conflict.id, status="failed")

        assert updated.status == "failed"

    def test_transition_pending_to_human_review(self, tmp_path) -> None:
        """Test conflict can transition from pending to human_review."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "transition_human.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )
        conflict = manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="our code",
            theirs_content="their code",
        )

        updated = manager.update_conflict(conflict.id, status="human_review")

        assert updated.status == "human_review"


# =============================================================================
# Query Tests
# =============================================================================


class TestQueryResolutionsByFile:
    """Tests for querying resolutions by file."""

    def test_list_conflicts_by_file_path(self, tmp_path) -> None:
        """Test list_conflicts filters by file_path."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "query_file.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="code 1",
            theirs_content="code 2",
        )
        manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/utils.py",
            ours_content="code 3",
            theirs_content="code 4",
        )

        results = manager.list_conflicts(file_path="src/main.py")

        assert len(results) == 1
        assert results[0].file_path == "src/main.py"


class TestQueryResolutionsByBranch:
    """Tests for querying resolutions by branch."""

    def test_list_resolutions_by_source_branch(self, tmp_path) -> None:
        """Test list_resolutions filters by source_branch."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "query_branch.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/auth",
            target_branch="main",
        )
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/api",
            target_branch="main",
        )

        results = manager.list_resolutions(source_branch="feature/auth")

        assert len(results) == 1
        assert results[0].source_branch == "feature/auth"

    def test_list_resolutions_by_target_branch(self, tmp_path) -> None:
        """Test list_resolutions filters by target_branch."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "query_target_branch.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/auth",
            target_branch="main",
        )
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/api",
            target_branch="develop",
        )

        results = manager.list_resolutions(target_branch="main")

        assert len(results) == 1
        assert results[0].target_branch == "main"


class TestQueryResolutionsByStatus:
    """Tests for querying resolutions by status."""

    def test_list_resolutions_by_status(self, tmp_path) -> None:
        """Test list_resolutions filters by status."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "query_status.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        res1 = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/auth",
            target_branch="main",
        )
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/api",
            target_branch="main",
        )  # Second resolution, not updated (remains pending)

        manager.update_resolution(res1.id, status="resolved")

        results = manager.list_resolutions(status="resolved")

        assert len(results) == 1
        assert results[0].status == "resolved"

    def test_list_conflicts_by_status(self, tmp_path) -> None:
        """Test list_conflicts filters by status."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "query_conflict_status.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        c1 = manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="code 1",
            theirs_content="code 2",
        )
        manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/utils.py",
            ours_content="code 3",
            theirs_content="code 4",
        )

        manager.update_conflict(c1.id, status="resolved", resolved_content="merged")

        results = manager.list_conflicts(status="pending")

        assert len(results) == 1
        assert results[0].file_path == "src/utils.py"


# =============================================================================
# Resolution History Tracking Tests
# =============================================================================


class TestResolutionHistoryTracking:
    """Tests for tracking resolution history."""

    def test_resolution_has_timestamps(self, tmp_path) -> None:
        """Test that resolutions track created_at and updated_at."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "timestamps.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        assert resolution.created_at is not None
        assert resolution.updated_at is not None

    def test_update_changes_updated_at(self, tmp_path) -> None:
        """Test that updating a resolution changes updated_at."""

        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "update_timestamp.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        original_updated_at = resolution.updated_at

        updated = manager.update_resolution(resolution.id, status="resolved")

        assert updated.updated_at != original_updated_at

    def test_get_conflicts_for_resolution(self, tmp_path) -> None:
        """Test getting all conflicts for a resolution."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "conflicts_for_resolution.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create prerequisites
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature", "/tmp/wt", "active"),
        )

        manager = MergeResolutionManager(db)
        resolution = manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/test",
            target_branch="main",
        )

        manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/main.py",
            ours_content="code 1",
            theirs_content="code 2",
        )
        manager.create_conflict(
            resolution_id=resolution.id,
            file_path="src/utils.py",
            ours_content="code 3",
            theirs_content="code 4",
        )

        results = manager.list_conflicts(resolution_id=resolution.id)

        assert len(results) == 2

    def test_list_resolutions_by_worktree(self, tmp_path) -> None:
        """Test listing resolutions by worktree."""
        from gobby.storage.merge_resolutions import MergeResolutionManager

        db_path = tmp_path / "resolutions_by_worktree.db"
        db = HubDatabase(db_path)
        run_migrations(db)

        # Create multiple worktrees
        db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-1", "Test Project"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-1", "proj-1", "feature1", "/tmp/wt1", "active"),
        )
        db.execute(
            """INSERT INTO worktrees (id, project_id, branch_name, worktree_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("wt-2", "proj-1", "feature2", "/tmp/wt2", "active"),
        )

        manager = MergeResolutionManager(db)
        manager.create_resolution(
            worktree_id="wt-1",
            source_branch="feature/auth",
            target_branch="main",
        )
        manager.create_resolution(
            worktree_id="wt-2",
            source_branch="feature/api",
            target_branch="main",
        )

        results = manager.list_resolutions(worktree_id="wt-1")

        assert len(results) == 1
        assert results[0].worktree_id == "wt-1"
