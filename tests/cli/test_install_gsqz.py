"""Tests for gsqz binary installer in install_setup.py.

Tests version tracking, fallback chain (GitHub → cargo-binstall → cargo install),
Windows support, and PATH setup.
"""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.install_setup import (
    _GSQZ_BIN_NAME,
    _GSQZ_VERSION_STAMP,
    _ensure_gobby_bin_on_path,
    _get_installed_gsqz_version,
    _get_latest_gsqz_version,
    _install_gsqz,
    _install_gsqz_from_cargo_binstall,
    _install_gsqz_from_cargo_install,
    _install_gsqz_from_github,
    _write_gsqz_version_stamp,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit


def _make_tarball(bin_name: str = "gsqz") -> io.BytesIO:
    """Create an in-memory tar.gz containing a fake gsqz binary."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho fake-gsqz\n"
        info = tarfile.TarInfo(name=bin_name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def _make_zip(bin_name: str = "gsqz.exe") -> io.BytesIO:
    """Create an in-memory zip containing a fake Windows binary."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as archive:
        archive.writestr(bin_name, b"fake-gsqz")
    buf.seek(0)
    return buf


class TestGetLatestGsqzVersion:
    """Tests for _get_latest_gsqz_version."""

    def test_success(self) -> None:
        payload = json.dumps({"crate": {"max_version": "0.2.0"}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp):
            assert _get_latest_gsqz_version() == "0.2.0"

    def test_network_error(self) -> None:
        from urllib.error import URLError

        with patch("gobby.cli.install_setup.urlopen", side_effect=URLError("timeout")):
            assert _get_latest_gsqz_version() is None

    def test_bad_json(self) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp):
            assert _get_latest_gsqz_version() is None

    def test_missing_key(self) -> None:
        payload = json.dumps({"crate": {}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp):
            assert _get_latest_gsqz_version() is None


class TestGetInstalledGsqzVersion:
    """Tests for _get_installed_gsqz_version."""

    def test_stamp_exists(self, tmp_path: Path) -> None:
        (tmp_path / _GSQZ_VERSION_STAMP).write_text("0.1.0\n")
        assert _get_installed_gsqz_version(tmp_path) == "0.1.0"

    def test_no_stamp_no_binary(self, tmp_path: Path) -> None:
        assert _get_installed_gsqz_version(tmp_path) is None

    def test_binary_exists_no_stamp(self, tmp_path: Path) -> None:
        (tmp_path / _GSQZ_BIN_NAME).write_bytes(b"\x00")
        assert _get_installed_gsqz_version(tmp_path) == "unknown"

    def test_empty_stamp(self, tmp_path: Path) -> None:
        (tmp_path / _GSQZ_VERSION_STAMP).write_text("")
        assert _get_installed_gsqz_version(tmp_path) is None


class TestWriteGsqzVersionStamp:
    """Tests for _write_gsqz_version_stamp."""

    def test_writes_version(self, tmp_path: Path) -> None:
        _write_gsqz_version_stamp(tmp_path, "0.1.0")
        stamp = tmp_path / _GSQZ_VERSION_STAMP
        assert stamp.exists()
        assert stamp.read_text().strip() == "0.1.0"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        _write_gsqz_version_stamp(tmp_path, "0.1.0")
        _write_gsqz_version_stamp(tmp_path, "0.2.0")
        stamp = tmp_path / _GSQZ_VERSION_STAMP
        assert stamp.read_text().strip() == "0.2.0"


class TestInstallGsqzFromGithub:
    """Tests for _install_gsqz_from_github."""

    def test_success(self, tmp_path: Path) -> None:
        tarball = _make_tarball("gsqz")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag",
                return_value="gsqz-v0.1.0",
            ),
        ):
            assert _install_gsqz_from_github(tmp_path, "aarch64-apple-darwin") is True
        assert (tmp_path / "gsqz").exists()

    def test_success_with_version(self, tmp_path: Path) -> None:
        tarball = _make_tarball("gsqz")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp) as mock_urlopen:
            assert _install_gsqz_from_github(tmp_path, "aarch64-apple-darwin", "0.1.0") is True
        url_called = mock_urlopen.call_args[0][0]
        if hasattr(url_called, "full_url"):
            url_called = url_called.full_url
        assert "gsqz-v0.1.0" in url_called

    def test_nested_path_in_tarball(self, tmp_path: Path) -> None:
        tarball = _make_tarball("gsqz-v0.1.0/gsqz")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag",
                return_value="gsqz-v0.1.0",
            ),
        ):
            assert _install_gsqz_from_github(tmp_path, "aarch64-apple-darwin") is True
        assert (tmp_path / "gsqz").exists()

    def test_network_failure(self, tmp_path: Path) -> None:
        from urllib.error import URLError

        with (
            patch("gobby.cli.install_setup.urlopen", side_effect=URLError("fail")),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag",
                return_value="gsqz-v0.1.0",
            ),
        ):
            assert _install_gsqz_from_github(tmp_path, "aarch64-apple-darwin") is False

    def test_missing_binary_in_tarball(self, tmp_path: Path) -> None:
        tarball = _make_tarball("not-gsqz")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag",
                return_value="gsqz-v0.1.0",
            ),
        ):
            result = _install_gsqz_from_github(tmp_path, "aarch64-apple-darwin")

        assert result is False
        assert not (tmp_path / "gsqz").exists()

    def test_windows_exe(self, tmp_path: Path) -> None:
        archive = _make_zip("gsqz.exe")
        mock_resp = MagicMock()
        mock_resp.read.return_value = archive.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag",
                return_value="gsqz-v0.1.0",
            ),
            patch("gobby.cli.install_setup._GSQZ_BIN_NAME", "gsqz.exe"),
        ):
            assert _install_gsqz_from_github(tmp_path, "x86_64-pc-windows-msvc") is True
        assert (tmp_path / "gsqz.exe").exists()


