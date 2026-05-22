"""Tests for gobby.cli.install_setup."""

import json
import tarfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from gobby.cli.install_setup import (
    _ensure_gobby_bin_on_path,
    _get_installed_gcode_version,
    _get_installed_gsqz_version,
    _get_latest_gcode_version,
    _get_latest_gsqz_version,
    _install_gcode,
    _install_gcode_from_cargo_binstall,
    _install_gcode_from_cargo_install,
    _install_gcode_from_github,
    _install_gsqz,
    _install_gsqz_from_cargo_binstall,
    _install_gsqz_from_cargo_install,
    _install_gsqz_from_github,
    _resolve_latest_release_tag,
    _run_npm_install,
    _write_gcode_version_stamp,
    _write_gsqz_version_stamp,
    ensure_daemon_config,
    run_daemon_setup,
)
from gobby.cli.installers.hook_commands import build_hook_command
from gobby.install.distribution import HomebrewHelperStatus
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit


class TestEnsureDaemonConfig:
    @patch("gobby.cli.install_setup.Path.expanduser")
    def test_exists(self, mock_expand, tmp_path):
        target = tmp_path / "bootstrap.yaml"
        target.touch()
        mock_expand.return_value = target

        res = ensure_daemon_config()
        assert not res["created"]

    @patch("gobby.cli.install_setup.Path.expanduser")
    @patch("gobby.cli.install_setup.get_install_dir")
    @patch("gobby.cli.install_setup.copy2")
    def test_copy_shared(self, mock_copy, mock_get_dir, mock_expand, tmp_path):
        target = tmp_path / "bootstrap.yaml"
        mock_expand.return_value = target

        shared_dir = tmp_path / "shared"
        shared_file = shared_dir / "config" / "bootstrap.yaml"
        shared_file.parent.mkdir(parents=True)
        shared_file.touch()

        mock_get_dir.return_value = tmp_path

        # copy2 is mocked so the file won't actually exist for chmod —
        # make the side_effect create it so chmod succeeds
        def fake_copy(src, dst):
            Path(dst).touch()

        mock_copy.side_effect = fake_copy

        res = ensure_daemon_config()
        assert res["created"]
        assert res["source"] == "shared"
        mock_copy.assert_called_once_with(shared_file, target)

    @patch("gobby.cli.install_setup.Path.expanduser")
    @patch("gobby.cli.install_setup.get_install_dir")
    def test_fallback_generate(self, mock_get_dir, mock_expand, tmp_path):
        target = tmp_path / "bootstrap.yaml"
        mock_expand.return_value = target

        mock_get_dir.return_value = tmp_path / "nonexistent"

        res = ensure_daemon_config()
        assert res["created"]
        assert res["source"] == "generated"
        assert target.exists()
        assert "hub_backend: postgres" in target.read_text()
        assert "database_url_ref: keyring:gobby:postgres_database_url" in target.read_text()
        assert "daemon_port: 60887" in target.read_text()


