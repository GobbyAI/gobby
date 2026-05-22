"""Tests for ghook binary installer in install_setup.py.

Tests version tracking, fallback chain (GitHub -> cargo-binstall -> cargo install),
release-tag parity, Windows asset extraction, and validation overrides.
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
    _GHOOK_BIN_NAME,
    _GHOOK_INSTALL_METHOD_ENV,
    _GHOOK_INSTALL_SIDECAR,
    _GHOOK_INSTALL_VERSION_ENV,
    _GHOOK_VERSION_STAMP,
    _get_installed_ghook_version,
    _get_latest_ghook_version,
    _install_ghook,
    _install_ghook_from_cargo_binstall,
    _install_ghook_from_cargo_install,
    _install_ghook_from_github,
    _probe_ghook_version,
    _write_ghook_version_stamp,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit

_FIXED_INSTALLED_AT = "2026-04-22T18:30:00Z"


def _make_tarball(bin_name: str = "ghook") -> io.BytesIO:
    """Create an in-memory tar.gz containing a fake ghook binary."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho fake-ghook\n"
        info = tarfile.TarInfo(name=bin_name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def _make_zip(bin_name: str = "ghook.exe") -> io.BytesIO:
    """Create an in-memory zip containing a fake Windows ghook binary."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as archive:
        archive.writestr(bin_name, b"fake-ghook")
    buf.seek(0)
    return buf


def _write_fake_ghook_binary(bin_dir: Path, name: str = "ghook") -> Path:
    """Create a fake ghook binary in the install bin directory."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / name
    binary.write_bytes(b"#!/bin/sh\necho fake-ghook\n")
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

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp):
            assert _get_latest_ghook_version() == "0.1.1"

    def test_network_error(self) -> None:
        from urllib.error import URLError

        with patch("gobby.cli.install_setup.urlopen", side_effect=URLError("timeout")):
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


class TestInstallGhookFromGithub:
    def test_success(self, tmp_path: Path) -> None:
        tarball = _make_tarball("ghook")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag",
                return_value="ghook-v0.1.1",
            ),
        ):
            assert _install_ghook_from_github(tmp_path, "aarch64-apple-darwin") is True
        assert (tmp_path / "ghook").exists()

    def test_versioned_url_uses_release_tag_prefix(self, tmp_path: Path) -> None:
        tarball = _make_tarball("ghook")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp) as mock_urlopen:
            assert _install_ghook_from_github(tmp_path, "aarch64-apple-darwin", "0.1.1") is True
        url_called = mock_urlopen.call_args[0][0]
        if hasattr(url_called, "full_url"):
            url_called = url_called.full_url
        assert "ghook-v0.1.1" in url_called

    def test_windows_zip_asset(self, tmp_path: Path) -> None:
        archive = _make_zip("ghook.exe")
        mock_resp = MagicMock()
        mock_resp.read.return_value = archive.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag",
                return_value="ghook-v0.1.1",
            ),
            patch("gobby.cli.install_setup._GHOOK_BIN_NAME", "ghook.exe"),
        ):
            assert _install_ghook_from_github(tmp_path, "x86_64-pc-windows-msvc") is True
        assert (tmp_path / "ghook.exe").exists()


