from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.runner_pid_file import ProbeState, SingletonProbe
from gobby.storage.schema_contract import expected_schema_identity

pytestmark = pytest.mark.unit


def _write_member(binary: Path, identity: dict[str, int | str]) -> None:
    payload = json.dumps(identity, sort_keys=True)
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{payload}'\n", encoding="utf-8")
    binary.chmod(0o755)


def _mixed_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, int | str], dict[str, int | str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pinned = expected_schema_identity()
    mixed = dict(pinned)
    mixed["latest_version"] = int(pinned["latest_version"]) + 1
    mixed["latest_checksum"] = "f" * 64

    (bin_dir / ".gdaemon-schema-identity.json").write_text(json.dumps(pinned), encoding="utf-8")
    for member in ("gcode", "gdaemon", "ghook", "gwiki"):
        _write_member(bin_dir / member, mixed if member == "ghook" else pinned)
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(bin_dir))
    return pinned, mixed


def test_status_reports_mixed_installed_binary_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned, mixed = _mixed_install(tmp_path, monkeypatch)
    monkeypatch.setattr("gobby.cli.daemon.unsupported_platform_error", lambda: None)
    monkeypatch.setattr("gobby.cli.daemon.get_gobby_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        "gobby.cli.daemon.probe_daemon_lock",
        lambda _path: SingletonProbe(state=ProbeState.ABSENT),
    )

    result = CliRunner().invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "mixed installed binary set" in result.output
    assert "ghook" in result.output
    assert f"v{mixed['latest_version']}" in result.output
    assert f"v{pinned['latest_version']}" in result.output


def test_start_refuses_mixed_installed_binary_set_before_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mixed_install(tmp_path, monkeypatch)
    dependencies = Mock(side_effect=AssertionError("dependency checks must not run"))
    monkeypatch.setattr("gobby.cli.daemon.worktree_daemon_refusal", lambda: None)
    monkeypatch.setattr("gobby.cli.daemon._start_dependency_errors", dependencies)

    result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code == 1, result.output
    assert "Refusing to start" in result.output
    assert "mixed installed binary set" in result.output
    assert "ghook" in result.output
    dependencies.assert_not_called()


def test_restart_refuses_mixed_installed_binary_set_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mixed_install(tmp_path, monkeypatch)
    stop = Mock(side_effect=AssertionError("stop must not run"))
    monkeypatch.setattr("gobby.cli.daemon.worktree_daemon_refusal", lambda: None)
    monkeypatch.setattr("gobby.cli.daemon._do_stop", stop)

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 1, result.output
    assert "Refusing to restart" in result.output
    assert "mixed installed binary set" in result.output
    assert "ghook" in result.output
    stop.assert_not_called()
