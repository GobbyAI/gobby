"""Tests for gobby.cli.install_setup."""

import hashlib
import json
import os
import re
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.parse import urlparse

import click
import pytest
import yaml

from gobby.agents.srt_runtime import SrtRuntimeError
from gobby.cli.install_setup import (
    _download_release_binary,
    _ensure_gobby_bin_on_path,
    _extract_binary_from_release_archive,
    _fetch_release_checksum,
    _get_installed_gcode_version,
    _get_installed_gwiki_version,
    _get_latest_gcode_version,
    _get_latest_gwiki_version,
    _install_gcode,
    _install_gcode_from_cargo_binstall,
    _install_gcode_from_cargo_install,
    _install_gcode_from_github,
    _install_gcode_from_submodule,
    _install_gwiki,
    _install_gwiki_from_cargo_binstall,
    _install_gwiki_from_cargo_git,
    _install_gwiki_from_cargo_install,
    _install_gwiki_from_github,
    _resolve_latest_release_tag,
    _run_npm_install,
    _verify_release_artifact,
    _write_gcode_version_stamp,
    _write_gwiki_version_stamp,
    ensure_daemon_config,
    run_daemon_setup,
)
from gobby.cli.installers.hook_commands import build_hook_command
from gobby.install.checksums import parse_sha256_digest
from gobby.install.distribution import HomebrewHelperStatus
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit
GCODE_PIN: str = MANAGED_BIN_VERSION_PINS["gcode"]
GWIKI_PIN: str = MANAGED_BIN_VERSION_PINS["gwiki"]
_BUNDLED_INSTALL_ROOT = Path(__file__).parents[2] / "src" / "gobby" / "install"


def _assert_fresh_local_dsn(bootstrap_text: str) -> None:
    """A newly minted bootstrap carries a random URL-safe password on the default port."""
    parsed = urlparse(yaml.safe_load(bootstrap_text)["database_url"])
    assert (parsed.scheme, parsed.username, parsed.port, parsed.path) == (
        "postgresql",
        "gobby",
        60891,
        "/gobby",
    )
    assert parsed.password is not None
    assert len(parsed.password) >= 32
    assert re.fullmatch(r"[A-Za-z0-9_-]+", parsed.password)


@pytest.fixture(autouse=True)
def _verified_gdaemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.cli.install_setup.ensure_gdaemon",
        lambda: {"installed": False, "version": "0.1.0", "method": "existing"},
    )


class TestEnsureDaemonConfig:
    @patch("gobby.cli.install_setup.Path.expanduser")
    def test_exists(self, mock_expand, tmp_path):
        target = tmp_path / "bootstrap.yaml"
        target.touch()
        mock_expand.return_value = target

        files_home = tmp_path / "files"
        files_home.mkdir()
        res = ensure_daemon_config(files_home=files_home)
        assert not res["created"]

    @patch("gobby.cli.install_setup.Path.expanduser")
    @patch("gobby.cli.install_setup.get_install_dir")
    def test_copy_shared(self, mock_get_dir, mock_expand, tmp_path):
        target = tmp_path / "bootstrap.yaml"
        mock_expand.return_value = target

        shared_file = tmp_path / "shared" / "config" / "bootstrap.yaml"
        shared_file.parent.mkdir(parents=True)
        shared_file.write_text("datastore_mode: local\ndaemon_port: 60887\nbind_host: localhost\n")

        mock_get_dir.return_value = tmp_path

        files_home = tmp_path / "files"
        files_home.mkdir()
        res = ensure_daemon_config(files_home=files_home)
        assert res["created"]
        assert res["source"] == "shared"
        assert yaml.safe_load(target.read_text())["files_home"] == str(files_home.resolve())
        _assert_fresh_local_dsn(target.read_text())
        assert (tmp_path / ".bootstrap.yaml.lock").exists()

    @patch("gobby.cli.install_setup.Path.expanduser")
    def test_existing_bootstrap_is_never_rewritten(self, mock_expand, tmp_path):
        target = tmp_path / "bootstrap.yaml"
        original = (
            "datastore_mode: local\ndatabase_url: postgresql://gobby:keep@localhost:60891/gobby\n"
        )
        target.write_text(original)
        mock_expand.return_value = target

        files_home = tmp_path / "files"
        files_home.mkdir()
        res = ensure_daemon_config(files_home=files_home)

        assert not res["created"]
        assert target.read_text() == original

    @patch("gobby.cli.install_setup.Path.expanduser")
    @patch("gobby.cli.install_setup.get_install_dir")
    def test_fallback_generate(self, mock_get_dir, mock_expand, tmp_path):
        target = tmp_path / "bootstrap.yaml"
        mock_expand.return_value = target

        mock_get_dir.return_value = tmp_path / "nonexistent"

        files_home = tmp_path / "files"
        files_home.mkdir()
        res = ensure_daemon_config(files_home=files_home)
        assert res["created"]
        assert res["source"] == "generated"
        assert target.exists()
        _assert_fresh_local_dsn(target.read_text())
        assert "daemon_port: 60887" in target.read_text()
        assert yaml.safe_load(target.read_text())["postgres_pool"] == {
            "acquire_timeout_seconds": 5.0,
            "open_timeout_seconds": 30.0,
            "max_lifetime_seconds": 300.0,
        }

    def test_bundled_bootstrap_exposes_postgres_pool_defaults(self) -> None:
        template = _BUNDLED_INSTALL_ROOT / "shared" / "config" / "bootstrap.yaml"

        content = yaml.safe_load(template.read_text())

        assert content["postgres_pool"] == {
            "acquire_timeout_seconds": 5.0,
            "open_timeout_seconds": 30.0,
        }
        assert "database_url" not in content

    def test_bundled_content_carries_no_dev_password(self) -> None:
        offenders = [
            path
            for path in _BUNDLED_INSTALL_ROOT.rglob("*")
            if "__pycache__" not in path.parts
            and path.is_file()
            and b"gobby_dev" in path.read_bytes()
        ]

        assert offenders == []


