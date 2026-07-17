"""Dedicated rotating log for code-index maintenance jobs."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from gobby.code_index.gcode_gateway import GcodeCommandResult

_LOGGERS: dict[str, logging.Logger] = {}
_FALLBACK_LOGGER = logging.getLogger(__name__)
_MAX_LOG_FILE_BYTES = 5 * 1024 * 1024
_RECORD_TERMINATOR = "\n"


class MaintenanceLogRecordTooLargeError(RuntimeError):
    """Raised when one maintenance-log record exceeds the file-size limit."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"Maintenance log record is too large "
            f"({size_bytes} emitted UTF-8 bytes exceeds {max_bytes} byte limit)"
        )


def log_gcode_maintenance_event(
    *,
    log_file: str,
    event: str,
    run_id: str,
    project_id: str | None,
    root_path: str | None,
    result: GcodeCommandResult,
    status: str,
    detail: str | None = None,
) -> None:
    """Write one size-checked JSONL maintenance event to the rotating log."""
    payload: dict[str, Any] = {
        "event": event,
        "run_id": run_id,
        "project_id": project_id,
        "root_path": root_path,
        "command": list(result.command),
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "duration_seconds": round(result.duration_seconds, 6),
        "exit_status": result.returncode,
        "timed_out": result.timed_out,
        "timeout_seconds": result.timeout_seconds,
        "status": status,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if detail:
        payload["detail"] = detail
    serialized = json.dumps(payload, sort_keys=True)
    record_size = len(f"{serialized}{_RECORD_TERMINATOR}".encode())
    if record_size > _MAX_LOG_FILE_BYTES:
        raise MaintenanceLogRecordTooLargeError(record_size, _MAX_LOG_FILE_BYTES)
    _logger(log_file).info(serialized)


def _logger(log_file: str) -> logging.Logger:
    expanded = str(Path(log_file).expanduser())
    existing = _LOGGERS.get(expanded)
    if existing is not None:
        return existing

    try:
        path = Path(expanded)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=_MAX_LOG_FILE_BYTES,
            backupCount=5,
            encoding="utf-8",
        )
        handler.terminator = _RECORD_TERMINATOR
    except OSError as exc:
        _FALLBACK_LOGGER.warning(
            "Failed to initialize code-index maintenance log file %s: %s",
            expanded,
            exc,
        )
        _LOGGERS[expanded] = _FALLBACK_LOGGER
        return _FALLBACK_LOGGER

    logger = logging.getLogger(f"gobby.code_index.maintenance_file.{expanded}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _LOGGERS[expanded] = logger
    return logger
