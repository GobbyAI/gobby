"""Tests for cli/sync.py — targeting uncovered lines."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli.installers.shared import _sync_user_templates_to_db as real_user_content_sync
from gobby.cli.sync import sync
from gobby.storage.hub.protocol import HubDatabase
from gobby.sync.integrity import BUNDLED_SYNC_CONTENT_TYPES, IntegrityResult

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def mock_runtime_hub_database() -> Iterator[MagicMock]:
    """Keep sync CLI tests isolated from the user's runtime hub config."""
    with patch("gobby.cli.runtime.require_cli_database") as mock_require_cli_database:
        mock_require_cli_database.return_value = MagicMock()
        yield mock_require_cli_database


@pytest.fixture(autouse=True)
def no_running_daemon() -> Iterator[MagicMock]:
    """Default every test to "no daemon answered" so the checkout gate stays out of the way."""
    with patch("gobby.cli.sync._running_daemon_install_dir", return_value=None) as probe:
        yield probe


@pytest.fixture(autouse=True)
def user_content_sync() -> Iterator[MagicMock]:
    """Stub the user-content import the sync wrapper runs outside dev mode."""
    with patch(
        "gobby.cli.installers.shared._sync_user_templates_to_db", return_value=0
    ) as mock_user_sync:
        yield mock_user_sync


# All lazy imports in sync() need to be patched at the source module:
#   from gobby.utils.dev import is_dev_mode          -> gobby.utils.dev.is_dev_mode
#   from gobby.sync.integrity import ...             -> gobby.sync.integrity.*
#   from gobby.cli.runtime import require_cli_database -> gobby.cli.runtime.require_cli_database
#   from gobby.sync_registry import sync_bundled_content_to_db -> gobby.sync_registry.sync_bundled_content_to_db