class TestRunDaemonSetup:
    @patch("gobby.cli.install_setup_impeccable.reconcile_impeccable_installation")
    @patch("gobby.cli.install_setup_impeccable.install_impeccable_cli")
    @patch("gobby.cli.install_setup_srt.install_srt_runtime")
    @patch("gobby.storage.hub.runtime.runtime_hub_database")
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup._install_gcode")
    @patch("gobby.cli.install_setup._install_ghook")
    @patch("gobby.cli.installers.ide_config.configure_vscode_family_terminal_integration")
    def test_run_daemon_setup_success(
        self,
        mock_ide,
        mock_ghook,
        mock_gcode,
        mock_run,
        mock_mcp,
        mock_sync,
        mock_init,
        mock_srt,
        mock_impeccable,
        mock_reconcile,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ):
        mock_db = MagicMock()
        mock_context = MagicMock()
        mock_context.__enter__.return_value = mock_db
        mock_init.return_value = mock_context
        mock_srt.return_value = MagicMock(
            installed=False,
            version="0.0.66",
            path=tmp_path / ".gobby" / "runtime" / "srt" / "0.0.66",
        )
        mock_impeccable.return_value = MagicMock(
            installed=False,
            version="3.5.0",
            path=tmp_path / ".gobby" / "tools" / "impeccable" / "3.5.0",
        )
        mock_sync.return_value = {"total_synced": 5, "errors": []}
        mock_mcp.return_value = {"success": True, "servers_added": ["gh"], "servers_skipped": []}
        mock_gcode.return_value = {"installed": True, "version": "1.0", "method": "github"}
        mock_ghook.return_value = {"installed": True, "version": "1.0", "method": "github"}
        mock_ide.return_value = {
            "Code": {"added": True},
            "Cursor": {"warning": "tmux executable was not found on PATH"},
            "Antigravity": {"error": "Failed to parse settings.json"},
        }

        mock_run.return_value = MagicMock(returncode=0)

        with patch("gobby.cli.installers.tmux_config.configure_tmux_clipboard") as mock_tmux:
            mock_tmux.return_value = {"success": True, "updated": True, "config_path": "/tmp"}
            run_daemon_setup(tmp_path, configure_ide_settings=True)

        mock_init.assert_called_once()
        assert mock_init.call_count == 1
        assert mock_init.call_args is not None
        mock_sync.assert_called_once_with(mock_db)
        assert mock_sync.call_count == 1
        assert mock_sync.call_args is not None
        mock_context.__exit__.assert_called_once()
        mock_db.close.assert_not_called()
        mock_mcp.assert_called_once()
        assert mock_mcp.call_count == 1
        assert mock_mcp.call_args is not None
        mock_srt.assert_called_once_with()
        mock_impeccable.assert_called_once_with()
        mock_reconcile.assert_called_once_with(tmp_path)
        mock_gcode.assert_called_once()
        assert mock_gcode.call_count == 1
        assert mock_gcode.call_args is not None
        mock_ghook.assert_called_once()
        assert mock_ghook.call_count == 1
        assert mock_ghook.call_args is not None
        mock_ide.assert_called_once()
        assert mock_ide.call_count == 1
        assert mock_ide.call_args is not None
        mock_tmux.assert_called_once_with()
        output = capsys.readouterr().out
        assert "Configured VS Code-family terminal integration: Code" in output
        assert (
            "Warning: Skipped Cursor terminal integration: tmux executable was not found on PATH"
        ) in output
        assert (
            "Warning: Failed to configure Antigravity terminal integration: "
            "Failed to parse settings.json"
        ) in output

    @patch("gobby.cli.install_setup_impeccable.install_impeccable_cli")
    @patch("gobby.cli.install_setup_srt.install_srt_runtime")
    @patch("gobby.storage.hub.runtime.runtime_hub_database")
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup._install_gcode")
    @patch("gobby.cli.install_setup._install_ghook")
    @patch("gobby.cli.installers.ide_config.configure_vscode_family_terminal_integration")
    def test_run_daemon_setup_stops_when_srt_install_fails(
        self,
        mock_ide: MagicMock,
        mock_ghook: MagicMock,
        mock_gcode: MagicMock,
        mock_run: MagicMock,
        mock_mcp: MagicMock,
        mock_sync: MagicMock,
        mock_init: MagicMock,
        mock_srt: MagicMock,
        mock_impeccable: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_srt.side_effect = SrtRuntimeError("Node.js 20.11 or newer is required")
        mock_impeccable.return_value = MagicMock(
            installed=False,
            version="3.5.0",
            path=tmp_path / ".gobby" / "tools" / "impeccable" / "3.5.0",
        )
        mock_init.return_value = MagicMock()
        mock_sync.return_value = {"total_synced": 0, "errors": []}
        mock_mcp.return_value = {"success": True, "servers_added": [], "servers_skipped": []}
        mock_gcode.return_value = {"skipped": True}
        mock_ghook.return_value = {"skipped": True}
        mock_ide.return_value = {"Code": {"added": False}}
        mock_run.return_value = MagicMock(returncode=0)

        with patch("gobby.cli.installers.tmux_config.configure_tmux_clipboard") as mock_tmux:
            mock_tmux.return_value = {"success": True, "updated": False}
            with pytest.raises(click.ClickException) as exc_info:
                run_daemon_setup(tmp_path, configure_ide_settings=True)

        assert exc_info.value.message == (
            "Failed to install managed Sandbox Runtime: Node.js 20.11 or newer is required"
        )
        assert exc_info.value.exit_code == 1
        mock_impeccable.assert_not_called()
        mock_gcode.assert_not_called()
        mock_ghook.assert_not_called()
        mock_ide.assert_not_called()
        mock_tmux.assert_not_called()

    @patch("gobby.cli.install_setup_impeccable.install_impeccable_cli")
    @patch("gobby.cli.install_setup_srt.install_srt_runtime")
    @patch("gobby.storage.hub.runtime.runtime_hub_database")
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("gobby.cli.install_setup._run_npm_install")
    @patch("gobby.cli.install_setup._run_managed_native_binary_installs")
    def test_run_daemon_setup_fails_when_impeccable_install_fails(
        self,
        mock_native: MagicMock,
        mock_npm: MagicMock,
        mock_mcp: MagicMock,
        mock_sync: MagicMock,
        mock_init: MagicMock,
        mock_srt: MagicMock,
        mock_impeccable: MagicMock,
        tmp_path: Path,
    ) -> None:
        from gobby.cli.install_setup_impeccable import ImpeccableInstallError

        mock_init.return_value = MagicMock()
        mock_sync.return_value = {"total_synced": 0, "errors": []}
        mock_mcp.return_value = {"success": True, "servers_added": [], "servers_skipped": []}
        mock_srt.return_value = MagicMock(
            installed=False,
            version="0.0.66",
            path=tmp_path / ".gobby" / "runtime" / "srt" / "0.0.66",
        )
        mock_impeccable.side_effect = ImpeccableInstallError("npm failed")

        with (
            patch("gobby.cli.installers.tmux_config.configure_tmux_clipboard"),
            pytest.raises(click.ClickException) as exc_info,
        ):
            run_daemon_setup(tmp_path, configure_ide_settings=False)

        assert str(exc_info.value) == "Failed to provision managed Impeccable CLI: npm failed"
        mock_mcp.assert_called_once_with()
        mock_srt.assert_called_once_with()
        mock_impeccable.assert_called_once_with()
        mock_npm.assert_not_called()
        mock_native.assert_not_called()

    @patch("gobby.cli.install_setup_impeccable.install_impeccable_cli")
    @patch("gobby.cli.install_setup_srt.install_srt_runtime")
    @patch("gobby.storage.hub.runtime.runtime_hub_database")
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup._install_gcode")
    @patch("gobby.cli.install_setup._install_ghook")
    @patch("gobby.cli.installers.ide_config.configure_vscode_family_terminal_integration")
    def test_run_daemon_setup_makes_same_run_hook_generation_use_ghook(
        self,
        mock_ide,
        mock_ghook,
        mock_gcode,
        mock_run,
        mock_mcp,
        mock_sync,
        mock_init,
        mock_srt,
        mock_impeccable,
        tmp_path,
    ):
        mock_db = MagicMock()
        mock_init.return_value = mock_db
        mock_srt.return_value = MagicMock(
            installed=False,
            version="0.0.66",
            path=tmp_path / ".gobby" / "runtime" / "srt" / "0.0.66",
        )
        mock_impeccable.return_value = MagicMock(
            installed=False,
            version="3.5.0",
            path=tmp_path / ".gobby" / "tools" / "impeccable" / "3.5.0",
        )
        mock_sync.return_value = {"total_synced": 0, "errors": []}
        mock_mcp.return_value = {"success": True, "servers_added": [], "servers_skipped": []}
        mock_gcode.return_value = {"skipped": True}
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
            patch("gobby.cli.installers.tmux_config.configure_tmux_clipboard") as mock_tmux,
        ):
            mock_tmux.return_value = {"success": True, "updated": False}
            run_daemon_setup(tmp_path, configure_ide_settings=True)
            command = build_hook_command("codex", "SessionStart", tmp_path / ".gobby" / "hooks")

        assert str(tmp_path / ".gobby" / "bin" / "ghook") in command
        assert "--gobby-owned" in command
        mock_srt.assert_called_once_with()
        mock_impeccable.assert_called_once_with()

    @patch("gobby.cli.install_setup_impeccable.install_impeccable_cli")
    @patch("gobby.cli.install_setup_srt.install_srt_runtime")
    @patch("gobby.storage.hub.runtime.runtime_hub_database")
    @patch("gobby.sync_registry.sync_bundled_content_to_db")
    @patch("gobby.cli.installers.install_default_mcp_servers")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup._install_gcode")
    @patch("gobby.cli.install_setup._install_ghook")
    @patch("gobby.cli.installers.ide_config.configure_vscode_family_terminal_integration")
    @patch("gobby.cli.install_setup.verify_homebrew_managed_bins")
    def test_homebrew_mode_installs_required_runtimes_but_skips_managed_helper_installs(
        self,
        mock_verify: MagicMock,
        mock_ide: MagicMock,
        mock_ghook: MagicMock,
        mock_gcode: MagicMock,
        mock_run: MagicMock,
        mock_mcp: MagicMock,
        mock_sync: MagicMock,
        mock_init: MagicMock,
        mock_srt: MagicMock,
        mock_impeccable: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_DISTRIBUTION", "homebrew")
        mock_db = MagicMock()
        mock_init.return_value = mock_db
        mock_srt.return_value = MagicMock(
            installed=True,
            version="0.0.66",
            path=tmp_path / ".gobby" / "runtime" / "srt" / "0.0.66",
        )
        mock_impeccable.return_value = MagicMock(
            installed=True,
            version="3.5.0",
            path=tmp_path / ".gobby" / "tools" / "impeccable" / "3.5.0",
        )
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
            for name in ("gcode", "ghook", "gwiki")
        ]

        with patch("gobby.cli.installers.tmux_config.configure_tmux_clipboard") as mock_tmux:
            mock_tmux.return_value = {"success": True, "updated": False}
            run_daemon_setup(tmp_path, configure_ide_settings=False)

        assert mock_sync.return_value == {"total_synced": 0, "errors": []}
        assert mock_mcp.return_value["success"] is True
        mock_srt.assert_called_once_with()
        mock_impeccable.assert_called_once_with()
        mock_verify.assert_called_once_with()
        mock_run.assert_not_called()
        mock_gcode.assert_not_called()
        mock_ghook.assert_not_called()
        mock_ide.assert_not_called()
        mock_tmux.assert_called_once_with()


