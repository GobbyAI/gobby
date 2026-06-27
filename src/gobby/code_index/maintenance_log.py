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
_MAX_STREAM_CHARS = 4000


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
    """Write one bounded JSONL maintenance event to the rotating log."""
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
        "stdout": _bound_stream(result.stdout),
        "stderr": _bound_stream(result.stderr),
    }
    if detail:
        payload["detail"] = detail
    _logger(log_file).info(json.dumps(payload, sort_keys=True))


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
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError as exc:
        _FALLBACK_LOGGER.warning(
            "Failed to initialize code-index maintenance log file %s: %s",
            expanded,
            exc,
        )
        return _FALLBACK_LOGGER

    logger = logging.getLogger(f"gobby.code_index.maintenance_file.{expanded}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _LOGGERS[expanded] = logger
    return logger


def _bound_stream(value: str) -> str:
    if len(value) <= _MAX_STREAM_CHARS:
        return value
    omitted = len(value) - _MAX_STREAM_CHARS
    return f"{value[:_MAX_STREAM_CHARS]}...<truncated {omitted} chars>"