class TestRunDaemonSetup:
    @patch("gobby.storage.hub.runtime.open_runtime_hub_database")
    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup._install_gsqz")
    @patch("gobby.cli.install_setup._install_gcode")
    @patch("gobby.cli.install_setup._install_ghook")
    @patch("gobby.cli.install_setup._install_gloc")
    @patch("gobby.cli.installers.ide_config.configure_vscode_family_terminal_titles")
    def test_run_daemon_setup_success(
        self,
        mock_ide,
        mock_gloc,
        mock_ghook,
        mock_gcode,
        mock_gsqz,
        mock_run,
        mock_mcp,
        mock_sync,
        mock_init,
        tmp_path,
    ):
        mock_db = MagicMock()
        mock_init.return_value = mock_db
        mock_sync.return_value = {"total_synced": 5, "errors": []}
        mock_mcp.return_value = {"success": True, "servers_added": ["gh"], "servers_skipped": []}
        mock_gsqz.return_value = {"installed": True, "version": "1.0", "method": "github"}
        mock_gcode.return_value = {"installed": True, "version": "1.0", "method": "github"}
        mock_ghook.return_value = {"installed": True, "version": "1.0", "method": "github"}
        mock_gloc.return_value = {"installed": True, "version": "1.0", "method": "github"}
        mock_ide.return_value = {"Code": {"added": True}}

        mock_run.return_value = MagicMock(returncode=0)

        run_daemon_setup(tmp_path)

        mock_init.assert_called_once()
        assert mock_init.call_count == 1
        assert mock_init.call_args is not None
        mock_sync.assert_called_once_with(mock_db)
        assert mock_sync.call_count == 1
        assert mock_sync.call_args is not None
        mock_db.close.assert_called_once()
        assert mock_db.close.call_count == 1
        assert mock_db.close.call_args is not None
        mock_mcp.assert_called_once()
        assert mock_mcp.call_count == 1
        assert mock_mcp.call_args is not None
        mock_gsqz.assert_called_once()
        assert mock_gsqz.call_count == 1
        assert mock_gsqz.call_args is not None
        mock_gcode.assert_called_once()
        assert mock_gcode.call_count == 1
        assert mock_gcode.call_args is not None
        mock_ghook.assert_called_once()
        assert mock_ghook.call_count == 1
        assert mock_ghook.call_args is not None
        mock_gloc.assert_called_once()
        assert mock_gloc.call_count == 1
        assert mock_gloc.call_args is not None
        mock_ide.assert_called_once()
        assert mock_ide.call_count == 1
        assert mock_ide.call_args is not None

    @patch("gobby.storage.hub.runtime.open_runtime_hub_database")
    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup._install_gsqz")
    @patch("gobby.cli.install_setup._install_gcode")
    @patch("gobby.cli.install_setup._install_ghook")
    @patch("gobby.cli.install_setup._install_gloc")
    @patch("gobby.cli.installers.ide_config.configure_vscode_family_terminal_titles")
    def test_run_daemon_setup_makes_same_run_hook_generation_use_ghook(
        self,
        mock_ide,
        mock_gloc,
        mock_ghook,
        mock_gcode,
        mock_gsqz,
        mock_run,
        mock_mcp,
        mock_sync,
        mock_init,
        tmp_path,
    ):
        mock_db = MagicMock()
        mock_init.return_value = mock_db
        mock_sync.return_value = {"total_synced": 0, "errors": []}
        mock_mcp.return_value = {"success": True, "servers_added": [], "servers_skipped": []}
        mock_gsqz.return_value = {"skipped": True}
        mock_gcode.return_value = {"skipped": True}
        mock_gloc.return_value = {"skipped": True}
        mock_ide.return_value = {"Code": {"added": False}}
        mock_run.return_value = MagicMock(returncode=0)

        def _fake_install_ghook():
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            ghook = bin_dir / "ghook"
            ghook.write_text("#!/bin/sh\n")
            ghook.chmod(0o755)
            return {"installed": True, "version": "0.1.1", "method": "github"}

        mock_ghook.side_effect = _fake_install_ghook

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.utils.native_bin.Path.home", return_value=tmp_path),
        ):
            run_daemon_setup(tmp_path)
            command = build_hook_command("codex", "SessionStart", tmp_path / ".gobby" / "hooks")

        assert str(tmp_path / ".gobby" / "bin" / "ghook") in command
        assert "--gobby-owned" in command

    @patch("gobby.storage.hub.runtime.open_runtime_hub_database")
    @patch("gobby.cli.installers.shared.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup._install_gsqz")
    @patch("gobby.cli.install_setup._install_gcode")
    @patch("gobby.cli.install_setup._install_ghook")
    @patch("gobby.cli.install_setup._install_gloc")
    @patch("gobby.cli.installers.ide_config.configure_vscode_family_terminal_titles")
    @patch("gobby.cli.install_setup.verify_homebrew_managed_bins")
    def test_homebrew_mode_skips_npm_and_managed_helper_installs(
        self,
        mock_verify: MagicMock,
        mock_ide: MagicMock,
        mock_gloc: MagicMock,
        mock_ghook: MagicMock,
        mock_gcode: MagicMock,
        mock_gsqz: MagicMock,
        mock_run: MagicMock,
        mock_mcp: MagicMock,
        mock_sync: MagicMock,
        mock_init: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_DISTRIBUTION", "homebrew")
        mock_db = MagicMock()
        mock_init.return_value = mock_db
        mock_sync.return_value = {"total_synced": 0, "errors": []}
        mock_mcp.return_value = {"success": True, "servers_added": [], "servers_skipped": []}
        mock_ide.return_value = {"Code": {"added": False}}
        mock_verify.return_value = [
            HomebrewHelperStatus(
                name=name,
                formula=f"gobby-{name}",
                minimum_version="1.0.0",
                path=f"/opt/homebrew/bin/{name}",
                version="1.0.0",
                ok=True,
            )
            for name in ("gcode", "gsqz", "ghook", "gloc")
        ]

        run_daemon_setup(tmp_path)

        mock_verify.assert_called_once_with()
        mock_run.assert_not_called()
        mock_gsqz.assert_not_called()
        mock_gcode.assert_not_called()
        mock_ghook.assert_not_called()
        mock_gloc.assert_not_called()


class TestRunNpmInstall:
    @patch("subprocess.run", side_effect=PermissionError("denied"))
    def test_warns_when_npm_cannot_execute(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_npm_install("Playwright CLI", "@playwright/cli@latest", tmp_path)

        assert mock_run.call_count == 1
        assert "Warning: Failed to run npm for Playwright CLI: denied" in capsys.readouterr().out

    @patch("subprocess.run", side_effect=OSError("exec format error"))
    def test_warns_on_os_error(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_npm_install("ClawHub CLI", "clawhub", tmp_path)

        assert mock_run.call_count == 1
        assert (
            "Warning: Failed to run npm for ClawHub CLI: exec format error"
            in capsys.readouterr().out
        )


class TestGsqzHelpers:
    @patch("gobby.cli.install_setup.urlopen")
    def test_get_latest_gsqz_version(self, mock_url):
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"crate": {"max_version": "1.2.3"}}).encode()
        fake_resp.__enter__.return_value = fake_resp
        mock_url.return_value = fake_resp

        assert _get_latest_gsqz_version() == "1.2.3"

    @patch("gobby.cli.install_setup.urlopen", side_effect=URLError("timeout"))
    def test_get_latest_gsqz_version_fail(self, mock_url):
        assert _get_latest_gsqz_version() is None

    def test_get_installed_gsqz_version(self, tmp_path):
        assert _get_installed_gsqz_version(tmp_path) is None

        (tmp_path / "gsqz").touch()
        assert _get_installed_gsqz_version(tmp_path) == "unknown"

        (tmp_path / ".gsqz-version").write_text("0.5.0\n")
        assert _get_installed_gsqz_version(tmp_path) == "0.5.0"

    def test_write_gsqz_version_stamp(self, tmp_path):
        _write_gsqz_version_stamp(tmp_path, "1.0.0")
        assert (tmp_path / ".gsqz-version").read_text() == "1.0.0\n"

    @patch("gobby.cli.install_setup.urlopen")
    def test_install_gsqz_from_github(self, mock_urlopen, tmp_path):
        # Create a fake tarball in memory
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="gsqz")
            info.size = 5
            tar.addfile(info, BytesIO(b"fake!"))

        buf.seek(0)
        fake_resp = MagicMock()
        fake_resp.read.return_value = buf.read()
        fake_resp.__enter__.return_value = fake_resp
        mock_urlopen.return_value = fake_resp

        with patch(
            "gobby.cli.install_setup._resolve_latest_release_tag",
            return_value="gsqz-v1.2.3",
        ):
            res = _install_gsqz_from_github(tmp_path, "target-triple")
        assert res is True
        assert (tmp_path / "gsqz").exists()
        assert (tmp_path / "gsqz").read_bytes() == b"fake!"

    @patch("gobby.cli.install_setup.urlopen")
    def test_resolve_latest_release_tag_prefers_matching_stable_prefix(self, mock_urlopen):
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps(
            [
                {
                    "tag_name": "sdk-v9.9.9",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-04-17T00:00:00Z",
                },
                {
                    "tag_name": "gsqz-v1.2.0",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-04-15T00:00:00Z",
                },
                {
                    "tag_name": "gsqz-v1.3.0-rc1",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2026-04-16T00:00:00Z",
                },
                {
                    "tag_name": "gsqz-v1.2.3",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-04-14T00:00:00Z",
                },
            ]
        ).encode()
        fake_resp.__enter__.return_value = fake_resp
        mock_urlopen.return_value = fake_resp

        assert _resolve_latest_release_tag(tag_prefix="gsqz-v") == "gsqz-v1.2.3"

    @patch("shutil.which", return_value="/bin/cargo-binstall")
    @patch("subprocess.run")
    def test_install_gsqz_from_cargo_binstall(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        res = _install_gsqz_from_cargo_binstall(tmp_path, "1.0.0")
        assert res is True

    @patch("shutil.which", return_value="/bin/cargo")
    @patch("subprocess.run")
    def test_install_gsqz_from_cargo_install(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        res = _install_gsqz_from_cargo_install(tmp_path, "1.0.0")
        assert res is True

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value="1.0.0")
    @patch("gobby.cli.install_setup._get_installed_gsqz_version", return_value="0.1.0")
    @patch("gobby.cli.install_setup._install_gsqz_from_github", return_value=True)
    @patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={})
    def test_install_gsqz(
        self, mock_path, mock_github, mock_installed, mock_latest, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            # Create binary so chmod succeeds
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gsqz").write_bytes(b"\x00")

            res = _install_gsqz()
            assert res["installed"] is True
            assert res["upgraded"] is True
            assert res["method"] == "github"

            mock_github.assert_called_once()

            # Check version stamp
            assert (bin_dir / ".gsqz-version").exists()


class TestGcodeHelpers:
    def test_get_installed_gcode_version(self, tmp_path):
        assert _get_installed_gcode_version(tmp_path) is None
        (tmp_path / ".gcode-version").write_text("1.0.0")
        assert _get_installed_gcode_version(tmp_path) == "1.0.0"

    def test_write_gcode_version_stamp(self, tmp_path):
        _write_gcode_version_stamp(tmp_path, "2.0.0")
        assert (tmp_path / ".gcode-version").read_text() == "2.0.0\n"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_installed_gcode_version", return_value=None)
    @patch("gobby.cli.install_setup._get_latest_gcode_version", return_value="0.2.3")
    @patch("gobby.cli.install_setup._install_gcode_from_submodule", return_value=True)
    @patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={})
    def test_install_gcode(
        self, mock_path, mock_sub, mock_latest, mock_installed, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            # Create binary so chmod succeeds
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode()
            assert res["installed"] is True
            assert res["method"] == "submodule"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_latest_gcode_version", return_value="0.8.0")
    @patch("gobby.cli.install_setup._get_installed_gcode_version", return_value="0.8.1")
    @patch("gobby.cli.install_setup._install_gcode_from_submodule")
    def test_install_gcode_skips_when_installed_version_satisfies_pin(
        self, mock_submodule, mock_installed, mock_latest, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode()

        assert res == {"installed": False, "skipped": True, "version": "0.8.1"}
        mock_submodule.assert_not_called()

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_latest_gcode_version", return_value="0.8.0")
    @patch("gobby.cli.install_setup._get_installed_gcode_version", return_value="0.7.9")
    @patch("gobby.cli.install_setup._install_gcode_from_submodule", return_value=True)
    @patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={})
    def test_install_gcode_installs_when_installed_version_is_below_pin(
        self, mock_path, mock_submodule, mock_installed, mock_latest, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode()

        assert res["installed"] is True
        assert res["method"] == "submodule"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_latest_gcode_version", return_value="0.8.0")
    @patch("gobby.cli.install_setup._get_installed_gcode_version", return_value="0.8.1")
    @patch("gobby.cli.install_setup._install_gcode_from_submodule", return_value=True)
    @patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={})
    def test_install_gcode_force_bypasses_pin_skip(
        self, mock_path, mock_submodule, mock_installed, mock_latest, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode(force=True)

        assert res["installed"] is True
        assert res["version"] == MANAGED_BIN_VERSION_PINS["gcode"]

    @patch("gobby.cli.install_setup.urlopen")
    def test_get_latest_gcode_version(self, mock_url):
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"crate": {"max_version": "0.2.3"}}).encode()
        fake_resp.__enter__.return_value = fake_resp
        mock_url.return_value = fake_resp

        assert _get_latest_gcode_version() == "0.2.3"

    @patch("gobby.cli.install_setup.urlopen", side_effect=URLError("timeout"))
    def test_get_latest_gcode_version_fail(self, mock_url):
        assert _get_latest_gcode_version() is None

    @patch("gobby.cli.install_setup.urlopen")
    def test_install_gcode_from_github_uses_binary_specific_tag_prefix(
        self, mock_urlopen, tmp_path
    ):
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="gcode")
            info.size = 5
            tar.addfile(info, BytesIO(b"fake!"))

        buf.seek(0)
        fake_resp = MagicMock()
        fake_resp.read.return_value = buf.read()
        fake_resp.__enter__.return_value = fake_resp
        mock_urlopen.return_value = fake_resp

        assert _install_gcode_from_github(tmp_path, "aarch64-apple-darwin", "0.2.3") is True
        url_called = mock_urlopen.call_args[0][0]
        if hasattr(url_called, "full_url"):
            url_called = url_called.full_url
        assert "gcode-v0.2.3" in url_called

    @patch("shutil.which", return_value="/usr/bin/cargo-binstall")
    @patch("subprocess.run")
    def test_install_gcode_from_cargo_binstall(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        assert _install_gcode_from_cargo_binstall(tmp_path) is True
        cmd = mock_run.call_args[0][0]
        assert "gobby-code" in cmd

    @patch("shutil.which", return_value="/usr/bin/cargo-binstall")
    @patch("subprocess.run")
    def test_install_gcode_from_cargo_binstall_with_version(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        _install_gcode_from_cargo_binstall(tmp_path, "0.2.3")
        cmd = mock_run.call_args[0][0]
        assert "gobby-code@0.2.3" in cmd

    @patch("shutil.which", return_value="/usr/bin/cargo")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup.click")
    def test_install_gcode_from_cargo_install(self, mock_click, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        assert _install_gcode_from_cargo_install(tmp_path) is True
        cmd = mock_run.call_args[0][0]
        assert "gobby-code" in cmd

    @patch("shutil.which", return_value="/usr/bin/cargo")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup.click")
    def test_install_gcode_from_cargo_install_with_version(
        self, mock_click, mock_run, mock_which, tmp_path
    ):
        mock_run.return_value = MagicMock(returncode=0)
        _install_gcode_from_cargo_install(tmp_path, "0.2.3")
        cmd = mock_run.call_args[0][0]
        assert "--version" in cmd
        assert "0.2.3" in cmd


class TestEnsurePath:
    @patch("gobby.cli.install_setup.sys.platform", "linux")
    @patch("gobby.cli.install_setup.os.environ")
    @patch("gobby.cli.install_setup.Path.home")
    def test_ensure_gobby_bin_on_path(self, mock_home, mock_environ, tmp_path):
        mock_environ.get.side_effect = lambda k, default="": "/bin/bash" if k == "SHELL" else ""
        mock_home.return_value = tmp_path

        res = _ensure_gobby_bin_on_path()
        assert res["added"] is True
        assert res["shell"] == "bash"

        bashrc = tmp_path / ".bashrc"
        assert bashrc.exists()
        assert "export PATH=" in bashrc.read_text()
        assert "# gobby" in bashrc.read_text()

        # Second run should skip
        res2 = _ensure_gobby_bin_on_path()
        assert res2["added"] is False