class TestRunNpmInstall:
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup.shutil.which", return_value=None)
    def test_skips_when_npm_is_missing(
        self,
        _mock_which: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_npm_install("Playwright CLI", "@playwright/cli@latest", tmp_path)

        mock_run.assert_not_called()
        assert "npm not found" in capsys.readouterr().out

    @patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/npm")
    @patch("subprocess.run", side_effect=PermissionError("denied"))
    def test_warns_when_npm_cannot_execute(
        self,
        mock_run: MagicMock,
        _mock_which: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_npm_install("Playwright CLI", "@playwright/cli@latest", tmp_path)

        assert mock_run.call_count == 1
        assert "Warning: Failed to run npm for Playwright CLI: denied" in capsys.readouterr().out

    @patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/npm")
    @patch("subprocess.run", side_effect=OSError("exec format error"))
    def test_warns_on_os_error(
        self,
        mock_run: MagicMock,
        _mock_which: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_npm_install("ClawHub CLI", "clawhub", tmp_path)

        assert mock_run.call_count == 1
        assert (
            "Warning: Failed to run npm for ClawHub CLI: exec format error"
            in capsys.readouterr().out
        )


class TestReleaseTagHelpers:
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
                    "tag_name": "gcode-v1.2.0",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-04-15T00:00:00Z",
                },
                {
                    "tag_name": "gcode-v1.3.0-rc1",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2026-04-16T00:00:00Z",
                },
                {
                    "tag_name": "gcode-v1.2.3",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-04-14T00:00:00Z",
                },
            ]
        ).encode()
        fake_resp.__enter__.return_value = fake_resp
        mock_urlopen.return_value = fake_resp

        assert _resolve_latest_release_tag(tag_prefix="gcode-v") == "gcode-v1.2.3"

    @patch("gobby.cli.install_setup.urlopen")
    def test_resolve_latest_release_tag_fails_closed_without_legacy_fallback(self, mock_urlopen):
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps(
            [
                {
                    "tag_name": "ghook-v0.7.1",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-06-30T00:00:00Z",
                }
            ]
        ).encode()
        fake_resp.__enter__.return_value = fake_resp
        mock_urlopen.return_value = fake_resp

        with pytest.raises(ValueError, match="gcode-v"):
            _resolve_latest_release_tag(tag_prefix="gcode-v")

        requested_urls = [call.args[0].full_url for call in mock_urlopen.call_args_list]
        assert requested_urls == [
            "https://api.github.com/repos/GobbyAI/gobby/releases?per_page=100",
        ]

    @patch("gobby.cli.install_setup.urlopen")
    def test_download_release_binary_uses_only_canonical_repo(self, mock_urlopen, tmp_path):
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="gcode")
            info.size = 5
            tar.addfile(info, BytesIO(b"fake!"))

        fake_resp = MagicMock()
        fake_resp.read.return_value = buf.getvalue()
        fake_resp.__enter__.return_value = fake_resp
        mock_urlopen.return_value = fake_resp

        with patch("gobby.cli.install_setup._verify_release_artifact", return_value=True):
            assert _download_release_binary(
                tmp_path,
                binary_name="gcode",
                artifact_name="gcode",
                target="aarch64-apple-darwin",
                version="1.5.0",
                tag_prefix="gcode-v",
                label="gcode",
            )

        requested_urls = [call.args[0].full_url for call in mock_urlopen.call_args_list]
        assert requested_urls == [
            "https://github.com/GobbyAI/gobby/releases/download/"
            "gcode-v1.5.0/gcode-aarch64-apple-darwin.tar.gz",
        ]
        assert (tmp_path / "gcode").read_bytes() == b"fake!"

    @patch("gobby.cli.install_setup.urlopen")
    def test_download_release_binary_fails_closed_when_asset_is_missing(
        self, mock_urlopen, tmp_path
    ):
        mock_urlopen.side_effect = URLError("not found")

        assert not _download_release_binary(
            tmp_path,
            binary_name="gcode",
            artifact_name="gcode",
            target="aarch64-apple-darwin",
            version="1.5.0",
            tag_prefix="gcode-v",
            label="gcode",
        )

        requested_urls = [call.args[0].full_url for call in mock_urlopen.call_args_list]
        assert requested_urls == [
            "https://github.com/GobbyAI/gobby/releases/download/"
            "gcode-v1.5.0/gcode-aarch64-apple-darwin.tar.gz",
        ]
        assert not (tmp_path / "gcode").exists()


