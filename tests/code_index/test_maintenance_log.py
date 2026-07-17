"""Tests for code-index maintenance log helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.code_index import maintenance_log
from gobby.code_index.gcode_gateway import GcodeCommandResult

pytestmark = pytest.mark.unit

MAX_LOG_FILE_BYTES = 5 * 1024 * 1024


@pytest.fixture(autouse=True)
def reset_maintenance_loggers() -> Iterator[None]:
    maintenance_log._LOGGERS.clear()
    yield
    for logger in maintenance_log._LOGGERS.values():
        if logger is maintenance_log._FALLBACK_LOGGER:
            continue
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
    maintenance_log._LOGGERS.clear()


def _result(*, stdout: str = "", stderr: str = "") -> GcodeCommandResult:
    return GcodeCommandResult(
        command=("gcode", "prune"),
        returncode=0,
        stdout=stdout,
        stderr=stderr,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        duration_seconds=1.0,
        timeout_seconds=60.0,
    )


def _write_event(log_file: Path, *, stdout: str = "", stderr: str = "") -> None:
    maintenance_log.log_gcode_maintenance_event(
        log_file=str(log_file),
        event="prune",
        run_id="run-1",
        project_id="project-1",
        root_path="/tmp/project",
        result=_result(stdout=stdout, stderr=stderr),
        status="success",
    )


def _stdout_for_emitted_size(tmp_path: Path, target_bytes: int) -> str:
    calibration_file = tmp_path / "calibration.log"
    _write_event(calibration_file)
    base_size = len(calibration_file.read_bytes())
    assert base_size <= target_bytes
    return "x" * (target_bytes - base_size)


def test_logger_memoizes_fallback_logger_after_handler_setup_failure(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    log_file = blocked_parent / "maintenance.log"
    expanded = str(log_file.expanduser())
    maintenance_log._LOGGERS.pop(expanded, None)

    try:
        with patch.object(maintenance_log._FALLBACK_LOGGER, "warning") as warning:
            first = maintenance_log._logger(str(log_file))
            second = maintenance_log._logger(str(log_file))

        assert first is maintenance_log._FALLBACK_LOGGER
        assert second is maintenance_log._FALLBACK_LOGGER
        warning.assert_called_once()
    finally:
        maintenance_log._LOGGERS.pop(expanded, None)


def test_log_persists_stream_larger_than_previous_clip(tmp_path: Path) -> None:
    log_file = tmp_path / "maintenance.log"
    stdout = "x" * 16_001

    _write_event(log_file, stdout=stdout)

    payload = json.loads(log_file.read_text(encoding="utf-8"))
    assert payload["stdout"] == stdout


@pytest.mark.parametrize("target_bytes", [MAX_LOG_FILE_BYTES - 1, MAX_LOG_FILE_BYTES])
def test_log_accepts_record_at_or_below_exact_emitted_byte_limit(
    tmp_path: Path,
    target_bytes: int,
) -> None:
    stdout = _stdout_for_emitted_size(tmp_path, target_bytes)
    log_file = tmp_path / f"boundary-{target_bytes}.log"

    _write_event(log_file, stdout=stdout)

    emitted = log_file.read_bytes()
    assert len(emitted) == target_bytes
    assert emitted.endswith(b"\n")
    assert json.loads(emitted)["stdout"] == stdout


def test_log_rejects_record_above_exact_emitted_byte_limit_without_writing(
    tmp_path: Path,
) -> None:
    target_bytes = MAX_LOG_FILE_BYTES + 1
    stdout = _stdout_for_emitted_size(tmp_path, target_bytes)
    log_file = tmp_path / "too-large.log"

    with pytest.raises(maintenance_log.MaintenanceLogRecordTooLargeError) as exc_info:
        _write_event(log_file, stdout=stdout)

    assert exc_info.value.size_bytes == target_bytes
    assert exc_info.value.max_bytes == MAX_LOG_FILE_BYTES
    assert not log_file.exists() or log_file.read_bytes() == b""
