"""Binary-only install components promote gclient and gterm without the daemon lock."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.install_components import run_install_components
from gobby.cli.install_files_home import (
    _install_maintenance_block_message,
    local_install_requires_maintenance,
)
from gobby.cli.install_setup import (
    _install_gclient_from_submodule,
    _install_gterm_from_submodule,
)
from gobby.cli.runtime import CliRuntime
from gobby.install.bin_freshness_promotion import native_bin_predates_source
from gobby.runner_pid_file import ProbeState


def test_binary_components_promote_while_a_daemon_singleton_is_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    promoted: list[tuple[str, Path]] = []

    def install_gclient(bin_dir: Path) -> str:
        promoted.append(("gclient", bin_dir))
        (bin_dir / "gclient").write_bytes(b"gclient")
        return "gclient-sha"

    def install_gterm(bin_dir: Path) -> str:
        promoted.append(("gterm", bin_dir))
        (bin_dir / "gterm").write_bytes(b"gterm")
        return "gterm-sha"

    def claim_singleton() -> None:
        raise AssertionError("binary install claimed the daemon singleton")

    monkeypatch.setattr("gobby.cli.install_setup._install_gclient_from_submodule", install_gclient)
    monkeypatch.setattr("gobby.cli.install_setup._install_gterm_from_submodule", install_gterm)
    monkeypatch.setattr("gobby.cli.install_files_home.acquire_install_maintenance", claim_singleton)
    bin_dir = tmp_path / "bin"

    results = run_install_components(
        ("gclient", "gterm"),
        project_path=tmp_path,
        no_interactive=True,
        embedding=None,
        runtime=cast(CliRuntime, SimpleNamespace()),
        bin_dir=bin_dir,
    )

    assert results["gclient"]["success"] is True
    assert results["gterm"]["success"] is True
    assert promoted == [("gclient", bin_dir), ("gterm", bin_dir)]


def test_full_install_still_refuses_a_live_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.cli.install_files_home.probe_daemon_lock",
        lambda _path: SimpleNamespace(state=ProbeState.DAEMON),
    )
    monkeypatch.setattr(
        "gobby.cli.install_files_home.format_singleton_status",
        lambda _probe: "daemon pid 1",
    )

    message = _install_maintenance_block_message(Path("/tmp/gobby-pid"))

    assert local_install_requires_maintenance(datastore_mode="local", full_install=True)
    assert "Stop the daemon" in message
    assert "files_home or personal identity" in message


def test_a_rebuilt_gclient_clears_the_stale_marker(tmp_path: Path) -> None:
    binary = tmp_path / "gclient"
    binary.write_bytes(b"old")
    os.utime(binary, (1, 1))
    assert native_bin_predates_source("gclient", bin_dir=tmp_path) is True

    binary.write_bytes(b"new")
    now = time.time() + 5
    os.utime(binary, (now, now))
    assert native_bin_predates_source("gclient", bin_dir=tmp_path) is False


def _signature_text(path: Path) -> str:
    probe = subprocess.run(
        ["codesign", "-dv", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.stderr + probe.stdout


def _unsigned_executable(destination: Path) -> None:
    shutil.copy("/usr/bin/true", destination)
    removed = subprocess.run(
        ["codesign", "--remove-signature", str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert removed.returncode == 0, removed.stderr
    assert "Signature=adhoc" not in _signature_text(destination)


def _which_build_tools(name: str) -> str | None:
    if name in {"cargo", "zig", "codesign"}:
        return f"/usr/bin/{name}"
    return None


_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_REPLACE = os.replace


def _run_except_cargo(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Let codesign run; the submodule installer must not invoke cargo here."""
    if args and args[0] == "cargo":
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return _REAL_SUBPROCESS_RUN(args, **kwargs)


@pytest.mark.parametrize(
    ("binary_name", "crate_dir", "module_name", "install"),
    [
        (
            "gclient",
            "gclient",
            "gobby.cli.install_setup_gclient",
            _install_gclient_from_submodule,
        ),
        (
            "gterm",
            "gterminal",
            "gobby.cli.install_setup_gterm",
            _install_gterm_from_submodule,
        ),
    ],
)
def test_submodule_install_ad_hoc_signs_the_staged_binary(
    binary_name: str,
    crate_dir: str,
    module_name: str,
    install: Callable[[Path], str | None],
    tmp_path: Path,
) -> None:
    """The real submodule installer signs the staged inode before promotion."""
    if sys.platform != "darwin" or shutil.which("codesign") is None:
        pytest.skip("ad-hoc signing is enforced on macOS")

    workspace = tmp_path / "workspace"
    (workspace / "crates" / crate_dir).mkdir(parents=True)
    (workspace / "src" / "gobby" / "cli").mkdir(parents=True)
    (workspace / "Cargo.toml").touch()
    (workspace / "crates" / crate_dir / "Cargo.toml").touch()
    source = workspace / "target" / "release" / binary_name
    source.parent.mkdir(parents=True)
    _unsigned_executable(source)
    dest_dir = tmp_path / "bin"
    dest_dir.mkdir()

    def replace_staged(src: str, dst: str) -> None:
        staged = Path(src)
        if staged.name == binary_name:
            assert "Signature=adhoc" in _signature_text(staged)
        _REAL_REPLACE(src, dst)

    with (
        patch("gobby.cli.install_setup.shutil.which", side_effect=_which_build_tools),
        patch("gobby.cli.install_setup.subprocess.run", side_effect=_run_except_cargo),
        patch(
            f"{module_name}.__file__",
            str(workspace / "src" / "gobby" / "cli" / "installer.py"),
        ),
        patch(f"{module_name}.try_acquire_native_bin_lock", return_value=MagicMock()),
        patch("gobby.install.bin_freshness_promotion.os.replace", side_effect=replace_staged),
    ):
        result = install(dest_dir)

    assert result == "promoted"
    assert "Signature=adhoc" in _signature_text(dest_dir / binary_name)
