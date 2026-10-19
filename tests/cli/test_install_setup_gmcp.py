"""gmcp installer builds gobby-mcp from the checkout and records its daemon start command."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from gobby.cli import install_setup_gmcp
from gobby.cli.install_setup_gterm import ManagedBinaryReleaseMissing
from gobby.install.bin_freshness_promotion import file_sha256

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

    install_setup_gmcp.write_gmcp_version_stamp(module, bin_dir, GMCP_PIN)

    assert (bin_dir / ".gmcp-version").read_text(encoding="utf-8") == f"{GMCP_PIN}\n"


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
    module, _bin_dir, _source = _gmcp_install_harness(
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


def test_daemon_start_command_is_replaced_whole(tmp_path: Path) -> None:
    install_setup_gmcp.write_daemon_start_command(
        tmp_path, ["python", "-m", "gobby.cli", "start", "--verbose", "--stale"]
    )

    install_setup_gmcp.write_daemon_start_command(tmp_path, START_ARGV)

    start = tmp_path / "gmcp-daemon-start.json"
    assert json.loads(start.read_text(encoding="utf-8")) == START_ARGV
    assert stat.S_IMODE(start.stat().st_mode) == 0o644
    assert sorted(path.name for path in tmp_path.iterdir()) == ["gmcp-daemon-start.json"]
