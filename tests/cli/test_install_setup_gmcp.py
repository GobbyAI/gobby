"""gmcp installer builds gobby-mcp from the checkout and records its daemon start command."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, call

import pytest

from gobby.cli import install_setup_gmcp
from gobby.cli.install_setup_gterm import ManagedBinaryReleaseMissing
from gobby.install import version_pins
from gobby.install.bin_freshness_promotion import file_sha256
from gobby.install.bin_release_identity import BinaryReleaseVersionError

pytestmark = pytest.mark.unit

GMCP_PIN = "0.1.1"
REPO_ROOT = Path(install_setup_gmcp.__file__).resolve().parents[3]
START_ARGV = [sys.executable, "-m", "gobby.cli", "start"]


def _gmcp_module(run: MagicMock, *, cargo: str | None = "/usr/bin/cargo") -> SimpleNamespace:
    return SimpleNamespace(
        shutil=SimpleNamespace(which=lambda _name: cargo),
        subprocess=SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
        click=SimpleNamespace(echo=MagicMock()),
        logger=MagicMock(),
        tempfile=tempfile,
        os=os,
        _GMCP_BIN_NAME="gmcp",
    )


def test_submodule_install_promotes_and_stamps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    release = tmp_path / "target" / "release"
    release.mkdir(parents=True)
    built = release / "gmcp"
    built.write_bytes(b"built-gmcp")
    run = MagicMock(name="cargo_build", return_value=SimpleNamespace(returncode=0))
    module = _gmcp_module(run)
    lock = MagicMock(name="native_bin_lock")
    acquire = MagicMock(name="try_acquire_native_bin_lock", return_value=lock)
    promote = MagicMock(name="stage_and_promote_binary_file")
    write_hash = MagicMock(name="write_source_hash")
    monkeypatch.setattr(install_setup_gmcp, "cargo_release_dir", lambda _root: release)
    monkeypatch.setattr(install_setup_gmcp, "workspace_binary_is_current", lambda *_a, **_k: False)
    monkeypatch.setattr(install_setup_gmcp, "try_acquire_native_bin_lock", acquire)
    monkeypatch.setattr(install_setup_gmcp, "stage_and_promote_binary_file", promote)
    monkeypatch.setattr(install_setup_gmcp, "write_source_hash", write_hash)

    outcome = install_setup_gmcp.install_gmcp_from_submodule(module, bin_dir)

    assert outcome == "promoted"
    run.assert_called_once()
    assert run.call_args.args[0] == [
        "cargo",
        "build",
        "--release",
        "-p",
        "gobby-mcp",
        "--manifest-path",
        str(REPO_ROOT / "Cargo.toml"),
    ]
    acquire.assert_called_once_with("gmcp", bin_dir=bin_dir)
    lock.__enter__.assert_called_once()
    promote.assert_called_once_with(
        built,
        destination=bin_dir / "gmcp",
        prepare_staged=ANY,
        workspace_root=REPO_ROOT,
    )
    write_hash.assert_called_once_with(bin_dir, "gmcp", file_sha256(built))


def _gmcp_install_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    present: bool,
    source_outcome: str | None,
) -> tuple[SimpleNamespace, Path, MagicMock]:
    home = tmp_path / ".gobby"
    bin_dir = home / "bin"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    if present:
        bin_dir.mkdir(parents=True)
        (bin_dir / "gmcp").write_bytes(b"existing")

    def build(destination: Path) -> str | None:
        if source_outcome == "promoted":
            (destination / "gmcp").write_bytes(b"workspace")
        return source_outcome

    source = MagicMock(name="source", side_effect=build)
    module = SimpleNamespace(
        logger=MagicMock(),
        _GMCP_BIN_NAME="gmcp",
        _get_installed_gmcp_version=MagicMock(return_value=None),
        _install_gmcp_from_submodule=source,
        _write_gmcp_version_stamp=MagicMock(name="stamp"),
        _ensure_gobby_bin_on_path=MagicMock(return_value={}),
    )
    monkeypatch.setattr(
        install_setup_gmcp,
        "probe_gmcp_version",
        lambda _module, _path: GMCP_PIN,
    )
    return module, bin_dir, source


def test_unpublished_absent_binary_without_checkout_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _bin_dir, source = _gmcp_install_harness(
        monkeypatch, tmp_path, present=False, source_outcome=None
    )

    with pytest.raises(ManagedBinaryReleaseMissing, match="gmcp"):
        install_setup_gmcp.install_gmcp(module)

    source.assert_called_once()


def test_promotion_records_daemon_start_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, _source = _gmcp_install_harness(
        monkeypatch, tmp_path, present=False, source_outcome="promoted"
    )
    start = tmp_path / ".gobby" / "gmcp-daemon-start.json"
    start.parent.mkdir(parents=True, exist_ok=True)
    start.write_text('["stale", "entries", "from", "an", "earlier", "promotion"]')
    start.chmod(0o600)

    result = install_setup_gmcp.install_gmcp(module)

    assert result["method"] == "workspace"
    assert json.loads(start.read_text(encoding="utf-8")) == START_ARGV
    assert stat.S_IMODE(start.stat().st_mode) == 0o644
    module._write_gmcp_version_stamp.assert_called_once_with(bin_dir, GMCP_PIN)
    module._ensure_gobby_bin_on_path.assert_called_once_with(bin_dir)


def test_daemon_start_command_is_replaced_whole(tmp_path: Path) -> None:
    install_setup_gmcp.write_daemon_start_command(
        tmp_path, ["python", "-m", "gobby.cli", "start", "--verbose", "--stale"]
    )

    install_setup_gmcp.write_daemon_start_command(tmp_path, START_ARGV)

    start = tmp_path / "gmcp-daemon-start.json"
    assert json.loads(start.read_text(encoding="utf-8")) == START_ARGV
    assert stat.S_IMODE(start.stat().st_mode) == 0o644
    assert sorted(path.name for path in tmp_path.iterdir()) == ["gmcp-daemon-start.json"]


def _gmcp_fetch_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    present: bool,
    installed_version: str | None,
    successful_fetch: str,
) -> tuple[SimpleNamespace, Path, MagicMock]:
    home = tmp_path / ".gobby"
    bin_dir = home / "bin"
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(version_pins, "UNPUBLISHED_MANAGED_BINS", frozenset({"gterm", "gclient"}))
    if present:
        bin_dir.mkdir(parents=True)
        (bin_dir / "gmcp").write_bytes(b"existing")

    def fetch_result(method: str) -> Callable[..., bool]:
        def run(*_args: object) -> bool:
            if method == successful_fetch:
                (bin_dir / "gmcp").write_bytes(method.encode())
                return True
            return False

        return run

    fetches = MagicMock(name="fetches")
    github = MagicMock(name="github", side_effect=fetch_result("github"))
    binstall = MagicMock(name="binstall", side_effect=fetch_result("binstall"))
    cargo_install = MagicMock(name="cargo_install", side_effect=fetch_result("cargo_install"))
    cargo_git = MagicMock(name="cargo_git", side_effect=fetch_result("cargo_git"))
    fetches.attach_mock(github, "github")
    fetches.attach_mock(binstall, "binstall")
    fetches.attach_mock(cargo_install, "cargo_install")
    fetches.attach_mock(cargo_git, "cargo_git")
    module = SimpleNamespace(
        sys=SimpleNamespace(platform="darwin"),
        platform=SimpleNamespace(machine=lambda: "arm64"),
        logger=MagicMock(),
        _GMCP_BIN_NAME="gmcp",
        _GMCP_TARGETS={("darwin", "arm64"): "aarch64-apple-darwin"},
        _get_installed_gmcp_version=MagicMock(return_value=installed_version),
        _write_gmcp_version_stamp=MagicMock(name="stamp"),
        _install_gmcp_from_submodule=MagicMock(name="source", return_value=None),
        _install_gmcp_from_github=github,
        _install_gmcp_from_cargo_binstall=binstall,
        _install_gmcp_from_cargo_install=cargo_install,
        _install_gmcp_from_cargo_git=cargo_git,
        _ensure_gobby_bin_on_path=MagicMock(return_value={}),
    )
    monkeypatch.setattr(
        install_setup_gmcp,
        "probe_gmcp_version",
        lambda _module, _path: GMCP_PIN,
    )
    return module, bin_dir, fetches


def test_published_binary_runs_fetch_chain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module, bin_dir, fetches = _gmcp_fetch_harness(
        monkeypatch,
        tmp_path,
        present=True,
        installed_version="0.0.1",
        successful_fetch="cargo_git",
    )

    result = install_setup_gmcp.install_gmcp(module)

    assert fetches.mock_calls == [
        call.github(bin_dir, "aarch64-apple-darwin", GMCP_PIN),
        call.binstall(bin_dir, GMCP_PIN),
        call.cargo_install(bin_dir, GMCP_PIN),
        call.cargo_git(bin_dir),
    ]
    assert result["method"] == "cargo-git"


def test_published_absent_binary_runs_fetch_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, bin_dir, fetches = _gmcp_fetch_harness(
        monkeypatch,
        tmp_path,
        present=False,
        installed_version=None,
        successful_fetch="cargo_install",
    )

    result = install_setup_gmcp.install_gmcp(module)

    assert fetches.mock_calls == [
        call.github(bin_dir, "aarch64-apple-darwin", GMCP_PIN),
        call.binstall(bin_dir, GMCP_PIN),
        call.cargo_install(bin_dir, GMCP_PIN),
    ]
    assert result["method"] == "cargo-install"


def test_release_refusal_is_reported_instead_of_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _bin_dir, _source = _gmcp_install_harness(
        monkeypatch, tmp_path, present=True, source_outcome=None
    )
    module._install_gmcp_from_submodule = MagicMock(
        name="source",
        side_effect=BinaryReleaseVersionError("gmcp: changed or unverified source"),
    )

    result = install_setup_gmcp.install_gmcp(module)

    assert result == {
        "installed": False,
        "skipped": False,
        "reason": "gmcp: changed or unverified source",
    }
