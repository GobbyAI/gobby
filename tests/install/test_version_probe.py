"""Tests for the shared native-binary version probe."""

from __future__ import annotations

import logging
import subprocess
from unittest.mock import MagicMock

import pytest

from gobby.install.version_probe import probe_native_bin_version

pytestmark = pytest.mark.unit


def _runner(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    runner = MagicMock(return_value=MagicMock(returncode=returncode, stdout=stdout, stderr=stderr))
    return runner


def test_returns_last_stdout_token() -> None:
    runner = _runner(stdout="gcode 0.9.9\n")
    assert probe_native_bin_version("/bin/gcode", runner=runner) == "0.9.9"
    # Probes always invoke `<binary> --version`.
    assert runner.call_args[0][0] == ["/bin/gcode", "--version"]


def test_falls_back_to_stderr_when_stdout_empty() -> None:
    runner = _runner(stdout="", stderr="gwiki 0.2.0\n")
    assert probe_native_bin_version("/bin/gwiki", runner=runner) == "0.2.0"


def test_non_zero_exit_returns_none() -> None:
    runner = _runner(returncode=1, stdout="0.1.0\n")
    assert probe_native_bin_version("/bin/ghook", runner=runner) is None


def test_empty_output_returns_none() -> None:
    runner = _runner(stdout="   \n")
    assert probe_native_bin_version("/bin/gsqz", runner=runner) is None


@pytest.mark.parametrize("error", [OSError("boom"), subprocess.SubprocessError("boom")])
def test_runner_errors_return_none(error: Exception) -> None:
    runner = MagicMock(side_effect=error)
    assert probe_native_bin_version("/bin/gloc", runner=runner) is None


def test_logs_failures_when_logger_and_label_supplied(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.version_probe")
    runner = MagicMock(side_effect=OSError("no such file"))
    with caplog.at_level(logging.WARNING, logger="test.version_probe"):
        assert probe_native_bin_version("/bin/ghook", runner=runner, logger=logger, label="ghook") is None
    assert any("ghook" in record.message for record in caplog.records)