class TestInstallGhookFromCargoBinstall:
    def test_success(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert _install_ghook_from_cargo_binstall(tmp_path) is True

    def test_with_version(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _install_ghook_from_cargo_binstall(tmp_path, "0.1.1")
            cmd = mock_run.call_args[0][0]
            assert "gobby-hooks@0.1.1" in cmd


class TestInstallGhookFromCargoInstall:
    def test_success(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
            patch("gobby.cli.install_setup.click"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert _install_ghook_from_cargo_install(tmp_path) is True
            cmd = mock_run.call_args[0][0]
            assert "gobby-hooks" in cmd


class TestProbeGhookVersion:
    def test_returns_last_token(self, tmp_path: Path) -> None:
        with patch("gobby.cli.install_setup.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ghook 0.1.1\n", stderr="")
            assert _probe_ghook_version(tmp_path / "ghook") == "0.1.1"


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

    def test_fresh_install_github(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def install_from_github_side_effect(*args: object, **kwargs: object) -> bool:
            _write_fake_ghook_binary(bin_dir)
            return True

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch(
                "gobby.cli.install_setup._install_ghook_from_github",
                side_effect=install_from_github_side_effect,
            ),
            patch("gobby.cli.install_setup._probe_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            result = _install_ghook()

        assert result["installed"] is True
        assert result["method"] == "github"
        assert result["version"] == "0.1.1"
        assert _read_ghook_sidecar(bin_dir) == {
            "install_method": "github-release",
            "install_source_url": (
                "https://github.com/GobbyAI/gobby-cli/releases/download/"
                "ghook-v0.1.1/ghook-aarch64-apple-darwin.tar.gz"
            ),
            "installed_version": "0.1.1",
            "installed_at": _FIXED_INSTALLED_AT,
        }
        assert (bin_dir / _GHOOK_INSTALL_SIDECAR).stat().st_mode & 0o777 == 0o644

    def test_already_up_to_date(self, tmp_path: Path, _patch_platform: None) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "ghook").write_bytes(b"\x00")
        pinned_version = MANAGED_BIN_VERSION_PINS["ghook"]
        (bin_dir / _GHOOK_VERSION_STAMP).write_text(f"{pinned_version}\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value=pinned_version),
        ):
            result = _install_ghook()

        assert result["installed"] is False
        assert result["skipped"] is True

    def test_newer_installed_version_skips_when_latest_is_lower(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "ghook").write_bytes(b"\x00")
        (bin_dir / _GHOOK_VERSION_STAMP).write_text("0.4.2\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.4.1"),
            patch("gobby.cli.install_setup._install_ghook_from_github") as mock_github,
        ):
            result = _install_ghook()

        assert result == {"installed": False, "skipped": True, "version": "0.4.2"}
        mock_github.assert_not_called()

    def test_version_override_skips_crates_lookup(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version") as mock_latest,
            patch("gobby.cli.install_setup._install_ghook_from_github", return_value=True),
            patch("gobby.cli.install_setup._probe_ghook_version", return_value="0.1.0"),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch.dict("os.environ", {_GHOOK_INSTALL_VERSION_ENV: "0.1.0"}),
        ):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "ghook").write_bytes(b"\x00")

            result = _install_ghook(force=True)

        mock_latest.assert_not_called()
        assert mock_latest.call_count == 0
        assert not mock_latest.called
        assert result["version"] == "0.1.0"

    def test_method_override_github(self, tmp_path: Path, _patch_platform: None) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def install_from_github_side_effect(*args: object, **kwargs: object) -> bool:
            _write_fake_ghook_binary(bin_dir)
            return True

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch(
                "gobby.cli.install_setup._install_ghook_from_github",
                side_effect=install_from_github_side_effect,
            ),
            patch("gobby.cli.install_setup._install_ghook_from_cargo_binstall") as mock_binstall,
            patch("gobby.cli.install_setup._install_ghook_from_cargo_install") as mock_install,
            patch("gobby.cli.install_setup._probe_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch.dict("os.environ", {_GHOOK_INSTALL_METHOD_ENV: "github"}),
        ):
            result = _install_ghook(force=True)

        mock_binstall.assert_not_called()
        assert mock_binstall.call_count == 0
        assert not mock_binstall.called
        mock_install.assert_not_called()
        assert mock_install.call_count == 0
        assert not mock_install.called
        assert result["method"] == "github"

    def test_method_override_cargo_binstall(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def subprocess_run_side_effect(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int,
        ) -> MagicMock:
            if cmd[0] == "cargo-binstall":
                _write_fake_ghook_binary(bin_dir)
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[0] == str(bin_dir / "ghook") and cmd[1:] == ["--version"]:
                return MagicMock(returncode=0, stdout="ghook 0.1.1\n", stderr="")
            raise AssertionError(f"unexpected subprocess.run command: {cmd}")

        def which_side_effect(tool: str) -> str | None:
            if tool == "cargo-binstall":
                return "/usr/bin/cargo-binstall"
            return None

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._install_ghook_from_github") as mock_github,
            patch("gobby.cli.install_setup.shutil.which", side_effect=which_side_effect),
            patch("gobby.cli.install_setup.subprocess.run", side_effect=subprocess_run_side_effect),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch.dict("os.environ", {_GHOOK_INSTALL_METHOD_ENV: "cargo-binstall"}),
        ):
            mock_github.return_value = False
            result = _install_ghook(force=True)

        mock_github.assert_not_called()
        assert result["method"] == "cargo-binstall"
        assert _read_ghook_sidecar(bin_dir) == {
            "install_method": "crates-binstall",
            "install_source_url": "https://crates.io/crates/gobby-hooks/0.1.1",
            "installed_version": "0.1.1",
            "installed_at": _FIXED_INSTALLED_AT,
        }

    def test_method_override_cargo_install(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def subprocess_run_side_effect(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int,
        ) -> MagicMock:
            if cmd[:2] == ["cargo", "install"]:
                _write_fake_ghook_binary(bin_dir)
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[0] == str(bin_dir / "ghook") and cmd[1:] == ["--version"]:
                return MagicMock(returncode=0, stdout="ghook 0.1.1\n", stderr="")
            raise AssertionError(f"unexpected subprocess.run command: {cmd}")

        def which_side_effect(tool: str) -> str | None:
            if tool == "cargo":
                return "/usr/bin/cargo"
            return None

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._install_ghook_from_github") as mock_github,
            patch("gobby.cli.install_setup._install_ghook_from_cargo_binstall") as mock_binstall,
            patch("gobby.cli.install_setup.shutil.which", side_effect=which_side_effect),
            patch("gobby.cli.install_setup.subprocess.run", side_effect=subprocess_run_side_effect),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch.dict("os.environ", {_GHOOK_INSTALL_METHOD_ENV: "cargo-install"}),
        ):
            mock_github.return_value = False
            mock_binstall.return_value = False
            result = _install_ghook(force=True)

        mock_github.assert_not_called()
        mock_binstall.assert_not_called()
        assert result["method"] == "cargo-install"
        assert _read_ghook_sidecar(bin_dir) == {
            "install_method": "cargo-install",
            "install_source_url": None,
            "installed_version": "0.1.1",
            "installed_at": _FIXED_INSTALLED_AT,
        }

    def test_github_fails_then_cargo_binstall_writes_sidecar(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def subprocess_run_side_effect(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int,
        ) -> MagicMock:
            if cmd[0] == "cargo-binstall":
                _write_fake_ghook_binary(bin_dir)
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[0] == str(bin_dir / "ghook") and cmd[1:] == ["--version"]:
                return MagicMock(returncode=0, stdout="ghook 0.1.1\n", stderr="")
            raise AssertionError(f"unexpected subprocess.run command: {cmd}")

        def which_side_effect(tool: str) -> str | None:
            if tool == "cargo-binstall":
                return "/usr/bin/cargo-binstall"
            return None

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._install_ghook_from_github", return_value=False),
            patch("gobby.cli.install_setup.shutil.which", side_effect=which_side_effect),
            patch("gobby.cli.install_setup.subprocess.run", side_effect=subprocess_run_side_effect),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            result = _install_ghook(force=True)

        assert result["installed"] is True
        assert result["method"] == "cargo-binstall"
        assert _read_ghook_sidecar(bin_dir) == {
            "install_method": "crates-binstall",
            "install_source_url": "https://crates.io/crates/gobby-hooks/0.1.1",
            "installed_version": "0.1.1",
            "installed_at": _FIXED_INSTALLED_AT,
        }

    def test_github_and_cargo_binstall_fail_then_cargo_install_writes_sidecar(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def subprocess_run_side_effect(
            cmd: list[str],
            capture_output: bool,
            text: bool,
            timeout: int,
        ) -> MagicMock:
            if cmd[0] == "cargo-binstall":
                return MagicMock(returncode=1, stdout="", stderr="download failed")
            if cmd[:2] == ["cargo", "install"]:
                _write_fake_ghook_binary(bin_dir)
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[0] == str(bin_dir / "ghook") and cmd[1:] == ["--version"]:
                return MagicMock(returncode=0, stdout="ghook 0.1.1\n", stderr="")
            raise AssertionError(f"unexpected subprocess.run command: {cmd}")

        def which_side_effect(tool: str) -> str | None:
            if tool == "cargo-binstall":
                return "/usr/bin/cargo-binstall"
            if tool == "cargo":
                return "/usr/bin/cargo"
            return None

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._install_ghook_from_github", return_value=False),
            patch("gobby.cli.install_setup.shutil.which", side_effect=which_side_effect),
            patch("gobby.cli.install_setup.subprocess.run", side_effect=subprocess_run_side_effect),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch("gobby.cli.install_setup.click"),
        ):
            result = _install_ghook(force=True)

        assert result["installed"] is True
        assert result["method"] == "cargo-install"
        assert _read_ghook_sidecar(bin_dir) == {
            "install_method": "cargo-install",
            "install_source_url": None,
            "installed_version": "0.1.1",
            "installed_at": _FIXED_INSTALLED_AT,
        }

    def test_sidecar_write_failure_logs_warning_and_install_still_succeeds(
        self,
        tmp_path: Path,
        _patch_platform: None,
        _fixed_installed_at: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"

        def install_from_github_side_effect(*args: object, **kwargs: object) -> bool:
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
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch(
                "gobby.cli.install_setup._install_ghook_from_github",
                side_effect=install_from_github_side_effect,
            ),
            patch("gobby.cli.install_setup._probe_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
            patch("gobby.cli.install_setup.os.chmod", side_effect=chmod_side_effect),
            caplog.at_level("WARNING", logger="gobby.cli.install_setup"),
        ):
            result = _install_ghook(force=True)

        assert result["installed"] is True
        assert result["method"] == "github"
        assert "failed writing install sidecar" in caplog.text
        assert (bin_dir / _GHOOK_INSTALL_SIDECAR).exists()

    def test_all_methods_fail(self, tmp_path: Path, _patch_platform: None) -> None:
        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_ghook_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._install_ghook_from_github", return_value=False),
            patch("gobby.cli.install_setup._install_ghook_from_cargo_binstall", return_value=False),
            patch("gobby.cli.install_setup._install_ghook_from_cargo_install", return_value=False),
        ):
            result = _install_ghook()

        assert result["installed"] is False
        assert "all installation methods failed" in result["reason"]

    def test_unsupported_platform(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.sys.platform", "freebsd"),
            patch("gobby.cli.install_setup.platform.machine", return_value="mips"),
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
        ):
            result = _install_ghook()

        assert result["skipped"] is True
        assert "unsupported platform" in result["reason"]
