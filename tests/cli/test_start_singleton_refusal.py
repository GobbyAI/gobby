"""`gobby start` refuses a live singleton holder without touching its claim.

These drive the real start command against a real singleton in a temporary home.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.runner_pid_file import (
    INHERITED_LOCK_FD_ENV,
    ProbeState,
    claim_pid_file,
    probe_daemon_lock,
    reserve_service_start,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.delenv(INHERITED_LOCK_FD_ENV, raising=False)
    monkeypatch.setattr("gobby.cli.daemon_start.worktree_daemon_refusal", lambda: None)
    monkeypatch.setattr("gobby.storage.schema_divergence.binary_set_apply_refusal", lambda: None)
    monkeypatch.setattr("gobby.cli.daemon_start._start_dependency_errors", lambda: [])
    with (
        patch("gobby.runner_pid_record.current_boot_id", return_value="boot:test"),
        patch("gobby.runner_pid_file.current_boot_id", return_value="boot:test"),
    ):
        yield tmp_path / "gobby.pid"


def _lock_record(pid_file: Path) -> bytes:
    return Path(f"{pid_file}.lock").read_bytes()


def test_start_refuses_a_live_daemon_and_leaves_its_claim_held(pid_file: Path) -> None:
    claim = claim_pid_file(pid_file, role="daemon")
    assert claim is not None
    record = _lock_record(pid_file)
    try:
        result = CliRunner().invoke(cli, ["start"])

        assert result.exit_code == 1
        assert f"Daemon already running (PID: {os.getpid()})" in result.output
        probe = probe_daemon_lock(pid_file)
        assert (probe.state, probe.pid) == (ProbeState.DAEMON, os.getpid())
        assert _lock_record(pid_file) == record
        assert claim_pid_file(pid_file, role="daemon") is None
    finally:
        claim.release()


def test_start_refuses_a_live_service_reservation_and_leaves_it_in_place(
    pid_file: Path,
) -> None:
    reserve_service_start(pid_file, backend="launchd")
    record = _lock_record(pid_file)

    result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code == 1
    assert "A service start reservation is already live" in result.output
    assert probe_daemon_lock(pid_file).state is ProbeState.LIVE_RESERVATION
    assert _lock_record(pid_file) == record
