"""Tests for the shared native-binary version probe."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.install.version_probe import probe_native_bin_version

pytestmark = pytest.mark.unit


def _binary(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def _runner(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    runner = MagicMock(return_value=MagicMock(returncode=returncode, stdout=stdout, stderr=stderr))
    return runner


def test_returns_last_stdout_token(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "gcode")
    runner = _runner(stdout="gcode 0.9.9\n")
    assert probe_native_bin_version(binary, runner=runner) == "0.9.9"
    # Probes always invoke `<binary> --version`.
    assert runner.call_args[0][0] == [str(binary.resolve()), "--version"]


def test_falls_back_to_stderr_when_stdout_empty(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "gwiki")
    runner = _runner(stdout="", stderr="gwiki 0.2.0\n")
    assert probe_native_bin_version(binary, runner=runner) == "0.2.0"


def test_falls_back_to_stderr_when_stdout_is_whitespace(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "gwiki")
    runner = _runner(stdout=" \n", stderr="gwiki 0.2.0\n")
    assert probe_native_bin_version(binary, runner=runner) == "0.2.0"


def test_non_zero_exit_returns_none(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "ghook")
    runner = _runner(returncode=1, stdout="0.1.0\n")
    assert probe_native_bin_version(binary, runner=runner) is None


def test_empty_output_returns_none(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "gsqz")
    runner = _runner(stdout="   \n")
    assert probe_native_bin_version(binary, runner=runner) is None


@pytest.mark.parametrize("error", [OSError("boom"), subprocess.SubprocessError("boom")])
def test_runner_errors_return_none(tmp_path: Path, error: Exception) -> None:
    binary = _binary(tmp_path, "gloc")
    runner = MagicMock(side_effect=error)
    assert probe_native_bin_version(binary, runner=runner) is None


def test_missing_binary_path_returns_none_without_running(tmp_path: Path) -> None:
    runner = _runner(stdout="gcode 0.9.9\n")

    assert probe_native_bin_version(tmp_path / "missing-gcode", runner=runner) is None
    runner.assert_not_called()


def test_directory_binary_path_returns_none_without_running(tmp_path: Path) -> None:
    runner = _runner(stdout="gcode 0.9.9\n")

    assert probe_native_bin_version(tmp_path, runner=runner) is None
    runner.assert_not_called()


def test_logs_failures_when_logger_and_label_supplied(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    binary = _binary(tmp_path, "ghook")
    logger = logging.getLogger("test.version_probe")
    runner = MagicMock(side_effect=OSError("no such file"))
    with caplog.at_level(logging.WARNING, logger="test.version_probe"):
        assert probe_native_bin_version(binary, runner=runner, logger=logger, label="ghook") is None
    assert any("ghook" in record.message for record in caplog.records)
