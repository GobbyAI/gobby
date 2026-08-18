"""Tests for project storage layer.

Tests cover:
- Soft delete (sets deleted_at, filters from list/get_by_name)
- resolve_ref (UUID and name resolution, excludes deleted)
- is_protected (system project detection)
- Constants (ORPHANED_PROJECT_ID, PERSONAL_PROJECT_ID, SYSTEM_PROJECT_NAMES)
"""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.paths import (
    FilesHomeError,
    FilesHomeNotOnThisDaemonError,
    get_gobby_home,
)
from gobby.runner_pid_file import PidFileClaim, claim_pid_file
from gobby.storage.projects import (
    ORPHANED_PROJECT_ID,
    PERSONAL_PROJECT_ID,
    SYSTEM_PROJECT_NAMES,
    LocalProjectManager,
    ensure_personal_project,
    ensure_personal_project_identity,
    personal_project_path,
)

pytestmark = pytest.mark.unit


def _write_local_bootstrap(files_home: Path) -> Path:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        f"datastore_mode: local\nfiles_home: {files_home}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    return bootstrap


def _write_remote_bootstrap() -> Path:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    return bootstrap


@pytest.fixture(autouse=True)
def _restore_bootstrap() -> Iterator[None]:
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    previous = bootstrap.read_bytes() if bootstrap.exists() else None
    yield
    if previous is None:
        bootstrap.unlink(missing_ok=True)
    else:
        bootstrap.write_bytes(previous)
        bootstrap.chmod(0o600)


def _hold_maintenance() -> PidFileClaim:
    claim = claim_pid_file(get_gobby_home() / "gobby.pid", role="maintenance")
    assert claim is not None
    return claim


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

    def test_personal_project_path(self, tmp_path: Path) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)

        path = personal_project_path()

        assert path == files_home / "_personal"
        assert path != get_gobby_home() / "personal"
        assert personal_project_path(tmp_path) == files_home / "_personal"


