"""Tests for cli/sync.py — targeting uncovered lines."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.sync import sync

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# All lazy imports in sync() need to be patched at the source module:
#   from gobby.utils.dev import is_dev_mode          -> gobby.utils.dev.is_dev_mode
#   from gobby.sync.integrity import ...             -> gobby.sync.integrity.*
#   from gobby.config.app import load_config         -> gobby.config.app.load_config
#   from gobby.storage.database import LocalDatabase -> gobby.storage.database.LocalDatabase
#   from gobby.storage.migrations import run_migrations -> gobby.storage.migrations.run_migrations
#   from gobby.cli.installers.shared import sync_bundled_content_to_db -> gobby.cli.installers.shared.sync_bundled_content_to_db


# ---------------------------------------------------------------------------
# Dev mode — basic sync
# ---------------------------------------------------------------------------
class TestSyncDevMode:
    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.storage.migrations.run_migrations")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_sync_items(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_db_cls: MagicMock,
        _mig: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 5, "errors": [], "details": {}}
        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Synced 5" in result.output

    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.storage.migrations.run_migrations")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_no_changes(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_db_cls: MagicMock,
        _mig: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}
        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No changes" in result.output

    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.storage.migrations.run_migrations")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_verbose(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_db_cls: MagicMock,
        _mig: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {
            "total_synced": 2,
            "errors": [],
            "details": {"skills": {"synced": 1, "updated": 1}},
        }
        result = runner.invoke(sync, ["--verbose"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "skills: 2 items" in result.output

    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.storage.migrations.run_migrations")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_with_errors(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_db_cls: MagicMock,
        _mig: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": ["something failed"]}
        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# verify-only flags
# ---------------------------------------------------------------------------
class TestSyncVerifyOnly:
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_verify_only_dev_mode(
        self, _dev: MagicMock, _install: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(sync, ["--verify-only"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No integrity check" in result.output

    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_verify_only_verbose(
        self, _dev: MagicMock, _install: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(sync, ["--verify-only", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No integrity check" in result.output


# ---------------------------------------------------------------------------
# Production mode integrity check
# ---------------------------------------------------------------------------
class TestSyncProductionMode:
    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.storage.migrations.run_migrations")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    @patch("gobby.sync.integrity.get_dirty_content_types", return_value=set())
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_all_clean(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        _dirty: MagicMock,
        mock_load: MagicMock,
        mock_db_cls: MagicMock,
        _mig: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        integrity_result = MagicMock()
        integrity_result.git_available = True
        integrity_result.all_clean = True
        integrity_result.dirty_files = []
        integrity_result.untracked_files = []
        mock_verify.return_value = integrity_result

        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}
        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "clean" in result.output.lower()

    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_verify_only_clean(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        runner: CliRunner,
    ) -> None:
        integrity_result = MagicMock()
        integrity_result.git_available = True
        integrity_result.all_clean = True
        integrity_result.dirty_files = []
        integrity_result.untracked_files = []
        mock_verify.return_value = integrity_result

        result = runner.invoke(sync, ["--verify-only"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.sync.integrity.get_dirty_content_types", return_value={"skills"})
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_verify_only_dirty(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        _dirty: MagicMock,
        runner: CliRunner,
    ) -> None:
        integrity_result = MagicMock()
        integrity_result.git_available = True
        integrity_result.all_clean = False
        integrity_result.dirty_files = ["file.py"]
        integrity_result.untracked_files = []
        mock_verify.return_value = integrity_result

        result = runner.invoke(sync, ["--verify-only"], catch_exceptions=False)
        assert result.exit_code == 1

    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_no_git(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        runner: CliRunner,
    ) -> None:
        integrity_result = MagicMock()
        integrity_result.git_available = False
        integrity_result.all_clean = True
        mock_verify.return_value = integrity_result

        result = runner.invoke(sync, ["--verify-only", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --type filtering
# ---------------------------------------------------------------------------
class TestSyncTypeFilter:
    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.storage.migrations.run_migrations")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_type_filter(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_db_cls: MagicMock,
        _mig: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 1, "errors": [], "details": {}}
        result = runner.invoke(sync, ["--type", "skills"], catch_exceptions=False)
        assert result.exit_code == 0
        mock_sync.assert_called_once()
        # Verify sync was called with skip_types excluding 'skills'
        call_kwargs = mock_sync.call_args[1]
        assert "skills" not in (call_kwargs.get("skip_types") or set())


# ---------------------------------------------------------------------------
# DB not found
# ---------------------------------------------------------------------------
class TestSyncDbNotFound:
    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_db_not_found(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_path = "/nonexistent/path/db.sqlite"
        mock_load.return_value = mock_config

        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# Force mode
# ---------------------------------------------------------------------------
class TestSyncForce:
    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.storage.migrations.run_migrations")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_force_skips_integrity(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_db_cls: MagicMock,
        _mig: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}
        result = runner.invoke(sync, ["--force", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Force mode" in result.output