class TestGcodeHelpers:
    def test_get_installed_gcode_version(self, tmp_path):
        assert _get_installed_gcode_version(tmp_path) is None
        (tmp_path / ".gcode-version").write_text("1.0.0")
        assert _get_installed_gcode_version(tmp_path) == "1.0.0"

    @patch("gobby.cli.install_setup.subprocess.run")
    def test_get_installed_gcode_version_prefers_binary_over_stamp(self, mock_run, tmp_path):
        (tmp_path / "gcode").write_bytes(b"fake")
        (tmp_path / ".gcode-version").write_text("0.9.8")
        mock_run.return_value = MagicMock(returncode=0, stdout="gcode 0.9.9\n", stderr="")

        assert _get_installed_gcode_version(tmp_path) == "0.9.9"

        mock_run.assert_called_once_with(
            [str(tmp_path / "gcode"), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_write_gcode_version_stamp(self, tmp_path):
        _write_gcode_version_stamp(tmp_path, "2.0.0")
        assert (tmp_path / ".gcode-version").read_text() == "2.0.0\n"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_installed_gcode_version", return_value=None)
    @patch("gobby.cli.install_setup._get_latest_gcode_version", return_value="0.2.3")
    @patch("gobby.cli.install_setup._install_gcode_from_submodule", return_value=True)
    @patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={})
    def test_binary_installers_pass_resolved_bin_dir(
        self, mock_path, mock_sub, mock_latest, mock_installed, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            # Create binary so chmod succeeds
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode()
            assert res["installed"] is True
            assert res["method"] == "workspace"
            mock_path.assert_called_once_with(bin_dir)

    def test_install_gcode_from_submodule_stages_under_lock(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "crates" / "gcode").mkdir(parents=True)
        (workspace / "src" / "gobby" / "cli").mkdir(parents=True)
        (workspace / "Cargo.toml").touch()
        (workspace / "crates" / "gcode" / "Cargo.toml").touch()
        source = workspace / "target" / "release" / "gcode"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"new-binary")
        destination_dir = tmp_path / "bin"
        lock = MagicMock()

        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo"),
            patch("gobby.cli.install_setup.subprocess.run", return_value=MagicMock(returncode=0)),
            patch(
                "gobby.cli.install_setup_gcode.__file__",
                str(workspace / "src" / "gobby" / "cli" / "install_setup_gcode.py"),
            ),
            patch(
                "gobby.cli.install_setup_gcode.try_acquire_native_bin_lock",
                return_value=lock,
            ) as acquire_lock,
            patch("gobby.install.bin_freshness_promotion.os.replace", wraps=os.replace) as replace,
        ):
            result = _install_gcode_from_submodule(destination_dir)

        assert result is True
        assert (destination_dir / "gcode").read_bytes() == b"new-binary"
        acquire_lock.assert_called_once_with("gcode", bin_dir=destination_dir)
        lock.__enter__.assert_called_once_with()
        assert replace.call_args.args[1] == destination_dir / "gcode"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_latest_gcode_version", return_value=GCODE_PIN)
    @patch("gobby.cli.install_setup._get_installed_gcode_version", return_value=GCODE_PIN)
    @patch("gobby.cli.install_setup._install_gcode_from_submodule")
    def test_install_gcode_skips_when_installed_version_satisfies_pin(
        self, mock_submodule, mock_installed, mock_latest, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode()

        assert res == {"installed": False, "skipped": True, "version": GCODE_PIN}
        assert (bin_dir / "gcode").exists()
        mock_latest.assert_not_called()
        mock_submodule.assert_not_called()

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup.subprocess.run")
    @patch("gobby.cli.install_setup._install_gcode_from_submodule")
    def test_install_gcode_refreshes_stale_stamp_when_binary_satisfies_pin(
        self, mock_submodule, mock_run, mock_machine, tmp_path
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"fake")
            (bin_dir / ".gcode-version").write_text("0.9.8")
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"gcode {GCODE_PIN}\n",
                stderr="",
            )

            res = _install_gcode()

        assert res == {"installed": False, "skipped": True, "version": GCODE_PIN}
        assert (bin_dir / ".gcode-version").read_text() == f"{GCODE_PIN}\n"
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
        assert res["method"] == "workspace"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_latest_gcode_version", return_value=GCODE_PIN)
    @patch("gobby.cli.install_setup._get_installed_gcode_version", return_value=GCODE_PIN)
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

    def test_install_gcode_uses_managed_pin_for_download_and_cargo_paths(self, tmp_path):
        pin = MANAGED_BIN_VERSION_PINS["gcode"]

        with (
            patch("gobby.cli.install_setup.sys.platform", "darwin"),
            patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_installed_gcode_version", return_value="0.1.0"),
            patch("gobby.cli.install_setup._install_gcode_from_submodule", return_value=False),
            patch(
                "gobby.cli.install_setup._install_gcode_from_github", return_value=False
            ) as github,
            patch(
                "gobby.cli.install_setup._install_gcode_from_cargo_binstall",
                return_value=False,
            ) as binstall,
            patch(
                "gobby.cli.install_setup._install_gcode_from_cargo_install",
                return_value=True,
            ) as cargo_install,
            patch(
                "gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}
            ) as ensure_path,
        ):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode()

        assert res["installed"] is True
        assert res["version"] == pin
        github.assert_called_once_with(bin_dir, "aarch64-apple-darwin", pin)
        binstall.assert_called_once_with(bin_dir, pin)
        cargo_install.assert_called_once_with(bin_dir, pin)
        ensure_path.assert_called_once_with(bin_dir)

    def test_install_gcode_uses_newer_installed_version_as_install_target(self, tmp_path):
        newer_version = "9.9.9"

        with (
            patch("gobby.cli.install_setup.sys.platform", "darwin"),
            patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch(
                "gobby.cli.install_setup._get_installed_gcode_version", return_value=newer_version
            ),
            patch("gobby.cli.install_setup._install_gcode_from_submodule", return_value=False),
            patch(
                "gobby.cli.install_setup._install_gcode_from_github", return_value=False
            ) as github,
            patch(
                "gobby.cli.install_setup._install_gcode_from_cargo_binstall",
                return_value=False,
            ) as binstall,
            patch(
                "gobby.cli.install_setup._install_gcode_from_cargo_install",
                return_value=True,
            ) as cargo_install,
            patch(
                "gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}
            ) as ensure_path,
        ):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcode").write_bytes(b"\x00")

            res = _install_gcode(force=True)

        assert res["installed"] is True
        assert res["version"] == newer_version
        github.assert_called_once_with(bin_dir, "aarch64-apple-darwin", newer_version)
        binstall.assert_called_once_with(bin_dir, newer_version)
        cargo_install.assert_called_once_with(bin_dir, newer_version)
        ensure_path.assert_called_once_with(bin_dir)

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

        with patch("gobby.cli.install_setup._verify_release_artifact", return_value=True):
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