class TestInstallGsqzFromCargoBinstall:
    """Tests for _install_gsqz_from_cargo_binstall."""

    def test_success(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert _install_gsqz_from_cargo_binstall(tmp_path) is True

    def test_with_version(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _install_gsqz_from_cargo_binstall(tmp_path, "0.1.0")
            cmd = mock_run.call_args[0][0]
            assert "gobby-squeeze@0.1.0" in cmd

    def test_not_available(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value=None),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            assert _install_gsqz_from_cargo_binstall(tmp_path) is False
            mock_run.assert_not_called()

    def test_timeout(self, tmp_path: Path) -> None:
        import subprocess

        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch(
                "gobby.cli.install_setup.subprocess.run",
                side_effect=subprocess.TimeoutExpired("cmd", 60),
            ),
        ):
            assert _install_gsqz_from_cargo_binstall(tmp_path) is False

    def test_nonzero_exit(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            assert _install_gsqz_from_cargo_binstall(tmp_path) is False


class TestInstallGsqzFromCargoInstall:
    """Tests for _install_gsqz_from_cargo_install."""

    def test_success(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
            patch("gobby.cli.install_setup.click"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert _install_gsqz_from_cargo_install(tmp_path) is True
            cmd = mock_run.call_args[0][0]
            assert "--root" in cmd

    def test_with_version(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
            patch("gobby.cli.install_setup.click"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _install_gsqz_from_cargo_install(tmp_path, "0.1.0")
            cmd = mock_run.call_args[0][0]
            assert "--version" in cmd
            assert "0.1.0" in cmd

    def test_no_cargo(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value=None),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            assert _install_gsqz_from_cargo_install(tmp_path) is False
            mock_run.assert_not_called()

    def test_compilation_failure(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
            patch("gobby.cli.install_setup.click"),
        ):
            mock_run.return_value = MagicMock(returncode=101)
            result = _install_gsqz_from_cargo_install(tmp_path)

        assert result is False
        assert not (tmp_path / "gsqz").exists()


class TestInstallGsqz:
    """Integration tests for _install_gsqz main function."""

    @pytest.fixture()
    def _patch_platform(self):
        """Patch platform detection to darwin/arm64."""
        with (
            patch("gobby.cli.install_setup.sys.platform", "darwin"),
            patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
        ):
            yield

    def test_fresh_install_github(self, tmp_path: Path, _patch_platform: None) -> None:
        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value="0.1.0"),
            patch("gobby.cli.install_setup._install_gsqz_from_github", return_value=True),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            # Create the binary so chmod doesn't fail
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gsqz").write_bytes(b"\x00")

            result = _install_gsqz()

        assert result["installed"] is True
        assert result["method"] == "github"
        assert result["version"] == "0.1.0"

    def test_already_up_to_date(self, tmp_path: Path, _patch_platform: None) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "gsqz").write_bytes(b"\x00")
        pinned_version = MANAGED_BIN_VERSION_PINS["gsqz"]
        (bin_dir / _GSQZ_VERSION_STAMP).write_text(f"{pinned_version}\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value=pinned_version),
        ):
            result = _install_gsqz()

        assert result["installed"] is False
        assert result["skipped"] is True
        assert result["version"] == pinned_version

    def test_newer_installed_version_skips_when_latest_is_lower(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "gsqz").write_bytes(b"\x00")
        (bin_dir / _GSQZ_VERSION_STAMP).write_text("0.4.3\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value="0.4.2"),
            patch("gobby.cli.install_setup._install_gsqz_from_github") as mock_github,
        ):
            result = _install_gsqz()

        assert result == {"installed": False, "skipped": True, "version": "0.4.3"}
        mock_github.assert_not_called()

    def test_upgrade_available(self, tmp_path: Path, _patch_platform: None) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "gsqz").write_bytes(b"\x00")
        (bin_dir / _GSQZ_VERSION_STAMP).write_text("0.1.0\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value="0.2.0"),
            patch("gobby.cli.install_setup._install_gsqz_from_github", return_value=True),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            result = _install_gsqz()

        assert result["installed"] is True
        assert result["upgraded"] is True
        assert result["version"] == "0.2.0"

    def test_force_reinstall(self, tmp_path: Path, _patch_platform: None) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "gsqz").write_bytes(b"\x00")
        (bin_dir / _GSQZ_VERSION_STAMP).write_text("0.1.0\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value="0.1.0"),
            patch("gobby.cli.install_setup._install_gsqz_from_github", return_value=True),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            result = _install_gsqz(force=True)

        assert result["installed"] is True
        assert result["version"] == "0.1.0"

    def test_github_fails_cargo_binstall_succeeds(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value="0.1.0"),
            patch("gobby.cli.install_setup._install_gsqz_from_github", return_value=False),
            patch("gobby.cli.install_setup._install_gsqz_from_cargo_binstall", return_value=True),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            (bin_dir / "gsqz").write_bytes(b"\x00")
            result = _install_gsqz()

        assert result["installed"] is True
        assert result["method"] == "cargo-binstall"

    def test_all_methods_fail(self, tmp_path: Path, _patch_platform: None) -> None:
        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value="0.1.0"),
            patch("gobby.cli.install_setup._install_gsqz_from_github", return_value=False),
            patch("gobby.cli.install_setup._install_gsqz_from_cargo_binstall", return_value=False),
            patch("gobby.cli.install_setup._install_gsqz_from_cargo_install", return_value=False),
        ):
            result = _install_gsqz()

        assert result["installed"] is False
        assert "all installation methods failed" in result["reason"]

    def test_unsupported_platform(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.sys.platform", "freebsd"),
            patch("gobby.cli.install_setup.platform.machine", return_value="mips"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _install_gsqz()

        assert result["skipped"] is True
        assert "unsupported platform" in result["reason"]

    def test_network_failure_keeps_existing_when_pin_satisfied(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "gsqz").write_bytes(b"\x00")
        pinned_version = MANAGED_BIN_VERSION_PINS["gsqz"]
        (bin_dir / _GSQZ_VERSION_STAMP).write_text(f"{pinned_version}\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gsqz_version", return_value=None),
        ):
            result = _install_gsqz()

        assert result["skipped"] is True
        assert result["version"] == pinned_version


class TestEnsureGobbyBinOnPath:
    """Tests for _ensure_gobby_bin_on_path."""

    def test_already_on_path(self, tmp_path: Path) -> None:
        gobby_bin = str(tmp_path / ".gobby" / "bin")
        with patch.dict("os.environ", {"PATH": f"{gobby_bin}:/usr/bin", "SHELL": "/bin/zsh"}):
            with patch("gobby.cli.install_setup.Path.home", return_value=tmp_path):
                result = _ensure_gobby_bin_on_path()
        assert result["added"] is False

    def test_zsh_appends(self, tmp_path: Path) -> None:
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text("# existing config\n")

        with (
            patch.dict("os.environ", {"PATH": "/usr/bin", "SHELL": "/bin/zsh"}),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _ensure_gobby_bin_on_path()

        assert result["added"] is True
        assert result["shell"] == "zsh"
        content = zshrc.read_text()
        assert ".gobby/bin" in content
        assert "# gobby" in content

    def test_bash_appends(self, tmp_path: Path) -> None:
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("# existing config\n")

        with (
            patch.dict("os.environ", {"PATH": "/usr/bin", "SHELL": "/bin/bash"}),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _ensure_gobby_bin_on_path()

        assert result["added"] is True
        assert result["shell"] == "bash"
        content = bashrc.read_text()
        assert ".gobby/bin" in content
        assert "# gobby" in content

    def test_fish_appends(self, tmp_path: Path) -> None:
        fish_config = tmp_path / ".config" / "fish" / "config.fish"
        fish_config.parent.mkdir(parents=True, exist_ok=True)
        fish_config.write_text("# fish config\n")

        with (
            patch.dict("os.environ", {"PATH": "/usr/bin", "SHELL": "/usr/bin/fish"}),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _ensure_gobby_bin_on_path()

        assert result["added"] is True
        assert result["shell"] == "fish"
        content = fish_config.read_text()
        assert "fish_add_path" in content
        assert "# gobby" in content

    def test_duplicate_guard(self, tmp_path: Path) -> None:
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text('export PATH="$HOME/.gobby/bin:$PATH"  # gobby\n')

        with (
            patch.dict("os.environ", {"PATH": "/usr/bin", "SHELL": "/bin/zsh"}),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _ensure_gobby_bin_on_path()

        assert result["added"] is False

    def test_windows_skips(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.sys.platform", "win32"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch.dict("os.environ", {"PATH": "/usr/bin"}),
            patch("gobby.cli.install_setup.click"),
        ):
            result = _ensure_gobby_bin_on_path()

        assert result["added"] is False

    def test_unknown_shell(self, tmp_path: Path) -> None:
        with (
            patch.dict("os.environ", {"PATH": "/usr/bin", "SHELL": "/bin/csh"}),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _ensure_gobby_bin_on_path()

        assert result["added"] is False
