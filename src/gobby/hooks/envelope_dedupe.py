"""Idempotency markers for ghook envelope delivery."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from gobby.cli.utils import get_gobby_home

ENVELOPE_ID_HEADER: Final = "X-Gobby-Envelope-Id"
ENVELOPE_REPLAY_GRACE_SECONDS: Final = 120.0
_ENVELOPE_FILENAME_RE: Final = re.compile(r"^[nc]-(?P<timestamp_ms>\d+)-.+$")


def get_processed_envelope_dir(inbox_dir: Path | None = None) -> Path:
    """Return the directory that stores processed envelope ID markers."""
    root = inbox_dir or get_gobby_home() / "hooks" / "inbox"
    return root / "processed"


def envelope_id_from_inbox_path(path: Path) -> str | None:
    """Return the durable envelope ID encoded in a ghook inbox filename."""
    stem = path.stem
    return stem or None


def envelope_timestamp_ms_from_inbox_path(path: Path) -> int | None:
    """Return the millisecond timestamp encoded in a ghook inbox filename."""
    match = _ENVELOPE_FILENAME_RE.match(path.stem)
    if match is None:
        return None
    try:
        return int(match.group("timestamp_ms"))
    except ValueError:
        return None


def is_inbox_envelope_fresh(
    path: Path,
    *,
    now: datetime | None = None,
    grace_seconds: float = ENVELOPE_REPLAY_GRACE_SECONDS,
) -> bool:
    """Return whether an inbox file is still inside the replay grace period."""
    timestamp_ms = envelope_timestamp_ms_from_inbox_path(path)
    if timestamp_ms is None:
        return False
    try:
        created_at = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
    except (OSError, OverflowError, ValueError):
        return False

    reference = now or datetime.now(UTC)
    return (reference - created_at).total_seconds() < grace_seconds


def is_envelope_processed(envelope_id: str, *, processed_dir: Path | None = None) -> bool:
    """Return whether an envelope ID has a terminal processed marker."""
    if not envelope_id:
        return False
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    if record is None:
        return False
    status = record.get("status")
    if not isinstance(status, str):
        return status is None
    return status == "processed"


def is_envelope_processing_active(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
    now: datetime | None = None,
    stale_after_seconds: float = ENVELOPE_REPLAY_GRACE_SECONDS,
) -> bool:
    """Return whether an envelope has an active in-flight processing marker."""
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    return _is_active_processing_record(
        record,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )


def clear_stale_envelope_processing_marker(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
    now: datetime | None = None,
    stale_after_seconds: float = ENVELOPE_REPLAY_GRACE_SECONDS,
) -> bool:
    """Remove a stale processing marker so the envelope can be retried."""
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    if record is None or record.get("status") != "processing":
        return False
    if _is_active_processing_record(
        record,
        now=now,
        stale_after_seconds=stale_after_seconds,
    ):
        return False

    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def claim_envelope_processing(envelope_id: str, *, processed_dir: Path | None = None) -> bool:
    """Atomically claim first processing rights for an envelope ID."""
    if not envelope_id:
        return False

    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "envelope_id": envelope_id,
                        "claimed_at": datetime.now(UTC).isoformat(),
                        "status": "processing",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    except FileExistsError:
        return False
    return True


def read_envelope_marker(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the persisted envelope marker, if present and well-formed."""
    if not envelope_id:
        return None
    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def envelope_terminal_response(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return a stored terminal hook response for a processed envelope."""
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    if record is None:
        return None
    status = record.get("status")
    if isinstance(status, str):
        if status != "processed":
            return None
    elif status is not None:
        return None
    response = record.get("response")
    return response if isinstance(response, dict) else None


def mark_envelope_processed(
    envelope_id: str,
    *,
    response: Mapping[str, Any] | None = None,
    processed_dir: Path | None = None,
) -> None:
    """Persist a terminal processed marker for an envelope ID."""
    if not envelope_id:
        return

    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    existing = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    if (
        response is None
        and existing is not None
        and existing.get("status", "processed") == "processed"
        and isinstance(existing.get("response"), dict)
    ):
        return
    record: dict[str, Any] = {
        "envelope_id": envelope_id,
        "processed_at": datetime.now(UTC).isoformat(),
        "status": "processed",
    }
    if response is not None:
        record["response"] = dict(response)

    temp_path = marker.with_name(f"{marker.name}.{uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(record, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        os.replace(temp_path, marker)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _processed_marker_path(envelope_id: str, *, processed_dir: Path | None = None) -> Path:
    digest = hashlib.sha256(envelope_id.encode("utf-8")).hexdigest()
    return (
        get_processed_envelope_dir() / f"{digest}.json"
        if processed_dir is None
        else processed_dir / f"{digest}.json"
    )


def _is_active_processing_record(
    record: Mapping[str, Any] | None,
    *,
    now: datetime | None,
    stale_after_seconds: float,
) -> bool:
    if record is None or record.get("status") != "processing":
        return False

    claimed_at = _processing_claimed_at(record)
    if claimed_at is None:
        return False

    reference = now or datetime.now(UTC)
    return (reference - claimed_at).total_seconds() < stale_after_seconds


def _processing_claimed_at(record: Mapping[str, Any]) -> datetime | None:
    raw_claimed_at = record.get("claimed_at")
    if not isinstance(raw_claimed_at, str):
        return None
    try:
        claimed_at = datetime.fromisoformat(raw_claimed_at)
    except ValueError:
        return None
    if claimed_at.tzinfo is None:
        return claimed_at.replace(tzinfo=UTC)
    return claimed_at.astimezone(UTC)
