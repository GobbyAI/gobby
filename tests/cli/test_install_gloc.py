"""Tests for gloc binary installer support in install_setup.py."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.install_setup import (
    _GLOC_BIN_NAME,
    _GLOC_VERSION_STAMP,
    _get_installed_gloc_version,
    _get_latest_gloc_version,
    _install_gloc,
    _install_gloc_from_cargo_binstall,
    _install_gloc_from_cargo_install,
    _install_gloc_from_github,
    _probe_gloc_version,
    _write_gloc_version_stamp,
)
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit


def _make_tarball(bin_name: str = "gloc") -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho fake-gloc\n"
        info = tarfile.TarInfo(name=bin_name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def _make_zip(bin_name: str = "gloc.exe") -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as archive:
        archive.writestr(bin_name, b"fake-gloc")
    buf.seek(0)
    return buf


class TestGetLatestGlocVersion:
    def test_success(self) -> None:
        payload = json.dumps({"crate": {"max_version": "0.1.1"}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp):
            assert _get_latest_gloc_version() == "0.1.1"

    def test_network_error(self) -> None:
        from urllib.error import URLError

        with patch("gobby.cli.install_setup.urlopen", side_effect=URLError("timeout")):
            assert _get_latest_gloc_version() is None


class TestGetInstalledGlocVersion:
    def test_stamp_exists(self, tmp_path: Path) -> None:
        (tmp_path / _GLOC_VERSION_STAMP).write_text("0.1.0\n")
        assert _get_installed_gloc_version(tmp_path) == "0.1.0"

    def test_binary_exists_no_stamp(self, tmp_path: Path) -> None:
        (tmp_path / _GLOC_BIN_NAME).write_bytes(b"\x00")
        assert _get_installed_gloc_version(tmp_path) == "unknown"

    def test_no_binary_no_stamp(self, tmp_path: Path) -> None:
        assert _get_installed_gloc_version(tmp_path) is None


class TestWriteGlocVersionStamp:
    def test_writes_version(self, tmp_path: Path) -> None:
        _write_gloc_version_stamp(tmp_path, "0.1.0")
        assert (tmp_path / _GLOC_VERSION_STAMP).read_text().strip() == "0.1.0"


class TestInstallGlocFromGithub:
    def test_success(self, tmp_path: Path) -> None:
        tarball = _make_tarball("gloc")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag", return_value="gloc-v0.1.1"
            ),
        ):
            assert _install_gloc_from_github(tmp_path, "aarch64-apple-darwin") is True
        assert (tmp_path / "gloc").exists()

    def test_versioned_url_uses_release_tag_prefix(self, tmp_path: Path) -> None:
        tarball = _make_tarball("gloc")
        mock_resp = MagicMock()
        mock_resp.read.return_value = tarball.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("gobby.cli.install_setup.urlopen", return_value=mock_resp) as mock_urlopen:
            assert _install_gloc_from_github(tmp_path, "aarch64-apple-darwin", "0.1.1") is True
        url_called = mock_urlopen.call_args[0][0]
        if hasattr(url_called, "full_url"):
            url_called = url_called.full_url
        assert "gloc-v0.1.1" in url_called

    def test_windows_zip_asset(self, tmp_path: Path) -> None:
        archive = _make_zip("gloc.exe")
        mock_resp = MagicMock()
        mock_resp.read.return_value = archive.read()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("gobby.cli.install_setup.urlopen", return_value=mock_resp),
            patch(
                "gobby.cli.install_setup._resolve_latest_release_tag", return_value="gloc-v0.1.1"
            ),
            patch("gobby.cli.install_setup._GLOC_BIN_NAME", "gloc.exe"),
        ):
            assert _install_gloc_from_github(tmp_path, "x86_64-pc-windows-msvc") is True
        assert (tmp_path / "gloc.exe").exists()


class TestInstallGlocFromCargo:
    def test_binstall_success(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert _install_gloc_from_cargo_binstall(tmp_path) is True
            cmd = mock_run.call_args[0][0]
            assert "gobby-local" in cmd

    def test_binstall_with_version(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo-binstall"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _install_gloc_from_cargo_binstall(tmp_path, "0.1.1")
            cmd = mock_run.call_args[0][0]
            assert "gobby-local@0.1.1" in cmd

    def test_install_success(self, tmp_path: Path) -> None:
        with (
            patch("gobby.cli.install_setup.shutil.which", return_value="/usr/bin/cargo"),
            patch("gobby.cli.install_setup.subprocess.run") as mock_run,
            patch("gobby.cli.install_setup.click"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert _install_gloc_from_cargo_install(tmp_path) is True
            cmd = mock_run.call_args[0][0]
            assert "gobby-local" in cmd


class TestProbeGlocVersion:
    def test_returns_last_token(self, tmp_path: Path) -> None:
        with patch("gobby.cli.install_setup.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="gloc 0.1.1\n", stderr="")
            assert _probe_gloc_version(tmp_path / "gloc") == "0.1.1"


class TestInstallGloc:
    @pytest.fixture()
    def _patch_platform(self):
        with (
            patch("gobby.cli.install_setup.sys.platform", "darwin"),
            patch("gobby.cli.install_setup.platform.machine", return_value="arm64"),
        ):
            yield

    def test_fresh_install_github(self, tmp_path: Path, _patch_platform: None) -> None:
        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gloc_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._install_gloc_from_github", return_value=True),
            patch("gobby.cli.install_setup._probe_gloc_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._ensure_gobby_bin_on_path", return_value={}),
        ):
            bin_dir = tmp_path / ".gobby" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gloc").write_bytes(b"\x00")

            result = _install_gloc()

        assert result["installed"] is True
        assert result["method"] == "github"
        assert result["version"] == "0.1.1"

    def test_already_up_to_date(self, tmp_path: Path, _patch_platform: None) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "gloc").write_bytes(b"\x00")
        pinned_version = MANAGED_BIN_VERSION_PINS["gloc"]
        (bin_dir / _GLOC_VERSION_STAMP).write_text(f"{pinned_version}\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gloc_version", return_value=pinned_version),
        ):
            result = _install_gloc()

        assert result == {"installed": False, "skipped": True, "version": pinned_version}

    def test_newer_installed_version_skips_when_latest_is_lower(
        self, tmp_path: Path, _patch_platform: None
    ) -> None:
        bin_dir = tmp_path / ".gobby" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "gloc").write_bytes(b"\x00")
        (bin_dir / _GLOC_VERSION_STAMP).write_text("0.1.2\n")

        with (
            patch("gobby.cli.install_setup.Path.home", return_value=tmp_path),
            patch("gobby.cli.install_setup._get_latest_gloc_version", return_value="0.1.1"),
            patch("gobby.cli.install_setup._install_gloc_from_github") as mock_github,
        ):
            result = _install_gloc()

        assert result == {"installed": False, "skipped": True, "version": "0.1.2"}
        mock_github.assert_not_called()
