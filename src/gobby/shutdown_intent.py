"""Shutdown intent marker helpers.

The marker lives at the legacy ``shutdown_source.json`` path so existing
diagnostics keep working while restart/stop can carry explicit policy.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class ShutdownIntent(StrEnum):
    """Semantic daemon shutdown intent."""

    STOP = "stop"
    RESTART = "restart"

    @property
    def cancel_agents(self) -> bool:
        return self is ShutdownIntent.STOP

    @property
    def preserve_agents(self) -> bool:
        return self is ShutdownIntent.RESTART


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
    def cancel_agents(self) -> bool:
        return self.intent.cancel_agents

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


def infer_shutdown_intent(source: str) -> ShutdownIntent:
    """Infer intent for legacy source-only markers."""
    return ShutdownIntent.RESTART if "restart" in source.lower() else ShutdownIntent.STOP


def get_shutdown_marker_path(home: Path | None = None) -> Path:
    """Return the shutdown marker path."""
    if home is None:
        home = Path(os.environ.get("GOBBY_HOME", str(Path.home() / ".gobby")))
    return home / "shutdown_source.json"


def write_shutdown_intent(
    source: str,
    intent: str | ShutdownIntent,
    sender_pid: int | None = None,
    *,
    home: Path | None = None,
) -> None:
    """Write a fresh shutdown marker with explicit intent and legacy source."""
    marker = get_shutdown_marker_path(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "source": source,
        "intent": coerce_shutdown_intent(intent).value,
        "sender_pid": sender_pid or os.getpid(),
        "timestamp": time.time(),
    }
    marker.write_text(json.dumps(data))


def write_stop_intent(source: str, sender_pid: int | None = None) -> None:
    """Write a stop marker."""
    write_shutdown_intent(source, ShutdownIntent.STOP, sender_pid)


def write_restart_intent(source: str, sender_pid: int | None = None) -> None:
    """Write a restart marker."""
    write_shutdown_intent(source, ShutdownIntent.RESTART, sender_pid)


def read_shutdown_intent(
    *,
    consume: bool = True,
    max_age_seconds: float = 10.0,
    home: Path | None = None,
) -> ShutdownIntentRecord:
    """Read and optionally remove the shutdown marker.

    Missing, stale, malformed, or legacy unknown markers resolve to ``stop``.
    """
    marker = get_shutdown_marker_path(home)
    try:
        if not marker.exists():
            return ShutdownIntentRecord(
                intent=ShutdownIntent.STOP,
                source="external_sigterm",
                sender_pid=None,
                timestamp=None,
            )

        data = json.loads(marker.read_text())
        if not isinstance(data, dict):
            raise TypeError("shutdown marker must be a JSON object")
        if consume:
            marker.unlink(missing_ok=True)

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
            coerce_shutdown_intent(str(raw_intent))
            if raw_intent is not None
            else infer_shutdown_intent(source)
        )
        return ShutdownIntentRecord(
            intent=intent,
            source=source,
            sender_pid=_optional_int(data.get("sender_pid")),
            timestamp=timestamp,
            raw=data,
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return ShutdownIntentRecord(
            intent=ShutdownIntent.STOP,
            source="unknown",
            sender_pid=None,
            timestamp=None,
            error=str(exc),
        )


def format_shutdown_source(record: ShutdownIntentRecord) -> str:
    """Format a marker for shutdown logs."""
    if record.error:
        return f"unknown (error reading shutdown_source.json: {record.error})"
    if record.timestamp is None and record.source == "external_sigterm":
        return "unknown (no shutdown_source.json - external SIGTERM)"
    if record.stale:
        return f"stale shutdown_source.json: {record.raw}"
    return f"source={record.source}, intent={record.intent.value}, sender_pid={record.sender_pid}"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
