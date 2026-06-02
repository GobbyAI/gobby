"""Tests for project storage layer.

Tests cover:
- Soft delete (sets deleted_at, filters from list/get_by_name)
- resolve_ref (UUID and name resolution, excludes deleted)
- is_protected (system project detection)
- Constants (ORPHANED_PROJECT_ID, PERSONAL_PROJECT_ID, SYSTEM_PROJECT_NAMES)
"""

import pytest

from gobby.storage.projects import (
    ORPHANED_PROJECT_ID,
    PERSONAL_PROJECT_ID,
    SYSTEM_PROJECT_NAMES,
    LocalProjectManager,
    ensure_personal_project,
    personal_project_path,
)

pytestmark = pytest.mark.unit


class TestConstants:
    """Test project constants."""

    def test_orphaned_project_id(self) -> None:
        assert ORPHANED_PROJECT_ID == "00000000-0000-0000-0000-000000000000"

    def test_personal_project_id(self) -> None:
        assert PERSONAL_PROJECT_ID == "00000000-0000-0000-0000-000000060887"

    def test_system_project_names(self) -> None:
        assert "_orphaned" in SYSTEM_PROJECT_NAMES
        assert "_migrated" in SYSTEM_PROJECT_NAMES
        assert "_personal" in SYSTEM_PROJECT_NAMES
        assert "gobby" in SYSTEM_PROJECT_NAMES
        assert "random-project" not in SYSTEM_PROJECT_NAMES

    def test_personal_project_path(self, tmp_path) -> None:
        assert personal_project_path(tmp_path) == tmp_path / "personal"


class TestPersonalProjectEnsure:
    """Tests for the backed personal system project."""

    def test_ensure_personal_project_creates_folder_and_repo_path(
        self,
        project_manager: LocalProjectManager,
        tmp_path,
    ) -> None:
        project = ensure_personal_project(project_manager.db, gobby_home=tmp_path)

        expected_path = tmp_path / "personal"
        assert expected_path.is_dir()
        assert project.id == PERSONAL_PROJECT_ID
        assert project.name == "_personal"
        assert project.repo_path == str(expected_path)

        second = ensure_personal_project(project_manager.db, gobby_home=tmp_path)
        assert second.id == project.id
        assert second.repo_path == str(expected_path)

    def test_ensure_personal_project_repairs_stale_repo_path(
        self,
        project_manager: LocalProjectManager,
        tmp_path,
    ) -> None:
        project_manager.ensure_exists(PERSONAL_PROJECT_ID, "_personal")
        project_manager.update(PERSONAL_PROJECT_ID, repo_path=None)

        project = ensure_personal_project(project_manager.db, gobby_home=tmp_path)

        assert project.repo_path == str(tmp_path / "personal")


class TestSoftDelete:
    """Tests for soft-delete functionality."""

    def test_soft_delete_sets_deleted_at(self, project_manager: LocalProjectManager) -> None:
        """Soft-deleting a project sets deleted_at timestamp."""
        project = project_manager.create(name="deletable", repo_path="/tmp/deletable")

        result = project_manager.soft_delete(project.id)
        assert result is True

        # get() by ID still returns deleted projects
        deleted = project_manager.get(project.id)
        assert deleted is not None
        assert deleted.deleted_at is not None

    def test_soft_delete_hides_from_list(self, project_manager: LocalProjectManager) -> None:
        """Soft-deleted projects are hidden from list()."""
        project = project_manager.create(name="will-delete", repo_path="/tmp/wd")
        project_manager.soft_delete(project.id)

        projects = project_manager.list()
        names = [p.name for p in projects]
        assert "will-delete" not in names

    def test_soft_delete_hides_from_get_by_name(self, project_manager: LocalProjectManager) -> None:
        """Soft-deleted projects are hidden from get_by_name()."""
        project = project_manager.create(name="hidden-proj", repo_path="/tmp/hp")
        project_manager.soft_delete(project.id)

        result = project_manager.get_by_name("hidden-proj")
        assert result is None

    def test_soft_delete_include_deleted_in_list(
        self, project_manager: LocalProjectManager
    ) -> None:
        """list(include_deleted=True) shows soft-deleted projects."""
        project = project_manager.create(name="show-deleted", repo_path="/tmp/sd")
        project_manager.soft_delete(project.id)

        projects = project_manager.list(include_deleted=True)
        names = [p.name for p in projects]
        assert "show-deleted" in names

    def test_soft_delete_nonexistent_returns_false(
        self, project_manager: LocalProjectManager
    ) -> None:
        """Soft-deleting a nonexistent project returns False."""
        result = project_manager.soft_delete("nonexistent-id")
        assert result is False

    def test_soft_delete_idempotent(self, project_manager: LocalProjectManager) -> None:
        """Soft-deleting an already-deleted project returns False."""
        project = project_manager.create(name="double-delete", repo_path="/tmp/dd")
        assert project_manager.soft_delete(project.id) is True
        assert project_manager.soft_delete(project.id) is False