class TestPersonalProjectEnsure:
    """Tests for the backed personal system project."""

    def test_identity_helper_creates_marker_without_database(self, tmp_path: Path) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        claim = _hold_maintenance()
        try:
            project_file = ensure_personal_project_identity()
        finally:
            claim.release()

        data = json.loads(project_file.read_text())
        assert project_file == files_home / "_personal" / ".gobby" / "project.json"
        assert data == {
            "id": PERSONAL_PROJECT_ID,
            "name": "_personal",
            "created_at": data["created_at"],
        }
        assert not (get_gobby_home() / "personal").exists()

    def test_identity_helper_requires_held_singleton(self, tmp_path: Path) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)

        with pytest.raises(RuntimeError, match="held singleton"):
            ensure_personal_project_identity()

        assert not (files_home / "_personal").exists()

    def test_identity_helper_propagates_write_failure(
        self,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        claim = _hold_maintenance()
        try:
            with (
                patch(
                    "gobby.paths.publish_files_home_descendant",
                    side_effect=FilesHomeError("read-only marker"),
                ),
                pytest.raises(FilesHomeError, match="read-only marker"),
            ):
                ensure_personal_project_identity()
        finally:
            claim.release()

        assert not (files_home / "_personal" / ".gobby" / "project.json").exists()

    def test_ensure_personal_project_creates_folder_and_repo_path(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        expected_path = files_home / "_personal"
        claim = _hold_maintenance()
        try:
            project = ensure_personal_project(project_manager.db)
            second = ensure_personal_project(project_manager.db)
        finally:
            claim.release()

        assert expected_path.is_dir()
        assert project.id == PERSONAL_PROJECT_ID
        assert project.name == "_personal"
        assert project.repo_path == str(expected_path)
        assert second.id == project.id
        assert second.repo_path == str(expected_path)
        assert not (get_gobby_home() / "personal").exists()

    def test_ensure_personal_project_repairs_stale_repo_path(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        project_manager.ensure_exists(PERSONAL_PROJECT_ID, "_personal")
        project_manager.update(PERSONAL_PROJECT_ID, repo_path=None)
        claim = _hold_maintenance()
        try:
            project = ensure_personal_project(project_manager.db)
        finally:
            claim.release()

        assert project.repo_path == str(files_home / "_personal")

    def test_ensure_personal_project_repairs_name_and_deleted_at(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        project_manager.ensure_exists(
            PERSONAL_PROJECT_ID,
            "wrong-name",
            repo_path="/stale",
        )
        project_manager.soft_delete(PERSONAL_PROJECT_ID)
        claim = _hold_maintenance()
        try:
            project = ensure_personal_project(project_manager.db)
        finally:
            claim.release()

        assert project.name == "_personal"
        assert project.repo_path == str(files_home / "_personal")
        assert project.deleted_at is None

    def test_ensure_personal_project_materializes_on_disk_identity(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        """gwiki/gcode read identity from .gobby/project.json, not the DB."""
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        claim = _hold_maintenance()
        try:
            ensure_personal_project(project_manager.db)
        finally:
            claim.release()

        project_file = files_home / "_personal" / ".gobby" / "project.json"
        assert project_file.is_file()
        data = json.loads(project_file.read_text())
        assert data["id"] == PERSONAL_PROJECT_ID
        assert data["name"] == "_personal"
        assert data["created_at"]

    def test_ensure_personal_project_preserves_valid_identity_file(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        gobby_dir = files_home / "_personal" / ".gobby"
        gobby_dir.mkdir(parents=True)
        existing = {
            "id": PERSONAL_PROJECT_ID,
            "name": "_personal",
            "created_at": "2026-01-01T00:00:00+00:00",
            "hooks_disabled": True,
        }
        project_file = gobby_dir / "project.json"
        project_file.write_text(json.dumps(existing, indent=2) + "\n")
        before = project_file.read_text()
        claim = _hold_maintenance()
        try:
            ensure_personal_project(project_manager.db)
        finally:
            claim.release()

        assert project_file.read_text() == before

    def test_ensure_personal_project_repairs_corrupt_identity_file(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        gobby_dir = files_home / "_personal" / ".gobby"
        gobby_dir.mkdir(parents=True)
        project_file = gobby_dir / "project.json"
        project_file.write_text("{not json")
        claim = _hold_maintenance()
        try:
            ensure_personal_project(project_manager.db)
        finally:
            claim.release()

        data = json.loads(project_file.read_text())
        assert data["id"] == PERSONAL_PROJECT_ID

    def test_ensure_personal_project_repairs_wrong_identity_file(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)
        gobby_dir = files_home / "_personal" / ".gobby"
        gobby_dir.mkdir(parents=True)
        project_file = gobby_dir / "project.json"
        project_file.write_text(json.dumps({"id": "some-other-project"}) + "\n")
        claim = _hold_maintenance()
        try:
            ensure_personal_project(project_manager.db)
        finally:
            claim.release()

        data = json.loads(project_file.read_text())
        assert data["id"] == PERSONAL_PROJECT_ID
        assert data["name"] == "_personal"

    def test_claimless_ensure_upserts_sentinel_without_filesystem(
        self,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        files_home = tmp_path / "files_home"
        files_home.mkdir()
        _write_local_bootstrap(files_home)

        project = ensure_personal_project(project_manager.db)

        assert project.id == PERSONAL_PROJECT_ID
        assert project.name == "_personal"
        assert not (files_home / "_personal").exists()
        assert not (get_gobby_home() / "personal").exists()

    def test_personal_project_path_raises_on_remote(self, tmp_path: Path) -> None:
        _write_remote_bootstrap()

        with pytest.raises(FilesHomeNotOnThisDaemonError):
            personal_project_path()


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

    def test_restore_makes_soft_deleted_project_active(
        self, project_manager: LocalProjectManager
    ) -> None:
        project = project_manager.create(name="restored-proj", repo_path="/tmp/restored")
        project_manager.soft_delete(project.id)

        restored = project_manager.restore(project.id)

        assert restored is not None
        assert restored.id == project.id
        assert restored.deleted_at is None
        assert project_manager.get_by_name("restored-proj") is not None

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
        result = project_manager.soft_delete("00000000-0000-0000-0000-0000000000ff")
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