# ---------------------------------------------------------------------------
# Dev mode — basic sync
# ---------------------------------------------------------------------------
class TestSyncDevMode:
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    def test_repo_subdirectory_skips_integrity_check(
        self,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_verify: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')
        subdirectory = tmp_path / "src" / "gobby" / "cli"
        subdirectory.mkdir(parents=True)
        monkeypatch.chdir(subdirectory)

        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")
        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}

        result = runner.invoke(sync, ["--verbose"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Dev mode: skipping integrity check" in result.output
        mock_verify.assert_not_called()
        assert mock_sync.call_args.kwargs["skip_types"] is None

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_sync_items(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 5, "errors": [], "details": {}}
        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Synced 5" in result.output

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_no_changes(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}
        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No changes" in result.output

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_verbose(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
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

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_dev_mode_with_errors(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": ["something failed"]}
        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Warning: something failed" in result.output


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
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
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
        mock_config.database_url = str(tmp_path / "test.db")
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
        assert "All bundled content is clean" in result.output

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
        assert result.exit_code == 0
        assert "Blocking tampered content types: skills" in result.output

        fail_result = runner.invoke(
            sync,
            ["--verify-only", "--fail-on-verify"],
            catch_exceptions=False,
        )
        assert fail_result.exit_code == 1

    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_verify_only_missing_manifest_fails_closed(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_verify.return_value = IntegrityResult(
            git_available=False,
            checked=False,
            source="none",
            errors=[
                "Bundled content manifest not found: /fake/install/bundled_content_manifest.json"
            ],
        )

        result = runner.invoke(sync, ["--verify-only", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Integrity verification unavailable" in result.output

        fail_result = runner.invoke(
            sync,
            ["--verify-only", "--fail-on-verify", "--verbose"],
            catch_exceptions=False,
        )
        assert fail_result.exit_code == 1

    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_verify_only_manifest_clean(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_verify.return_value = IntegrityResult(
            git_available=False,
            checked=True,
            source="manifest",
        )

        result = runner.invoke(sync, ["--verify-only", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Git not available; verified packaged manifest" in result.output
        assert "All bundled content is clean" in result.output

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_non_git_tampering_passes_skip_types_to_sync(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_verify.return_value = IntegrityResult(
            dirty_files=["shared/workflows/pipelines/demo.yaml"],
            git_available=False,
            checked=True,
            source="manifest",
        )
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")
        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}

        result = runner.invoke(sync, [], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Blocking tampered content types: pipelines" in result.output
        assert mock_sync.call_args.kwargs["skip_types"] == {"pipelines"}

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_missing_manifest_skips_all_bundled_sync_targets(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_verify.return_value = IntegrityResult(
            git_available=False,
            checked=False,
            source="none",
            errors=[
                "Bundled content manifest not found: /fake/install/bundled_content_manifest.json"
            ],
        )
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")
        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}

        result = runner.invoke(sync, [], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Integrity verification unavailable" in result.output
        assert mock_sync.call_args.kwargs["skip_types"] == BUNDLED_SYNC_CONTENT_TYPES


# ---------------------------------------------------------------------------
# --type filtering
# ---------------------------------------------------------------------------
class TestSyncTypeFilter:
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_type_filter(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
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
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    def test_db_not_found(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_require_database: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_require_database.side_effect = RuntimeError("not found")

        result = runner.invoke(sync, [], catch_exceptions=False)
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# Force mode
# ---------------------------------------------------------------------------
class TestSyncForce:
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_force_skips_integrity(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}
        result = runner.invoke(sync, ["--force", "--verbose"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Force mode" in result.output


# ---------------------------------------------------------------------------
# Checkout gate — shared bundled rows belong to the daemon's checkout
# ---------------------------------------------------------------------------
class TestSyncCheckoutGate:
    DAEMON_DIR = Path("/checkouts/main/src/gobby/install")
    WORKTREE_DIR = Path("/checkouts/wt-feature/src/gobby/install")

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    @patch("gobby.cli.sync.get_install_dir")
    def test_foreign_checkout_is_refused_without_force(
        self,
        mock_install: MagicMock,
        _dev: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        no_running_daemon: MagicMock,
    ) -> None:
        mock_install.return_value = self.WORKTREE_DIR
        no_running_daemon.return_value = self.DAEMON_DIR

        result = runner.invoke(sync, [])

        assert result.exit_code == 1
        assert str(self.DAEMON_DIR) in result.output
        assert str(self.WORKTREE_DIR) in result.output
        assert "--force" in result.output
        mock_sync.assert_not_called()

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    @patch("gobby.cli.sync.get_install_dir")
    def test_force_overwrites_with_a_banner(
        self,
        mock_install: MagicMock,
        _dev: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        no_running_daemon: MagicMock,
    ) -> None:
        mock_install.return_value = self.WORKTREE_DIR
        no_running_daemon.return_value = self.DAEMON_DIR
        mock_sync.return_value = {"total_synced": 3, "errors": [], "details": {}}

        result = runner.invoke(sync, ["--force"])

        assert result.exit_code == 0
        assert "WARNING" in result.output
        assert str(self.DAEMON_DIR) in result.output
        assert str(self.WORKTREE_DIR) in result.output
        assert "Synced 3 bundled items" in result.output
        mock_sync.assert_called_once()

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.utils.dev.is_dev_mode", return_value=True)
    @patch("gobby.cli.sync.get_install_dir")
    def test_daemon_checkout_syncs_without_a_banner(
        self,
        mock_install: MagicMock,
        _dev: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        no_running_daemon: MagicMock,
    ) -> None:
        mock_install.return_value = self.DAEMON_DIR
        no_running_daemon.return_value = self.DAEMON_DIR
        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}

        result = runner.invoke(sync, [])

        assert result.exit_code == 0
        assert "WARNING" not in result.output
        mock_sync.assert_called_once()


class TestRunningDaemonInstallDir:
    @pytest.fixture(autouse=True)
    def no_running_daemon(self) -> Iterator[None]:
        """These tests exercise the real probe, so the module-level stub is switched off."""
        yield

    @staticmethod
    def _response(status_code: int, payload: object) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    @patch("gobby.cli.utils_config.get_daemon_url", return_value="http://localhost:1")
    @patch("gobby.cli.sync.httpx.get")
    def test_reports_the_daemon_install_dir(self, mock_get: MagicMock, _url: MagicMock) -> None:
        from gobby.cli.sync import _running_daemon_install_dir

        mock_get.return_value = self._response(
            200, {"install_dir": "/checkouts/main/src/gobby/install"}
        )

        assert _running_daemon_install_dir() == Path("/checkouts/main/src/gobby/install")
        mock_get.assert_called_once_with("http://localhost:1/api/health", timeout=2.0)

    @pytest.mark.parametrize(
        "status_code, payload",
        [(503, {"install_dir": "/x"}), (200, {}), (200, {"install_dir": ""}), (200, ["nope"])],
    )
    @patch("gobby.cli.utils_config.get_daemon_url", return_value="http://localhost:1")
    @patch("gobby.cli.sync.httpx.get")
    def test_unusable_health_payloads_mean_no_daemon(
        self, mock_get: MagicMock, _url: MagicMock, status_code: int, payload: object
    ) -> None:
        from gobby.cli.sync import _running_daemon_install_dir

        mock_get.return_value = self._response(status_code, payload)

        assert _running_daemon_install_dir() is None

    @patch("gobby.cli.utils_config.get_daemon_url", return_value="http://localhost:1")
    @patch("gobby.cli.sync.httpx.get", side_effect=httpx.ConnectError("refused"))
    def test_unreachable_daemon_means_no_daemon(self, _get: MagicMock, _url: MagicMock) -> None:
        from gobby.cli.sync import _running_daemon_install_dir

        assert _running_daemon_install_dir() is None


# ---------------------------------------------------------------------------
# User-content wiring — gobby sync must go through the installers.shared wrapper
# ---------------------------------------------------------------------------
class TestSyncUserContentWiring:
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.runtime.require_cli_database")
    @patch("gobby.sync.integrity.get_dirty_content_types", return_value=set())
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_sync_imports_user_content(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        _dirty: MagicMock,
        mock_load: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
        user_content_sync: MagicMock,
    ) -> None:
        integrity_result = MagicMock()
        integrity_result.git_available = True
        integrity_result.all_clean = True
        integrity_result.dirty_files = []
        integrity_result.untracked_files = []
        mock_verify.return_value = integrity_result

        mock_config = MagicMock()
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load.return_value = mock_config
        (tmp_path / "test.db").write_text("")

        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}
        user_content_sync.return_value = 3

        result = runner.invoke(sync, [], catch_exceptions=False)

        assert result.exit_code == 0
        user_content_sync.assert_called_once()
        assert "Synced 3 bundled items to database" in result.output

    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.runtime.require_cli_database")
    def test_dev_mode_sync_skips_user_content(
        self,
        mock_db: MagicMock,
        mock_verify: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
        user_content_sync: MagicMock,
    ) -> None:
        mock_db.return_value = MagicMock()
        mock_sync.return_value = {"total_synced": 0, "errors": [], "details": {}}

        result = runner.invoke(sync, [], catch_exceptions=False)

        assert result.exit_code == 0
        mock_sync.assert_called_once()
        assert "No changes to sync" in result.output
        user_content_sync.assert_not_called()


class TestSyncInstanceYamlEndToEnd:
    @patch("gobby.cli.mcp_proxy.check_daemon_running", return_value=False)
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.sync.integrity.get_dirty_content_types", return_value=set())
    @patch("gobby.sync.integrity.verify_bundled_integrity")
    @patch("gobby.cli.sync.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.utils.dev.is_dev_mode", return_value=False)
    def test_prod_sync_upserts_instance_yaml_rows(
        self,
        _dev: MagicMock,
        _install: MagicMock,
        mock_verify: MagicMock,
        _dirty: MagicMock,
        mock_bundled: MagicMock,
        _daemon: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        temp_db: HubDatabase,
        mock_runtime_hub_database: MagicMock,
        user_content_sync: MagicMock,
    ) -> None:
        """gobby sync from a registered project persists .gobby/mcp/servers YAML rows."""
        import gobby.paths
        import gobby.utils.project_context
        from gobby.mcp_proxy.sync_templates import sync_bundled_mcp_templates
        from gobby.storage.mcp import LocalMCPManager
        from gobby.storage.projects import LocalProjectManager

        integrity_result = MagicMock()
        integrity_result.git_available = True
        integrity_result.all_clean = True
        integrity_result.dirty_files = []
        integrity_result.untracked_files = []
        mock_verify.return_value = integrity_result
        mock_bundled.return_value = {"total_synced": 0, "errors": [], "details": {}}
        mock_runtime_hub_database.return_value = temp_db
        user_content_sync.side_effect = real_user_content_sync

        project_id = LocalProjectManager(temp_db).create("sync-e2e").id
        monkeypatch.setattr(
            gobby.utils.project_context,
            "get_project_context",
            lambda cwd=None: {"id": project_id, "project_path": str(cwd)},
        )

        templates = tmp_path / "templates"
        templates.mkdir()
        (templates / "demo.yaml").write_text(
            "name: demo\n"
            "description: Template demo\n"
            "version: 1\n"
            "enabled: true\n"
            "transport: stdio\n"
            "command: npx\n"
            'args: ["-y", "demo-pkg"]\n'
            "params:\n"
            "  - name: token\n"
            "    env: DEMO_TOKEN\n"
            "    required: true\n"
            "    secret: true\n"
            "    default_secret: demo_token\n",
            encoding="utf-8",
        )
        sync_bundled_mcp_templates(temp_db, templates, tag="gobby")

        project_servers = tmp_path / "project-servers"
        project_servers.mkdir()
        (project_servers / "lightspeed.yaml").write_text(
            "template: demo\nvalues:\n  token: $secret:lightspeed_api_token\n",
            encoding="utf-8",
        )
        empty = tmp_path / "empty"
        monkeypatch.setattr(gobby.paths, "get_project_rules_dir", lambda _path: empty)
        monkeypatch.setattr(gobby.paths, "get_global_rules_dir", lambda: empty)
        monkeypatch.setattr(gobby.paths, "get_project_variables_dir", lambda _path: empty)
        monkeypatch.setattr(gobby.paths, "get_global_variables_dir", lambda: empty)
        monkeypatch.setattr(gobby.paths, "get_project_mcp_templates_dir", lambda _path: empty)
        monkeypatch.setattr(gobby.paths, "get_global_mcp_templates_dir", lambda: empty)
        monkeypatch.setattr(
            gobby.paths, "get_project_mcp_servers_dir", lambda _path: project_servers
        )
        monkeypatch.setattr(gobby.paths, "get_global_mcp_servers_dir", lambda: empty)

        result = runner.invoke(sync, [], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Synced 1 bundled items to database" in result.output
        row = LocalMCPManager(temp_db).get_server("lightspeed", project_id=project_id)
        assert row is not None
        assert row.template == "demo"
        assert row.project_id == project_id
        assert row.env is not None
        assert row.env["DEMO_TOKEN"] == "$secret:lightspeed_api_token"
