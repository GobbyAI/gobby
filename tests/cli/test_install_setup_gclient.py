"""State-table tests for the gclient managed-binary installer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from gobby.cli import install_setup_gclient, install_setup_gterm
from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

pytestmark = pytest.mark.unit
GCLIENT_PIN = MANAGED_BIN_VERSION_PINS["gclient"]


def _gclient_install_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    published: bool | None,
    present: bool,
    installed_version: str | None,
    source_succeeds: bool = False,
    successful_fetch: str | None = None,
) -> tuple[SimpleNamespace, Path, MagicMock, MagicMock, MagicMock]:
    bin_dir = tmp_path / ".gobby" / "bin"
    if present:
        bin_dir.mkdir(parents=True)
        (bin_dir / "gclient").write_bytes(b"existing")

    source = MagicMock(name="source")

    def source_build(destination: Path) -> bool:
        if source_succeeds:
            (destination / "gclient").write_bytes(b"workspace")
        return source_succeeds

    source.side_effect = source_build
    fetches = MagicMock(name="fetches")

    def fetch_result(method: str) -> Callable[..., bool]:
        def run(*_args: object) -> bool:
            if method == successful_fetch:
                (bin_dir / "gclient").write_bytes(method.encode())
                return True
            return False

        return run

    github = MagicMock(name="github", side_effect=fetch_result("github"))
    binstall = MagicMock(name="binstall", side_effect=fetch_result("binstall"))
    cargo_install = MagicMock(name="cargo_install", side_effect=fetch_result("cargo_install"))
    cargo_git = MagicMock(name="cargo_git", side_effect=fetch_result("cargo_git"))
    fetches.attach_mock(github, "github")
    fetches.attach_mock(binstall, "binstall")
    fetches.attach_mock(cargo_install, "cargo_install")
    fetches.attach_mock(cargo_git, "cargo_git")
    stamp = MagicMock(name="stamp")
    module = SimpleNamespace(
        Path=SimpleNamespace(home=lambda: tmp_path),
        sys=SimpleNamespace(platform="darwin"),
        platform=SimpleNamespace(machine=lambda: "arm64"),
        logger=MagicMock(),
        _GCLIENT_BIN_NAME="gclient",
        _GCLIENT_TARGETS={("darwin", "arm64"): "aarch64-apple-darwin"},
        _get_installed_gclient_version=MagicMock(return_value=installed_version),
        _write_gclient_version_stamp=stamp,
        _install_gclient_from_submodule=source,
        _install_gclient_from_github=github,
        _install_gclient_from_cargo_binstall=binstall,
        _install_gclient_from_cargo_install=cargo_install,
        _install_gclient_from_cargo_git=cargo_git,
        _ensure_gobby_bin_on_path=MagicMock(return_value={}),
    )
    if published is not None:
        monkeypatch.setattr(install_setup_gclient, "is_published", lambda _name: published)
    monkeypatch.setattr(
        install_setup_gclient,
        "probe_gclient_version",
        lambda _module, _path: GCLIENT_PIN,
    )
    return module, bin_dir, source, fetches, stamp


def test_unpublished_absent_binary_builds_or_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, source, _, stamp = _gclient_install_harness(
        monkeypatch,
        tmp_path / "build",
        published=None,
        present=False,
        installed_version=None,
        source_succeeds=True,
    )

    result = install_setup_gclient.install_gclient(module)

    source.assert_called_once_with(bin_dir)
    stamp.assert_called_once_with(bin_dir, GCLIENT_PIN)
    assert result["method"] == "workspace"

    missing_module, _, _, missing_fetches, _ = _gclient_install_harness(
        monkeypatch,
        tmp_path / "missing",
        published=None,
        present=False,
        installed_version=None,
    )
    error_type = install_setup_gterm.ManagedBinaryReleaseMissing
    with pytest.raises(error_type, match="gclient"):
        install_setup_gclient.install_gclient(missing_module)
    assert missing_fetches.mock_calls == []


def test_unpublished_present_binary_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, source, fetches, stamp = _gclient_install_harness(
        monkeypatch,
        tmp_path,
        published=None,
        present=True,
        installed_version="0.0.1",
    )

    result = install_setup_gclient.install_gclient(module)

    stamp.assert_called_once_with(bin_dir, "0.0.1")
    source.assert_not_called()
    assert fetches.mock_calls == []
    assert result == {
        "installed": False,
        "skipped": True,
        "version": "0.0.1",
        "method": "local",
    }


def test_published_satisfying_binary_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, source, fetches, stamp = _gclient_install_harness(
        monkeypatch,
        tmp_path,
        published=True,
        present=True,
        installed_version=GCLIENT_PIN,
    )

    result = install_setup_gclient.install_gclient(module)

    stamp.assert_called_once_with(bin_dir, GCLIENT_PIN)
    source.assert_not_called()
    assert fetches.mock_calls == []
    assert result["installed"] is False
    assert result["version"] == GCLIENT_PIN


def test_published_binary_runs_fetch_chain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module, bin_dir, source, fetches, _ = _gclient_install_harness(
        monkeypatch,
        tmp_path,
        published=True,
        present=True,
        installed_version="0.0.1",
        successful_fetch="cargo_git",
    )

    result = install_setup_gclient.install_gclient(module)

    source.assert_not_called()
    assert fetches.mock_calls == [
        call.github(bin_dir, "aarch64-apple-darwin", GCLIENT_PIN),
        call.binstall(bin_dir, GCLIENT_PIN),
        call.cargo_install(bin_dir, GCLIENT_PIN),
        call.cargo_git(bin_dir),
    ]
    assert result["method"] == "cargo-git"


def test_published_absent_binary_runs_fetch_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, source, fetches, _ = _gclient_install_harness(
        monkeypatch,
        tmp_path,
        published=True,
        present=False,
        installed_version=None,
        successful_fetch="cargo_install",
    )

    result = install_setup_gclient.install_gclient(module)

    source.assert_not_called()
    assert fetches.mock_calls == [
        call.github(bin_dir, "aarch64-apple-darwin", GCLIENT_PIN),
        call.binstall(bin_dir, GCLIENT_PIN),
        call.cargo_install(bin_dir, GCLIENT_PIN),
    ]
    assert result["method"] == "cargo-install"


def test_force_rebuilds_unpublished_present_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, source, fetches, _ = _gclient_install_harness(
        monkeypatch,
        tmp_path / "build",
        published=None,
        present=True,
        installed_version=GCLIENT_PIN,
        source_succeeds=True,
    )

    result = install_setup_gclient.install_gclient(module, force=True)

    source.assert_called_once_with(bin_dir)
    assert fetches.mock_calls == []
    assert result["installed"] is True
    assert result["method"] == "workspace"

    missing_module, _, _, _, _ = _gclient_install_harness(
        monkeypatch,
        tmp_path / "missing",
        published=None,
        present=True,
        installed_version=GCLIENT_PIN,
    )
    error_type = install_setup_gterm.ManagedBinaryReleaseMissing
    with pytest.raises(error_type, match="gclient"):
        install_setup_gclient.install_gclient(missing_module, force=True)


def test_force_refetches_published_satisfying_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, source, fetches, _ = _gclient_install_harness(
        monkeypatch,
        tmp_path,
        published=True,
        present=True,
        installed_version=GCLIENT_PIN,
        successful_fetch="github",
    )

    result = install_setup_gclient.install_gclient(module, force=True)

    source.assert_not_called()
    assert fetches.mock_calls == [call.github(bin_dir, "aarch64-apple-darwin", GCLIENT_PIN)]
    assert result["installed"] is True
    assert result["method"] == "github"
