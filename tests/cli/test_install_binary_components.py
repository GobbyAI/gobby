"""Binary-only install components promote gclient and gterm without the daemon lock."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from gobby.cli.install_components import run_install_components
from gobby.cli.install_files_home import (
    _install_maintenance_block_message,
    local_install_requires_maintenance,
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
