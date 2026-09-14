from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.native_binary_selection import (
    NativeBinarySelectionError,
    install_native_binary,
    select_native_binaries,
    select_native_binary,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_native_bin_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOBBY_NATIVE_BIN_DIR", raising=False)


def _write_executable(path: Path, content: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def test_explicit_override_selects_absolute_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _write_executable(tmp_path / "relative-bin" / "gterm")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", "relative-bin")

    selected = select_native_binary("gterm", required=True)

    assert selected is not None
    assert selected.path == binary.resolve()
    assert selected.path.is_absolute()
    assert selected.source == "GOBBY_NATIVE_BIN_DIR"


def test_installed_directory_is_the_only_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = _write_executable(tmp_path / ".gobby" / "bin" / "gterm")
    stale_debug = _write_executable(tmp_path / "repo" / "target" / "debug" / "gterm")
    monkeypatch.setenv("PATH", f"{stale_debug.parent}{os.pathsep}{os.environ['PATH']}")

    with patch.object(Path, "home", return_value=tmp_path):
        selected = select_native_binary("gterm", required=True)

    assert selected is not None
    assert selected.path == installed.resolve()
    assert selected.source == "installed-default"


def test_stale_checkout_and_path_do_not_rescue_missing_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_debug = _write_executable(tmp_path / "repo" / "target" / "debug" / "gterm")
    _write_executable(tmp_path / "repo" / ".gobby-native-bin" / "gterm")
    monkeypatch.setenv("PATH", f"{stale_debug.parent}{os.pathsep}{os.environ['PATH']}")

    with patch.object(Path, "home", return_value=tmp_path):
        assert select_native_binary("gterm", required=False) is None
        with pytest.raises(NativeBinarySelectionError, match=r"~/.gobby/bin/gterm"):
            select_native_binary("gterm", required=True)


@pytest.mark.parametrize("value", ["", "missing", "not-a-directory"])
def test_invalid_explicit_directory_never_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    _write_executable(tmp_path / ".gobby" / "bin" / "gterm")
    if value == "not-a-directory":
        (tmp_path / value).write_text("not a directory")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", value)

    with patch.object(Path, "home", return_value=tmp_path):
        with pytest.raises(NativeBinarySelectionError, match="GOBBY_NATIVE_BIN_DIR"):
            select_native_binary("gterm", required=False)


def test_explicit_override_requires_every_requested_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_dir = tmp_path / "native"
    _write_executable(native_dir / "gterm")
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(native_dir))

    with pytest.raises(NativeBinarySelectionError, match="gclient"):
        select_native_binaries(("gterm", "gclient"), required=True)


@pytest.mark.parametrize("kind", ["directory", "non-executable", "symlink"])
def test_selected_binary_must_be_a_regular_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    native_dir = tmp_path / "native"
    native_dir.mkdir()
    candidate = native_dir / "gterm"
    if kind == "directory":
        candidate.mkdir()
    elif kind == "non-executable":
        candidate.write_text("not executable")
        candidate.chmod(0o644)
    else:
        target = _write_executable(native_dir / "real-gterm")
        candidate.symlink_to(target)
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(native_dir))

    with pytest.raises(NativeBinarySelectionError, match="regular executable"):
        select_native_binary("gterm", required=True)


def test_provenance_header_identifies_selected_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"terminal-binary\n"
    binary = _write_executable(tmp_path / "native" / "gterm", payload)
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(binary.parent))

    selected = select_native_binary("gterm", required=True)

    assert selected is not None
    stat_result = binary.stat()
    header = selected.header()
    assert "source=GOBBY_NATIVE_BIN_DIR" in header
    assert f"path={binary.resolve()}" in header
    assert f"sha256={hashlib.sha256(payload).hexdigest()}" in header
    assert f"device={stat_result.st_dev}" in header
    assert f"inode={stat_result.st_ino}" in header


def test_harness_install_replaces_binary_with_new_inode(tmp_path: Path) -> None:
    source = _write_executable(tmp_path / "build" / "gterm", b"new binary\n")
    destination = _write_executable(tmp_path / "installed" / "gterm", b"old binary\n")
    old_stat = destination.stat()

    installed = install_native_binary(source, destination)

    new_stat = destination.stat()
    assert installed.path == destination.resolve()
    assert installed.source == "harness-managed"
    assert destination.read_bytes() == source.read_bytes()
    assert os.access(destination, os.X_OK)
    assert (installed.device, installed.inode) == (new_stat.st_dev, new_stat.st_ino)
    assert (new_stat.st_dev, new_stat.st_ino) != (old_stat.st_dev, old_stat.st_ino)


def test_external_e2e_header_requires_binary_and_includes_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.e2e.conftest import terminal_native_binary_headers

    binary = _write_executable(tmp_path / ".gobby" / "bin" / "gterm", b"gterm e2e\n")
    with patch.object(Path, "home", return_value=tmp_path):
        headers = terminal_native_binary_headers(("tests/e2e/test_external_terminal_attach.py",))

    assert len(headers) == 1
    assert str(binary.resolve()) in headers[0]
    assert hashlib.sha256(binary.read_bytes()).hexdigest() in headers[0]

    monkeypatch.delenv("GOBBY_NATIVE_BIN_DIR", raising=False)
    with (
        patch.object(Path, "home", return_value=tmp_path / "missing-home"),
        pytest.raises(NativeBinarySelectionError),
    ):
        terminal_native_binary_headers(("tests/e2e/test_external_terminal_attach.py",))


def test_terminal_stack_headers_identify_gterm_and_gclient(tmp_path: Path) -> None:
    from tests.e2e.conftest import terminal_native_binary_headers

    gterm = _write_executable(tmp_path / ".gobby" / "bin" / "gterm", b"gterm\n")
    gclient = _write_executable(tmp_path / ".gobby" / "bin" / "gclient", b"gclient\n")

    with patch.object(Path, "home", return_value=tmp_path):
        headers = terminal_native_binary_headers(("tests/e2e/test_terminal_client_stack.py",))

    assert [header.split()[1] for header in headers] == ["name=gterm", "name=gclient"]
    assert str(gterm.resolve()) in headers[0]
    assert hashlib.sha256(gterm.read_bytes()).hexdigest() in headers[0]
    assert str(gclient.resolve()) in headers[1]
    assert hashlib.sha256(gclient.read_bytes()).hexdigest() in headers[1]
