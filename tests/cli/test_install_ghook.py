"""Tests for ghook binary installer in install_setup.py.

Tests version tracking, the workspace-only install source, and install provenance.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.install_setup import (
    _GHOOK_BIN_NAME,
    _GHOOK_INSTALL_SIDECAR,
    _GHOOK_VERSION_STAMP,
    _get_installed_ghook_version,
    _get_latest_ghook_version,
    _install_ghook,
    _install_ghook_from_workspace,
    _is_native_ghook_binary,
    _probe_ghook_version,
    _write_ghook_version_stamp,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit

_FIXED_INSTALLED_AT = "2026-04-22T18:30:00Z"


def _write_fake_ghook_binary(bin_dir: Path, name: str = "ghook") -> Path:
    """Create a fake ghook binary in the install bin directory."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / name
    binary.write_bytes(b"#!/bin/sh\necho fake-ghook\n")
    return binary


def _write_fake_native_ghook_binary(bin_dir: Path, name: str = "ghook") -> Path:
    """Create a fake native-looking ghook binary in the install bin directory."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / name
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"fake-ghook\n")
    return binary


def _read_ghook_sidecar(bin_dir: Path) -> dict[str, object]:
    """Load the ghook install provenance sidecar."""
    return json.loads((bin_dir / _GHOOK_INSTALL_SIDECAR).read_text())


class TestGetLatestGhookVersion:
    def test_success(self) -> None:
        payload = json.dumps({"crate": {"max_version": "0.1.1"}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_release.urlopen", return_value=mock_resp):
            assert _get_latest_ghook_version() == "0.1.1"

    def test_network_error(self) -> None:
        from urllib.error import URLError

        with patch("gobby.cli.install_release.urlopen", side_effect=URLError("timeout")):
            assert _get_latest_ghook_version() is None


class TestGetInstalledGhookVersion:
    def test_stamp_exists(self, tmp_path: Path) -> None:
        (tmp_path / _GHOOK_VERSION_STAMP).write_text("0.1.0\n")
        assert _get_installed_ghook_version(tmp_path) == "0.1.0"

    def test_binary_exists_no_stamp(self, tmp_path: Path) -> None:
        (tmp_path / _GHOOK_BIN_NAME).write_bytes(b"\x00")
        assert _get_installed_ghook_version(tmp_path) == "unknown"

    def test_no_binary_no_stamp(self, tmp_path: Path) -> None:
        assert _get_installed_ghook_version(tmp_path) is None


class TestWriteGhookVersionStamp:
    def test_writes_version(self, tmp_path: Path) -> None:
        _write_ghook_version_stamp(tmp_path, "0.1.0")
        assert (tmp_path / _GHOOK_VERSION_STAMP).read_text().strip() == "0.1.0"


class TestIsNativeGhookBinary:
    def test_accepts_native_executable_magic(self, tmp_path: Path) -> None:
        binary = _write_fake_native_ghook_binary(tmp_path)

        assert _is_native_ghook_binary(binary) is True

    def test_rejects_shell_wrapper(self, tmp_path: Path) -> None:
        binary = _write_fake_ghook_binary(tmp_path)

        assert _is_native_ghook_binary(binary) is False


class TestInstallGhookFromWorkspace:
    def test_builds_gobby_hooks_and_promotes_under_lock(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "crates" / "ghook").mkdir(parents=True)
        (workspace / "src" / "gobby" / "cli").mkdir(parents=True)
        (workspace / "Cargo.toml").touch()
        (workspace / "crates" / "ghook" / "Cargo.toml").touch()
        source = workspace / "target" / "release" / _GHOOK_BIN_NAME
        source.parent.mkdir(parents=True)
        source.write_bytes(b"new-binary")
        destination_dir = tmp_path / "bin"
        lock = MagicMock()

        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo"),
            patch(
                "gobby.cli.install_setup.subprocess.run", return_value=MagicMock(returncode=0)
            ) as run,
            patch("gobby.cli.install_setup.click"),
            patch(
                "gobby.cli.install_setup_ghook.__file__",
                str(workspace / "src" / "gobby" / "cli" / "install_setup_ghook.py"),
            ),
            patch(
                "gobby.install.bin_set_coherence.try_acquire_native_bin_lock",
                return_value=lock,
            ) as acquire_lock,
            patch(
                "gobby.install.bin_set_coherence.probe_set_member_identity",
                return_value={
                    "runner_protocol": 1,
                    "baseline_version": 420,
                    "baseline_checksum": "baseline-420",
                    "latest_version": 420,
                    "latest_checksum": "latest-420",
                    "assets_root_hash": "root-420",
                },
            ),
        ):
            result = _install_ghook_from_workspace(destination_dir)

        assert result is True
        assert (destination_dir / _GHOOK_BIN_NAME).read_bytes() == b"new-binary"
        build = run.call_args_list[0].args[0]
        assert build[:5] == ["cargo", "build", "--release", "-p", "gobby-hooks"]
        assert build[-1] == str(workspace / "Cargo.toml")
        acquire_lock.assert_called_once_with("ghook", bin_dir=destination_dir)

    def test_without_cargo_builds_nothing(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value=None),
            patch("gobby.cli.install_setup.subprocess.run") as run,
        ):
            assert _install_ghook_from_workspace(tmp_path) is False

        run.assert_not_called()
        assert list(tmp_path.iterdir()) == []


class TestProbeGhookVersion:
    def test_returns_last_token(self, tmp_path: Path) -> None:
        ghook_path = tmp_path / "ghook"
        ghook_path.touch()
        with patch("gobby.cli.install_setup.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ghook 0.1.1\n", stderr="")
            assert _probe_ghook_version(ghook_path) == "0.1.1"


class TestInstallGhook:
    @pytest.fixture()
    def _patch_platform(self):
        with (
            patch("gobby.cli.install_setup.sys.platform", "darwin"),
            patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
        ):
            yield

    @pytest.fixture()
    def _fixed_installed_at(self):
        with patch(
            "gobby.cli.install_setup._ghook_installed_at_utc",
            return_value=_FIXED_INSTALLED_AT,
        ):
            yield

    def test_install_ghook_passes_resolved_bin_dir(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def install_from_workspace_side_effect(*args: object, **kwargs: object) -> bool:
            _write_fake_ghook_binary(bin_dir)
            return True

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch(
                "gobby.cli.install_setup._install_ghook_from_workspace",
                side_effect=install_from_workspace_side_effect,
            ),
            patch("gobby.cli.install_setup._probe_ghook_version", return_value="0.1.1"),
            patch(
                "gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}
            ) as ensure_path,
        ):
            result = _install_ghook()

        assert result["installed"] is True
        assert result["method"] == "workspace"
        assert result["version"] == "0.1.1"
        ensure_path.assert_called_once_with(bin_dir)
        assert _read_ghook_sidecar(bin_dir) == {
            "install_method": "workspace",
            "install_source_url": None,
            "installed_version": "0.1.1",
            "installed_at": _FIXED_INSTALLED_AT,
        }
        assert (bin_dir / _GHOOK_INSTALL_SIDECAR).stat().st_mode & 0o777 == 0o644

    def test_already_up_to_date(self, tmp_path: Path, _patch_platform: None) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        _write_fake_native_ghook_binary(bin_dir)
        pinned_version = MANAGED_BIN_VERSION_PINS["ghook"]
        (bin_dir / _GHOOK_VERSION_STAMP).write_text(f"{pinned_version}\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version") as mock_latest,
            patch("gobby.cli.install_setup._install_ghook_from_workspace") as mock_workspace,
        ):
            result = _install_ghook()

        assert result["installed"] is False
        assert result["skipped"] is True
        assert result["version"] == pinned_version
        mock_latest.assert_not_called()
        mock_workspace.assert_not_called()

    def test_newer_installed_version_skips_when_latest_is_lower(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        _write_fake_native_ghook_binary(bin_dir)
        (bin_dir / _GHOOK_VERSION_STAMP).write_text(f"{MANAGED_BIN_VERSION_PINS['ghook']}\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version") as mock_latest,
            patch("gobby.cli.install_setup._install_ghook_from_workspace") as mock_workspace,
        ):
            result = _install_ghook()

        assert result == {
            "installed": False,
            "skipped": True,
            "version": MANAGED_BIN_VERSION_PINS["ghook"],
        }
        mock_latest.assert_not_called()
        mock_workspace.assert_not_called()

    def test_replaces_shell_wrapper_even_when_version_stamp_satisfies_pin(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        wrapper = _write_fake_ghook_binary(bin_dir)
        pinned_version = MANAGED_BIN_VERSION_PINS["ghook"]
        (bin_dir / _GHOOK_VERSION_STAMP).write_text(f"{pinned_version}\n")

        def install_from_workspace_side_effect(*args: object, **kwargs: object) -> bool:
            _write_fake_native_ghook_binary(bin_dir)
            return True

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch(
                "gobby.cli.install_setup._install_ghook_from_workspace",
                side_effect=install_from_workspace_side_effect,
            ) as mock_workspace,
            patch("gobby.cli.install_setup._probe_ghook_version", return_value=pinned_version),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            result = _install_ghook()

        assert wrapper.read_bytes().startswith(b"\xcf\xfa\xed\xfe")
        assert result["installed"] is True
        assert result["method"] == "workspace"
        assert result["version"] == pinned_version
        mock_workspace.assert_called_once()

    def test_sidecar_write_failure_logs_warning_and_install_still_succeeds(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def install_from_workspace_side_effect(*args: object, **kwargs: object) -> bool:
            _write_fake_ghook_binary(bin_dir)
            return True

        real_os_chmod = __import__("os").chmod

        def chmod_side_effect(
            path: str | bytes | Path,
            mode: int,
            *args: object,
            **kwargs: object,
        ) -> None:
            if Path(path).name == _GHOOK_INSTALL_SIDECAR:
                raise PermissionError("sidecar chmod blocked")
            real_os_chmod(path, mode, *args, **kwargs)

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch(
                "gobby.cli.install_setup._install_ghook_from_workspace",
                side_effect=install_from_workspace_side_effect,
            ),
            patch("gobby.cli.install_setup._probe_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch("gobby.cli.install_setup.os.chmod", side_effect=chmod_side_effect),
            caplog.at_level("WARNING", logger="gobby.cli.install_setup"),
        ):
            result = _install_ghook(force=True)

        assert result["installed"] is True
        assert result["method"] == "workspace"
        assert "failed writing install sidecar" in caplog.text
        assert (bin_dir / _GHOOK_INSTALL_SIDECAR).exists()

    def test_workspace_build_unavailable_refuses(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._install_ghook_from_workspace", return_value=False),
        ):
            result = _install_ghook()

        assert result["installed"] is False
        assert "ghook installs only from the Gobby workspace build" in result["reason"]

    def test_unsupported_platform(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.sys.platform", "freebsd"),
            patch("gobby.cli.install_setup.platform.machine", return_value="mips"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _install_ghook()

        assert result["skipped"] is True
        assert "unsupported platform" in result["reason"]