class TestGwikiHelpers:
    def test_get_installed_gwiki_version(self, tmp_path):
        assert _get_installed_gwiki_version(tmp_path) is None
        (tmp_path / ".gwiki-version").write_text("0.1.0")
        assert _get_installed_gwiki_version(tmp_path) == "0.1.0"

    @patch("gobby.cli.install_setup.subprocess.run")
    def test_get_installed_gwiki_version_prefers_binary_over_stamp(self, mock_run, tmp_path):
        (tmp_path / "gwiki").write_bytes(b"fake")
        (tmp_path / ".gwiki-version").write_text("0.0.1")
        mock_run.return_value = MagicMock(returncode=0, stdout="gwiki 0.1.0\n", stderr="")

        assert _get_installed_gwiki_version(tmp_path) == "0.1.0"

        mock_run.assert_called_once_with(
            [str(tmp_path / "gwiki"), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_write_gwiki_version_stamp(self, tmp_path):
        _write_gwiki_version_stamp(tmp_path, GWIKI_PIN)
        assert (tmp_path / ".gwiki-version").read_text() == f"{GWIKI_PIN}\n"

    @patch("gobby.cli.install_setup.sys.platform", "darwin")
    @patch("gobby.cli.install_setup.platform.machine", return_value="arm64")
    @patch("gobby.cli.install_setup._get_latest_gwiki_version")
    @patch("gobby.cli.install_setup._get_installed_gwiki_version", return_value=GWIKI_PIN)
    @patch("gobby.cli.install_setup._install_gwiki_from_submodule")
    @patch("gobby.cli.install_setup._install_gwiki_from_github")
    @patch("gobby.cli.install_setup._install_gwiki_from_cargo_binstall")
    @patch("gobby.cli.install_setup._install_gwiki_from_cargo_install")
    @patch("gobby.cli.install_setup._install_gwiki_from_cargo_git")
    def test_install_gwiki_skips_when_installed_version_satisfies_pin(
        self,
        mock_cargo_git,
        mock_cargo_install,
        mock_binstall,
        mock_github,
        mock_submodule,
        mock_installed,
        mock_latest,
        mock_machine,
        tmp_path,
    ):
        with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gwiki").write_bytes(b"\x00")

            res = _install_gwiki()

        assert res == {"installed": False, "skipped": True, "version": GWIKI_PIN}
        assert (bin_dir / ".gwiki-version").read_text() == f"{GWIKI_PIN}\n"
        mock_latest.assert_not_called()
        mock_submodule.assert_not_called()
        mock_github.assert_not_called()
        mock_binstall.assert_not_called()
        mock_cargo_install.assert_not_called()
        mock_cargo_git.assert_not_called()

    def test_install_gwiki_uses_managed_pin_for_download_and_cargo_paths(self, tmp_path):
        with (
            patch("gobby.cli.install_setup.sys.platform", "darwin"),
            patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_installed_gwiki_version", return_value="0.0.1"),
            patch("gobby.cli.install_setup._install_gwiki_from_submodule", return_value=False),
            patch(
                "gobby.cli.install_setup._install_gwiki_from_github", return_value=False
            ) as github,
            patch(
                "gobby.cli.install_setup._install_gwiki_from_cargo_binstall",
                return_value=False,
            ) as binstall,
            patch(
                "gobby.cli.install_setup._install_gwiki_from_cargo_install",
                return_value=True,
            ) as cargo_install,
            patch(
                "gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}
            ) as ensure_path,
        ):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gwiki").write_bytes(b"\x00")

            res = _install_gwiki()

        assert res["installed"] is True
        assert res["version"] == GWIKI_PIN
        github.assert_called_once_with(bin_dir, "aarch64-apple-darwin", GWIKI_PIN)
        binstall.assert_called_once_with(bin_dir, GWIKI_PIN)
        cargo_install.assert_called_once_with(bin_dir, GWIKI_PIN)
        ensure_path.assert_called_once_with(bin_dir)

    def test_install_gwiki_detects_missing_binary_after_install(self, tmp_path) -> None:
        with (
            patch("gobby.cli.install_setup.sys.platform", "darwin"),
            patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_installed_gwiki_version", return_value=None),
            patch("gobby.cli.install_setup._install_gwiki_from_submodule", return_value=False),
            patch("gobby.cli.install_setup._install_gwiki_from_github", return_value=True),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path") as ensure_path,
        ):
            res = _install_gwiki()

        assert res["installed"] is False
        assert res["skipped"] is False
        assert "did not create" in res["reason"]
        ensure_path.assert_not_called()

    @patch("gobby.cli.install_setup.urlopen")
    def test_get_latest_gwiki_version(self, mock_url):
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"crate": {"max_version": GWIKI_PIN}}).encode()
        fake_resp.__enter__.return_value = fake_resp
        mock_url.return_value = fake_resp

        assert _get_latest_gwiki_version() == GWIKI_PIN

    @patch("gobby.cli.install_setup.urlopen", side_effect=URLError("timeout"))
    def test_get_latest_gwiki_version_fail(self, mock_url):
        assert _get_latest_gwiki_version() is None

    @patch("gobby.cli.install_setup.urlopen")
    def test_install_gwiki_from_github_uses_wiki_tag_prefix(self, mock_urlopen, tmp_path):
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="gwiki")
            info.size = 5
            tar.addfile(info, BytesIO(b"fake!"))

        buf.seek(0)
        fake_resp = MagicMock()
        fake_resp.read.return_value = buf.read()
        fake_resp.__enter__.return_value = fake_resp
        mock_urlopen.return_value = fake_resp

        with patch("gobby.cli.install_setup._verify_release_artifact", return_value=True):
            assert _install_gwiki_from_github(tmp_path, "aarch64-apple-darwin", GWIKI_PIN) is True
        url_called = mock_urlopen.call_args[0][0]
        if hasattr(url_called, "full_url"):
            url_called = url_called.full_url
        assert f"gwiki-v{GWIKI_PIN}" in url_called
        assert (tmp_path / "gwiki").read_bytes() == b"fake!"

    @patch("shutil.which", return_value="/usr/bin/cargo-binstall")
    @patch("subprocess.run")
    def test_install_gwiki_from_cargo_binstall_with_version(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        assert _install_gwiki_from_cargo_binstall(tmp_path, GWIKI_PIN) is True
        cmd = mock_run.call_args[0][0]
        assert f"gobby-wiki@{GWIKI_PIN}" in cmd

    @patch("shutil.which", return_value="/usr/bin/cargo")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup.click")
    def test_install_gwiki_from_cargo_install_with_version(
        self, mock_click, mock_run, mock_which, tmp_path
    ):
        mock_run.return_value = MagicMock(returncode=0)
        assert _install_gwiki_from_cargo_install(tmp_path, GWIKI_PIN) is True
        cmd = mock_run.call_args[0][0]
        assert "gobby-wiki" in cmd
        assert "--version" in cmd
        assert GWIKI_PIN in cmd

    @patch("shutil.which", return_value="/usr/bin/cargo")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup.click")
    def test_install_gwiki_from_cargo_git_uses_wiki_package(
        self, mock_click, mock_run, mock_which, tmp_path
    ):
        mock_run.return_value = MagicMock(returncode=0)
        assert _install_gwiki_from_cargo_git(tmp_path) is True
        cmd = mock_run.call_args[0][0]
        assert "-p" in cmd
        assert "gobby-wiki" in cmd

    @patch("shutil.which", return_value="/usr/bin/cargo")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup.click")
    def test_install_gwiki_from_cargo_git_pins_default_pin_tag(
        self, mock_click, mock_run, mock_which, tmp_path
    ):
        # The cargo-git fallback must not build HEAD: with no explicit version
        # it pins --tag gwiki-v<managed pin>.
        mock_run.return_value = MagicMock(returncode=0)
        assert _install_gwiki_from_cargo_git(tmp_path) is True
        cmd = mock_run.call_args[0][0]
        assert "--tag" in cmd
        assert cmd[cmd.index("--tag") + 1] == f"gwiki-v{GWIKI_PIN}"

    @patch("shutil.which", return_value="/usr/bin/cargo")
    @patch("subprocess.run")
    @patch("gobby.cli.install_setup.click")
    def test_install_gwiki_from_cargo_git_pins_explicit_version_tag(
        self, mock_click, mock_run, mock_which, tmp_path
    ):
        mock_run.return_value = MagicMock(returncode=0)
        assert _install_gwiki_from_cargo_git(tmp_path, "9.9.9") is True
        cmd = mock_run.call_args[0][0]
        assert "--tag" in cmd
        assert cmd[cmd.index("--tag") + 1] == "gwiki-v9.9.9"


class TestEnsurePath:
    @patch("gobby.cli.install_setup.sys.platform", "linux")
    @patch("gobby.cli.install_setup.os.environ")
    @patch("gobby.cli.install_setup.Path.home")
    @patch("gobby.cli.install_setup.tempfile.gettempdir")
    def test_ensure_gobby_bin_on_path(
        self,
        mock_gettempdir: MagicMock,
        mock_home: MagicMock,
        mock_environ: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_environ.get.side_effect = lambda k, default="": "/bin/bash" if k == "SHELL" else ""
        mock_home.return_value = tmp_path
        mock_gettempdir.return_value = str(tmp_path / "systmp")

        bin_dir = tmp_path / "configured-gobby" / "bin"
        res = _ensure_gobby_bin_on_path(bin_dir)
        assert res["added"] is True
        assert res["shell"] == "bash"

        bashrc = tmp_path / ".bashrc"
        assert bashrc.exists()
        content = bashrc.read_text()
        assert f'export PATH={bin_dir.resolve()}:"$PATH"  # gobby\n' in content
        # $PATH must stay expandable: never swallowed into single quotes.
        assert "'$PATH'" not in content

        # Second run should skip
        res2 = _ensure_gobby_bin_on_path(bin_dir)
        assert res2["added"] is False

    @patch("gobby.cli.install_setup.sys.platform", "linux")
    @patch("gobby.cli.install_setup.os.environ")
    @patch("gobby.cli.install_setup.Path.home")
    def test_ensure_gobby_bin_on_path_refuses_tempdir_bin(
        self,
        mock_home: MagicMock,
        mock_environ: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_environ.get.side_effect = lambda k, default="": "/bin/bash" if k == "SHELL" else ""
        mock_home.return_value = tmp_path

        # tmp_path lives under the real tempfile.gettempdir(), so an isolated
        # GOBBY_HOME bin dir must be refused without touching any rc file.
        res = _ensure_gobby_bin_on_path(tmp_path / "ephemeral-gobby-home" / "bin")

        assert res == {"added": False}
        assert not (tmp_path / ".bashrc").exists()

    @patch("gobby.cli.install_setup.sys.platform", "linux")
    @patch("gobby.cli.install_setup.os.environ")
    @patch("gobby.cli.install_setup.Path.home")
    @patch("gobby.cli.install_setup.tempfile.gettempdir")
    def test_ensure_gobby_bin_on_path_line_sources_cleanly(
        self,
        mock_gettempdir: MagicMock,
        mock_home: MagicMock,
        mock_environ: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_environ.get.side_effect = lambda k, default="": "/bin/bash" if k == "SHELL" else ""
        mock_home.return_value = tmp_path
        mock_gettempdir.return_value = str(tmp_path / "systmp")

        bin_dir = tmp_path / "configured-gobby" / "bin"
        assert _ensure_gobby_bin_on_path(bin_dir)["added"] is True

        proc = subprocess.run(
            ["/bin/sh", "-c", f'. {tmp_path / ".bashrc"}; printf %s "$PATH"'],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout == f"{bin_dir.resolve()}:/usr/bin:/bin"

    @patch("gobby.cli.install_setup.sys.platform", "linux")
    @patch("gobby.cli.install_setup.os.environ")
    @patch("gobby.cli.install_setup.Path.home")
    @patch("gobby.cli.install_setup.tempfile.gettempdir")
    def test_ensure_gobby_bin_on_path_skips_non_utf8_rc(
        self,
        mock_gettempdir: MagicMock,
        mock_home: MagicMock,
        mock_environ: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_environ.get.side_effect = lambda k, default="": "/bin/bash" if k == "SHELL" else ""
        mock_home.return_value = tmp_path
        mock_gettempdir.return_value = str(tmp_path / "systmp")
        bashrc = tmp_path / ".bashrc"
        bashrc.write_bytes(b"\xff")

        res = _ensure_gobby_bin_on_path(tmp_path / "configured-gobby" / "bin")

        assert res == {"added": False}
        assert bashrc.read_bytes() == b"\xff"

    @patch("gobby.cli.install_setup.sys.platform", "linux")
    @patch("gobby.cli.install_setup.os.environ")
    @patch("gobby.cli.install_setup.Path.home")
    @patch("gobby.cli.install_setup.tempfile.gettempdir")
    def test_ensure_gobby_bin_on_path_skips_unreadable_rc(
        self,
        mock_gettempdir: MagicMock,
        mock_home: MagicMock,
        mock_environ: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_environ.get.side_effect = lambda k, default="": "/bin/bash" if k == "SHELL" else ""
        mock_home.return_value = tmp_path
        mock_gettempdir.return_value = str(tmp_path / "systmp")
        rc_file = tmp_path / ".bashrc"
        rc_file.write_text("# user config\n")

        with patch.object(Path, "read_text", side_effect=OSError("permission denied")) as read_text:
            res = _ensure_gobby_bin_on_path(tmp_path / "configured-gobby" / "bin")

        assert res == {"added": False}
        read_text.assert_called_once()
        assert rc_file.read_text() == "# user config\n"


def _checksum_resp(text: str) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = text.encode()
    resp.__enter__.return_value = resp
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _release_tarball(bin_name: str = "gcode", payload: bytes = b"fake-binary") -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=bin_name)
        info.size = len(payload)
        tar.addfile(info, BytesIO(payload))
    buf.seek(0)
    return buf.read()


def _archive_resp(data: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = data
    resp.__enter__.return_value = resp
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestParseSha256Digest:
    def test_bare_digest(self):
        digest = "a" * 64
        assert parse_sha256_digest(f"{digest}\n") == digest

    def test_sha256sum_line_format(self):
        digest = "b" * 64
        assert parse_sha256_digest(f"{digest}  gcode-aarch64-apple-darwin.tar.gz\n") == digest

    def test_uppercase_is_normalized(self):
        assert parse_sha256_digest("C" * 64) == "c" * 64

    def test_no_valid_digest_returns_none(self):
        assert parse_sha256_digest("not-a-checksum\n") is None
        assert parse_sha256_digest("") is None
        assert parse_sha256_digest("abc123\n") is None


class TestFetchReleaseChecksum:
    def test_success(self):
        digest = "d" * 64
        with patch("gobby.cli.install_setup.urlopen", return_value=_checksum_resp(f"{digest}\n")):
            result = _fetch_release_checksum("https://example.com/x.sha256", label="gcode")
        assert result == digest

    def test_network_error_returns_none(self):
        with patch("gobby.cli.install_setup.urlopen", side_effect=URLError("boom")):
            result = _fetch_release_checksum("https://example.com/x.sha256", label="gcode")
        assert result is None

    def test_unparseable_body_returns_none(self):
        with patch("gobby.cli.install_setup.urlopen", return_value=_checksum_resp("garbage\n")):
            result = _fetch_release_checksum("https://example.com/x.sha256", label="gcode")
        assert result is None


class TestVerifyReleaseArtifact:
    def test_matching_digest_passes(self):
        data = b"payload-bytes"
        digest = hashlib.sha256(data).hexdigest()
        with patch("gobby.cli.install_setup._fetch_release_checksum", return_value=digest):
            assert (
                _verify_release_artifact(data, checksum_url="https://x/.sha256", label="gcode")
                is True
            )

    def test_mismatched_digest_fails(self):
        with patch("gobby.cli.install_setup._fetch_release_checksum", return_value="0" * 64):
            assert (
                _verify_release_artifact(
                    b"payload-bytes", checksum_url="https://x/.sha256", label="gcode"
                )
                is False
            )

    def test_missing_checksum_fails_closed(self):
        with patch("gobby.cli.install_setup._fetch_release_checksum", return_value=None):
            assert (
                _verify_release_artifact(
                    b"payload-bytes", checksum_url="https://x/.sha256", label="gcode"
                )
                is False
            )


class TestDownloadReleaseBinaryChecksum:
    """Integration: _download_release_binary verifies before placement."""

    def _download(self, bin_dir: Path):
        return _download_release_binary(
            bin_dir,
            binary_name="gcode",
            artifact_name="gcode",
            target="aarch64-apple-darwin",
            version="0.1.0",
            tag_prefix="gcode-v",
            label="gcode",
        )

    def test_places_binary_when_checksum_matches(self, tmp_path):
        archive = _release_tarball("gcode")
        digest = hashlib.sha256(archive).hexdigest()
        # urlopen order: archive download, then checksum fetch.
        with patch(
            "gobby.cli.install_setup.urlopen",
            side_effect=[_archive_resp(archive), _checksum_resp(f"{digest}\n")],
        ):
            result = self._download(tmp_path)
        assert result is True
        assert (tmp_path / "gcode").exists()
        assert (tmp_path / "gcode").read_bytes() == b"fake-binary"

    def test_archive_promotion_stages_under_native_lock(self, tmp_path):
        archive = _release_tarball("gcode", b"new-binary")
        lock = MagicMock()

        with (
            patch(
                "gobby.cli.install_setup.try_acquire_native_bin_lock", return_value=lock
            ) as acquire_lock,
            patch("gobby.install.bin_freshness_promotion.os.replace", wraps=os.replace) as replace,
        ):
            result = _extract_binary_from_release_archive(
                archive,
                archive_ext="tar.gz",
                binary_name="gcode",
                bin_dir=tmp_path,
                label="gcode",
            )

        assert result is True
        assert (tmp_path / "gcode").read_bytes() == b"new-binary"
        acquire_lock.assert_called_once_with("gcode", bin_dir=tmp_path)
        lock.__enter__.assert_called_once_with()
        assert replace.call_args.args[1] == tmp_path / "gcode"

    def test_archive_promotion_preserves_binary_when_lock_is_held(self, tmp_path):
        destination = tmp_path / "gcode"
        destination.write_bytes(b"old-binary")

        with patch("gobby.cli.install_setup.try_acquire_native_bin_lock", return_value=None):
            result = _extract_binary_from_release_archive(
                _release_tarball("gcode", b"new-binary"),
                archive_ext="tar.gz",
                binary_name="gcode",
                bin_dir=tmp_path,
                label="gcode",
            )

        assert result is False
        assert destination.read_bytes() == b"old-binary"

    def test_rejects_and_skips_placement_on_mismatch(self, tmp_path):
        archive = _release_tarball("gcode")
        with patch(
            "gobby.cli.install_setup.urlopen",
            side_effect=[
                _archive_resp(archive),
                _checksum_resp(("0" * 64) + "\n"),
                _archive_resp(archive),
                _checksum_resp(("0" * 64) + "\n"),
            ],
        ):
            result = self._download(tmp_path)
        assert result is False
        assert not (tmp_path / "gcode").exists()

    def test_rejects_and_skips_placement_on_missing_checksum(self, tmp_path: Path) -> None:
        archive = _release_tarball("gcode")
        with patch(
            "gobby.cli.install_setup.urlopen",
            side_effect=[
                _archive_resp(archive),
                URLError("no checksum published"),
                _archive_resp(archive),
                URLError("no checksum published"),
            ],
        ):
            result = self._download(tmp_path)
        assert result is False
        assert not (tmp_path / "gcode").exists()
