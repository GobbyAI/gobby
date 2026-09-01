"""Tests for the LocalProjectManager storage layer."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import (
    LocalProjectCheckoutManager,
    OverlayRegistrationRejectedError,
)
from gobby.storage.projects import IsolatedAgentProjectPathError, LocalProjectManager, Project
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    patch_local_machine_id,
    write_project_marker,
)

pytestmark = pytest.mark.unit

# projects.id is a native uuid column; a valid-but-unknown UUID exercises the
# "nonexistent project" paths without tripping the uuid cast.
MISSING_PROJECT_ID = str(uuid.uuid4())


class TestProject:
    """Tests for Project dataclass."""

    def test_from_row(self, project_manager: LocalProjectManager) -> None:
        """Test creating Project from database row."""
        # Create a project first
        project = project_manager.create(name="test-project")

        # Fetch raw row
        row = project_manager.db.fetchone("SELECT * FROM projects WHERE id = %s", (project.id,))
        assert row is not None

        # Create from row
        project_from_row = Project.from_row(row)
        assert project_from_row.id == project.id
        assert project_from_row.name == "test-project"

    def test_to_dict(self, project_manager: LocalProjectManager) -> None:
        """Test converting Project to dictionary."""
        project = project_manager.create(
            name="test-project",
            github_url="https://github.com/test/repo",
        )

        d = project.to_dict()
        assert d["id"] == project.id
        assert d["name"] == "test-project"
        assert "repo_path" not in d
        assert d["github_url"] == "https://github.com/test/repo"
        assert d["linear_project_id"] is None
        assert "created_at" in d
        assert "updated_at" in d


class TestLocalProjectManager:
    """Tests for LocalProjectManager class."""

    def test_create_project(self, project_manager: LocalProjectManager) -> None:
        """Test creating a new project."""
        project = project_manager.create(
            name="my-project",
            github_url="https://github.com/user/repo",
        )

        assert project.id is not None
        assert project.name == "my-project"
        assert project.repo_path is None
        assert project.github_url == "https://github.com/user/repo"

    def test_create_project_minimal(self, project_manager: LocalProjectManager) -> None:
        """Test creating a project with only required fields."""
        project = project_manager.create(name="minimal-project")

        assert project.id is not None
        assert project.name == "minimal-project"
        assert project.repo_path is None
        assert project.github_url is None

    def test_create_duplicate_name_raises(self, project_manager: LocalProjectManager) -> None:
        """Test that creating a project with duplicate name raises error."""
        project_manager.create(name="unique-project")

        with pytest.raises(UniqueViolation):
            project_manager.create(name="unique-project")

    def test_create_reuses_name_from_soft_deleted_project(
        self, project_manager: LocalProjectManager
    ) -> None:
        deleted = project_manager.create(name="reusable-project")
        project_manager.soft_delete(deleted.id)

        created = project_manager.create(name="reusable-project")

        assert created.id != deleted.id
        assert project_manager.get_by_name("reusable-project") == created

    def test_get_project(self, project_manager: LocalProjectManager) -> None:
        """Test getting a project by ID."""
        created = project_manager.create(name="get-test")

        retrieved = project_manager.get(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.name == "get-test"

    def test_get_nonexistent_project(self, project_manager: LocalProjectManager) -> None:
        """Test getting a nonexistent project returns None."""
        result = project_manager.get(MISSING_PROJECT_ID)
        assert result is None

    def test_get_by_name(self, project_manager: LocalProjectManager) -> None:
        """Test getting a project by name."""
        created = project_manager.create(name="named-project")

        retrieved = project_manager.get_by_name("named-project")
        assert retrieved is not None
        assert retrieved.id == created.id

    def test_get_by_name_nonexistent(self, project_manager: LocalProjectManager) -> None:
        """Test getting nonexistent project by name returns None."""
        result = project_manager.get_by_name("nonexistent")
        assert result is None

    def test_get_or_create_existing(self, project_manager: LocalProjectManager) -> None:
        """Test get_or_create returns existing project."""
        created = project_manager.create(name="existing-project")

        retrieved = project_manager.get_or_create(name="existing-project")
        assert retrieved.id == created.id

    def test_get_or_create_new(self, project_manager: LocalProjectManager) -> None:
        """Test get_or_create creates new project."""
        result = project_manager.get_or_create(name="new-project")

        assert result.name == "new-project"
        assert result.repo_path in (None, "")

    def test_get_or_create_is_atomic_for_concurrent_calls(
        self,
        project_manager: LocalProjectManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_get_by_name = project_manager.get_by_name
        lookups_ready = threading.Barrier(2)

        def synchronized_get_by_name(name: str, include_deleted: bool = False) -> Project | None:
            lookups_ready.wait(timeout=5)
            return original_get_by_name(name, include_deleted)

        monkeypatch.setattr(project_manager, "get_by_name", synchronized_get_by_name)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: project_manager.get_or_create(name="concurrent-project"),
                    range(2),
                )
            )

        assert results[0].id == results[1].id
        row = project_manager.db.fetchone(
            "SELECT COUNT(*) AS count FROM projects WHERE name = %s AND deleted_at IS NULL",
            ("concurrent-project",),
        )
        assert row is not None
        assert row["count"] == 1

    def test_ensure_exists_raises_on_conflicting_active_name(
        self, project_manager: LocalProjectManager
    ) -> None:
        project_manager.create(name="synced-project")

        with pytest.raises(UniqueViolation):
            project_manager.ensure_exists(str(uuid.uuid4()), "synced-project")

    def test_ensure_exists_keeps_database_name(self, project_manager: LocalProjectManager) -> None:
        project_id = str(uuid.uuid4())
        project_manager.ensure_exists(project_id, "old-name")

        ensured = project_manager.ensure_exists(project_id, "new-name")

        assert ensured.id == project_id
        assert ensured.name == "old-name"
        assert ensured.repo_path is None

    def test_list_projects(self, project_manager: LocalProjectManager) -> None:
        """Test listing all projects."""
        project_manager.create(name="alpha")
        project_manager.create(name="beta")
        project_manager.create(name="gamma")

        projects = project_manager.list()
        # Filter out migration placeholder projects
        user_projects = [p for p in projects if not p.name.startswith("_")]
        assert len(user_projects) == 3
        # Should be sorted by name
        names = [p.name for p in user_projects]
        assert names == ["alpha", "beta", "gamma"]

    def test_list_empty(self, project_manager: LocalProjectManager) -> None:
        """Test listing projects when no user projects exist."""
        projects = project_manager.list()
        # May contain migration placeholder projects (_orphaned, _migrated)
        user_projects = [p for p in projects if not p.name.startswith("_")]
        assert user_projects == []

    def test_update_project(self, project_manager: LocalProjectManager) -> None:
        """Test updating project fields."""
        created = project_manager.create(name="original")

        updated = project_manager.update(
            created.id,
            name="updated",
        )

        assert updated is not None
        assert updated.name == "updated"
        assert updated.repo_path is None

    def test_update_linear_project_id(self, project_manager: LocalProjectManager) -> None:
        """Test updating the Linear project binding."""
        created = project_manager.create(name="linear-bound")

        updated = project_manager.update(created.id, linear_project_id="lin-proj")

        assert updated is not None
        assert updated.linear_project_id == "lin-proj"

    def test_update_partial(self, project_manager: LocalProjectManager) -> None:
        """Test updating only some fields."""
        created = project_manager.create(name="partial")

        updated = project_manager.update(
            created.id,
            github_url="https://github.com/new/url",
        )

        assert updated is not None
        assert updated.name == "partial"  # unchanged
        assert updated.repo_path is None
        assert updated.github_url == "https://github.com/new/url"

    def test_update_nonexistent(self, project_manager: LocalProjectManager) -> None:
        """Test updating nonexistent project returns None."""
        result = project_manager.update(MISSING_PROJECT_ID, name="new-name")
        assert result is None

    def test_update_no_fields(self, project_manager: LocalProjectManager) -> None:
        """Test update with no fields returns existing project."""
        created = project_manager.create(name="no-change")

        result = project_manager.update(created.id)
        assert result is not None
        assert result.id == created.id

    def test_update_ignores_invalid_fields(self, project_manager: LocalProjectManager) -> None:
        """Test that update ignores fields not in allowed list."""
        created = project_manager.create(name="ignore-invalid")

        result = project_manager.update(
            created.id,
            invalid_field="should be ignored",
        )

        assert result is not None
        assert result.id == created.id

    def test_delete_project(self, project_manager: LocalProjectManager) -> None:
        """Test deleting a project."""
        created = project_manager.create(name="to-delete")

        result = project_manager.delete(created.id)
        assert result is True

        # Should no longer exist
        assert project_manager.get(created.id) is None

    def test_delete_nonexistent(self, project_manager: LocalProjectManager) -> None:
        """Test deleting nonexistent project returns False."""
        result = project_manager.delete(MISSING_PROJECT_ID)
        assert result is False


def _checkout_row(db: HubDatabase, machine_id: str, project_id: str) -> dict[str, object] | None:
    row = db.fetchone(
        """
        SELECT root_path FROM project_checkouts
        WHERE machine_id = %s AND project_id = %s
        """,
        (machine_id, project_id),
    )
    return dict(row) if row is not None else None


def test_create_writes_checkout_not_repo_path(
    project_manager: LocalProjectManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(project_manager.db)
    patch_local_machine_id(monkeypatch, machine_id)
    root = tmp_path / "created-root"
    root.mkdir()
    project_id = str(uuid.uuid4())
    write_project_marker(root, project_id=project_id, name="checkout-create")
    monkeypatch.setattr("gobby.storage.projects.uuid.uuid4", lambda: uuid.UUID(project_id))
    project = project_manager.create(name="checkout-create", repo_path=str(root))
    assert project.id == project_id
    assert project.repo_path is None
    stored = project_manager.db.fetchone(
        "SELECT repo_path FROM projects WHERE id = %s", (project.id,)
    )
    assert stored is not None
    assert stored["repo_path"] in (None, "")
    checkout = _checkout_row(project_manager.db, machine_id, project.id)
    assert checkout is not None
    assert checkout["root_path"] == str(root)


def test_ensure_exists_writes_checkout_not_repo_path(
    project_manager: LocalProjectManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(project_manager.db)
    patch_local_machine_id(monkeypatch, machine_id)
    root = tmp_path / "ensured-root"
    root.mkdir()
    project_id = str(uuid.uuid4())
    write_project_marker(root, project_id=project_id, name="checkout-ensure")
    project = project_manager.ensure_exists(project_id, "checkout-ensure", str(root))
    assert project.id == project_id
    assert project.repo_path is None
    checkout = _checkout_row(project_manager.db, machine_id, project.id)
    assert checkout is not None
    assert checkout["root_path"] == str(root)


def test_update_writes_checkout_not_repo_path(
    project_manager: LocalProjectManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(project_manager.db)
    patch_local_machine_id(monkeypatch, machine_id)
    created = project_manager.create(name="checkout-update")
    root = tmp_path / "updated-root"
    root.mkdir()
    write_project_marker(root, project_id=created.id, name="checkout-update")
    updated = project_manager.update(created.id, repo_path=str(root))
    assert updated is not None
    assert updated.repo_path is None
    checkout = _checkout_row(project_manager.db, machine_id, created.id)
    assert checkout is not None
    assert checkout["root_path"] == str(root)


def test_create_refuses_overlay_path(
    project_manager: LocalProjectManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(project_manager.db)
    patch_local_machine_id(monkeypatch, machine_id)
    owner = project_manager.create(name="overlay-owner")
    overlay = tmp_path / "wt"
    overlay.mkdir()
    project_id = str(uuid.uuid4())
    write_project_marker(overlay, project_id=project_id, name="overlay-create")
    monkeypatch.setattr("gobby.storage.projects.uuid.uuid4", lambda: uuid.UUID(project_id))
    insert_overlay(
        project_manager.db,
        project_id=owner.id,
        machine_id=machine_id,
        path=str(overlay),
        kind="worktree",
    )
    with pytest.raises((OverlayRegistrationRejectedError, IsolatedAgentProjectPathError)):
        project_manager.create(name="overlay-create", repo_path=str(overlay))
    assert _checkout_row(project_manager.db, machine_id, project_id) is None


def test_sample_project_uses_isolated_machine_helper(
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
) -> None:
    from gobby.utils.project_context import get_project_context

    assert "repo_path" not in sample_project
    checkout = LocalProjectCheckoutManager(project_manager.db).get(
        _sample_checkout_machine(project_manager.db, sample_project["id"]),
        sample_project["id"],
    )
    assert checkout is not None
    marker = get_project_context(Path(checkout.root_path))
    assert marker is not None
    assert marker["id"] == sample_project["id"]
    machine = project_manager.db.fetchone(
        "SELECT 1 FROM machines WHERE id = %s", (checkout.machine_id,)
    )
    assert machine is not None


def _sample_checkout_machine(db: HubDatabase, project_id: str) -> str:
    row = db.fetchone(
        "SELECT machine_id FROM project_checkouts WHERE project_id = %s",
        (project_id,),
    )
    assert row is not None
    return str(row["machine_id"])