class TestResolveRef:
    """Tests for resolve_ref."""

    def test_resolve_by_id(self, project_manager: LocalProjectManager) -> None:
        """resolve_ref finds project by UUID."""
        project = project_manager.create(name="by-id", repo_path="/tmp/bi")
        result = project_manager.resolve_ref(project.id)
        assert result is not None
        assert result.name == "by-id"

    def test_resolve_by_name(self, project_manager: LocalProjectManager) -> None:
        """resolve_ref finds project by name."""
        project_manager.create(name="by-name", repo_path="/tmp/bn")
        result = project_manager.resolve_ref("by-name")
        assert result is not None
        assert result.name == "by-name"

    def test_resolve_excludes_deleted(self, project_manager: LocalProjectManager) -> None:
        """resolve_ref does not return soft-deleted projects."""
        project = project_manager.create(name="deleted-ref", repo_path="/tmp/dr")
        project_manager.soft_delete(project.id)

        assert project_manager.resolve_ref(project.id) is None
        assert project_manager.resolve_ref("deleted-ref") is None

    def test_resolve_not_found(self, project_manager: LocalProjectManager) -> None:
        """resolve_ref returns None for unknown refs."""
        assert project_manager.resolve_ref("nonexistent") is None


class TestIsProtected:
    """Tests for is_protected."""

    def test_system_projects_are_protected(self, project_manager: LocalProjectManager) -> None:
        """System projects (_orphaned, _migrated, _personal, gobby) are protected."""
        # _orphaned is created by migrations
        orphaned = project_manager.get_by_name("_orphaned")
        assert orphaned is not None
        assert project_manager.is_protected(orphaned) is True

    def test_regular_projects_not_protected(self, project_manager: LocalProjectManager) -> None:
        """Regular projects are not protected."""
        project = project_manager.create(name="regular", repo_path="/tmp/reg")
        assert project_manager.is_protected(project) is False


class TestProjectDeletedAtField:
    """Tests for deleted_at field on Project dataclass."""

    def test_to_dict_excludes_deleted_at_when_none(
        self, project_manager: LocalProjectManager
    ) -> None:
        """to_dict() does not include deleted_at when it's None."""
        project = project_manager.create(name="no-deleted", repo_path="/tmp/nd")
        d = project.to_dict()
        assert "deleted_at" not in d

    def test_to_dict_includes_deleted_at_when_set(
        self, project_manager: LocalProjectManager
    ) -> None:
        """to_dict() includes deleted_at when project is soft-deleted."""
        project = project_manager.create(name="has-deleted", repo_path="/tmp/hd")
        project_manager.soft_delete(project.id)
        deleted = project_manager.get(project.id)
        assert deleted is not None
        d = deleted.to_dict()
        assert "deleted_at" in d
        assert d["deleted_at"] is not None
