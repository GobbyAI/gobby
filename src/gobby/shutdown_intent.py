"""Shutdown intent marker helpers."""

from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)


class ShutdownIntent(StrEnum):
    """Semantic daemon shutdown intent."""

    STOP = "stop"
    RESTART = "restart"

    @property
    def preserve_agents(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class ShutdownIntentRecord:
    """Parsed shutdown marker state."""

    intent: ShutdownIntent
    source: str
    sender_pid: int | None
    timestamp: float | None
    stale: bool = False
    error: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def preserve_agents(self) -> bool:
        return self.intent.preserve_agents


def coerce_shutdown_intent(value: str | ShutdownIntent | None) -> ShutdownIntent:
    """Coerce raw marker values to a shutdown intent."""
    if isinstance(value, ShutdownIntent):
        return value
    if value == ShutdownIntent.RESTART.value:
        return ShutdownIntent.RESTART
    return ShutdownIntent.STOP


def get_shutdown_marker_path(home: Path | None = None) -> Path:
    """Return the shutdown marker path."""
    if home is None:
        home = get_gobby_home()
    return home / "shutdown_intent_active.json"


def get_shutdown_source_path(home: Path | None = None) -> Path:
    """Return the persistent last-shutdown source path."""
    if home is None:
        home = get_gobby_home()
    return home / "shutdown_source.json"


get_active_shutdown_marker_path = get_shutdown_marker_path


def write_shutdown_intent(
    source: str,
    intent: str | ShutdownIntent,
    sender_pid: int | None = None,
    *,
    home: Path | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    """Write a fresh shutdown marker with explicit intent and source."""
    data: dict[str, object] = {
        "source": source,
        "intent": coerce_shutdown_intent(intent).value,
        "sender_pid": sender_pid or os.getpid(),
        "timestamp": time.time(),
    }
    if details:
        data["details"] = dict(details)
    for marker in (get_shutdown_source_path(home), get_shutdown_marker_path(home)):
        try:
            _write_marker_atomically(marker, data)
        except OSError:
            logger.exception("Failed to write shutdown intent markers")
            raise


def clear_active_shutdown_intent(*, home: Path | None = None) -> None:
    """Clear the active shutdown marker after a replacement daemon is listening."""
    marker = get_shutdown_marker_path(home)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(
            "Failed to clear active shutdown marker",
            extra={"marker_path": str(marker), "error": str(exc)},
        )


def _write_marker_atomically(marker: Path, data: Mapping[str, object]) -> None:
    """Durably replace a shutdown marker with complete owner-only JSON."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{marker.name}.",
        suffix=".tmp",
        dir=marker.parent,
        text=True,
    )
    temp_path = Path(temp_name)
    file_object_created = False
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        file_object_created = True
        with handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, marker)
        _fsync_parent_directory(marker.parent)
    except BaseException:
        if not file_object_created:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to clean up shutdown marker temp file: %s", temp_path)
        raise


def _fsync_parent_directory(directory: Path) -> None:
    """Persist a same-directory replacement where directory fsync is supported."""
    if os.name == "nt":
        return
    unsupported_errors = {
        errno.EACCES,
        errno.EBADF,
        errno.EINVAL,
        errno.ENOTSUP,
        errno.EPERM,
    }
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in unsupported_errors:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in unsupported_errors:
                raise
    finally:
        os.close(directory_fd)


def read_shutdown_intent(
    *,
    consume: bool = True,
    max_age_seconds: float = 10.0,
    home: Path | None = None,
) -> ShutdownIntentRecord:
    """Read and optionally remove the shutdown marker.

    Missing, stale, or malformed markers resolve to ``stop``.
    """
    marker = get_shutdown_marker_path(home)
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source="external_sigterm",
            sender_pid=None,
            timestamp=None,
        )
    except OSError as exc:
        logger.warning("Failed to read shutdown marker %s: %s", marker, exc)
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source="unknown",
            sender_pid=None,
            timestamp=None,
            error=str(exc),
        )

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("shutdown marker must be a JSON object")
    except (json.JSONDecodeError, TypeError) as exc:
        if consume:
            _quarantine_malformed_marker(marker, raw, exc)
        else:
            logger.warning(
                "Malformed shutdown marker at %s: %s; content=%r",
                marker,
                exc,
                raw,
            )
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source="unknown",
            sender_pid=None,
            timestamp=None,
            error=str(exc),
        )

    if consume:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Failed to consume shutdown marker %s: %s", marker, exc)

    return _record_from_marker_data(data, max_age_seconds=max_age_seconds)


def recover_stale_restart_intent(
    record: ShutdownIntentRecord,
    *,
    max_age_seconds: float,
) -> ShutdownIntentRecord:
    """Re-evaluate a consumed stale restart marker with a longer age window."""
    if (
        not record.stale
        or record.raw is None
        or record.raw.get("intent") != ShutdownIntent.RESTART.value
    ):
        return record

    recovered = _record_from_marker_data(record.raw, max_age_seconds=max_age_seconds)
    if recovered.stale or recovered.intent is not ShutdownIntent.RESTART:
        return record
    return recovered


def read_active_shutdown_intent(
    *,
    max_age_seconds: float = 120.0,
    home: Path | None = None,
) -> ShutdownIntentRecord | None:
    """Read the non-consuming active shutdown marker used by hook guards."""
    marker = get_active_shutdown_marker_path(home)
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("Failed to read active shutdown marker %s: %s", marker, exc)
        return None

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("shutdown marker must be a JSON object")
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("Malformed active shutdown marker at %s: %s", marker, exc)
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source="unknown",
            sender_pid=None,
            timestamp=None,
            error=str(exc),
        )

    return _record_from_marker_data(data, max_age_seconds=max_age_seconds)


def read_shutdown_source_record(
    *,
    max_age_seconds: float = float("inf"),
    home: Path | None = None,
) -> ShutdownIntentRecord | None:
    """Read the persistent last-shutdown source marker without consuming it."""
    marker = get_shutdown_source_path(home)
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("Failed to read shutdown source marker %s: %s", marker, exc)
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source="unknown",
            sender_pid=None,
            timestamp=None,
            error=str(exc),
        )

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise TypeError("shutdown source marker must be a JSON object")
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("Malformed shutdown source marker at %s: %s", marker, exc)
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source="unknown",
            sender_pid=None,
            timestamp=None,
            error=str(exc),
        )

    return _record_from_marker_data(data, max_age_seconds=max_age_seconds)


def _record_from_marker_data(
    data: dict[str, Any],
    *,
    max_age_seconds: float,
) -> ShutdownIntentRecord:
    source = str(data.get("source", "unknown"))
    timestamp = _optional_float(data.get("timestamp"))
    age = (time.time() - timestamp) if timestamp is not None else None
    stale = age is None or age >= max_age_seconds
    if stale:
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source=source,
            sender_pid=_optional_int(data.get("sender_pid")),
            timestamp=timestamp,
            stale=True,
            raw=data,
        )

    raw_intent = data.get("intent")
    intent = (
        coerce_shutdown_intent(str(raw_intent)) if raw_intent is not None else ShutdownIntent.STOP
    )
    return ShutdownIntentRecord(
        intent=intent,
        source=source,
        sender_pid=_optional_int(data.get("sender_pid")),
        timestamp=timestamp,
        raw=data,
    )


def format_shutdown_source(record: ShutdownIntentRecord) -> str:
    """Format a marker for shutdown logs."""
    if record.error:
        return f"unknown (error reading shutdown_intent_active.json: {record.error})"
    if record.timestamp is None and record.source == "external_sigterm":
        return "unknown (no shutdown_intent_active.json - external SIGTERM)"
    if record.stale:
        return f"stale shutdown_intent_active.json: {record.raw}"
    return f"source={record.source}, intent={record.intent.value}, sender_pid={record.sender_pid}"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError) as exc:
        logger.debug(
            "Failed to convert shutdown marker value to int",
            extra={"shutdown_value": repr(value), "error": str(exc)},
        )
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError) as exc:
        logger.debug(
            "Failed to convert shutdown marker value to float",
            extra={"shutdown_value": repr(value), "error": str(exc)},
        )
        return None


def _quarantine_malformed_marker(marker: Path, content: str, exc: BaseException) -> None:
    malformed_marker = marker.with_name(f"{marker.name}.malformed")
    logger.warning(
        "Malformed shutdown marker at %s: %s; content=%r; moving to %s",
        marker,
        exc,
        content,
        malformed_marker,
    )
    try:
        marker.replace(malformed_marker)
    except OSError as rename_exc:
        logger.warning(
            "Failed to rename malformed shutdown marker %s to %s: %s",
            marker,
            malformed_marker,
            rename_exc,
        )
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as unlink_exc:
            logger.warning("Failed to remove malformed shutdown marker %s: %s", marker, unlink_exc)
