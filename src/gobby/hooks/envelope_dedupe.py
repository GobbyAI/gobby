"""Idempotency markers for ghook envelope delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from gobby.cli.utils import get_gobby_home

logger = logging.getLogger(__name__)

ENVELOPE_ID_HEADER: Final = "X-Gobby-Envelope-Id"
ENVELOPE_REPLAY_GRACE_SECONDS: Final = 120.0
_ENVELOPE_FILENAME_RE: Final = re.compile(r"^[nc]-(?P<timestamp_ms>\d+)-.+$")

# A marker only has to outlive the window in which its envelope can still
# arrive again: is_envelope_processed guards inbox replay and
# envelope_terminal_response answers a duplicate delivery from a retrying
# ghook client, and both are bounded by ENVELOPE_REPLAY_GRACE_SECONDS. A day
# is ~700x that grace period, so nothing that could still be replayed is ever
# inside the prune, and the directory settles at roughly one day of traffic.
PROCESSED_MARKER_RETENTION_SECONDS: Final = 24 * 60 * 60.0

# One pass reads at most this many directory entries. Without a bound the
# first pass after this landed would have walked 1.7M entries -- a 172-second
# scan -- in one go; with it the backlog drains over successive passes while
# each pass stays short. Steady state is ~27k entries, so an ordinary pass
# sees the whole directory and the bound never binds.
PROCESSED_MARKER_PRUNE_MAX_ENTRIES: Final = 100_000


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
    age_seconds = (reference - created_at).total_seconds()
    return 0 <= age_seconds < grace_seconds


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
    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    if record is None:
        return _clear_stale_unreadable_marker(
            marker,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
    if record.get("status") != "processing":
        return False
    if _is_active_processing_record(
        record,
        now=now,
        stale_after_seconds=stale_after_seconds,
    ):
        return False

    latest_record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    if latest_record is None:
        return _clear_stale_unreadable_marker(
            marker,
            now=now,
            stale_after_seconds=stale_after_seconds,
        )
    if latest_record.get("status") != "processing" or _is_active_processing_record(
        latest_record,
        now=now,
        stale_after_seconds=stale_after_seconds,
    ):
        return False

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


def release_envelope_processing_claim(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
) -> bool:
    """Release a live processing claim so a retry can reclaim the envelope."""
    if not envelope_id:
        return False
    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    claimed_marker = marker.with_name(f".{marker.name}.{uuid4().hex}.release")
    try:
        marker.rename(claimed_marker)
    except FileNotFoundError:
        return False
    try:
        try:
            record = json.loads(claimed_marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = None
        if isinstance(record, dict) and record.get("status") == "processing":
            claimed_marker.unlink(missing_ok=True)
            return True

        try:
            os.link(claimed_marker, marker)
        except FileExistsError:
            pass
        finally:
            claimed_marker.unlink(missing_ok=True)
        return False
    except Exception:
        if claimed_marker.exists() and not marker.exists():
            try:
                os.link(claimed_marker, marker)
            except FileExistsError:
                pass
        claimed_marker.unlink(missing_ok=True)
        raise


def read_envelope_marker(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the persisted envelope marker, if present.

    Non-empty malformed JSON is treated as processed. The marker is already
    terminal enough to prevent duplicate hook execution, even if its response
    payload cannot be replayed.
    """
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
        if raw.strip():
            logger.warning(
                "Malformed processed hook envelope marker %s; treating as processed",
                marker,
            )
            return {"envelope_id": envelope_id, "status": "processed"}
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
    """Persist a terminal processed marker for an envelope ID.

    A response-less write keeps an existing stored response so inbox replay can
    mark completion without degrading future duplicate-response replay.
    """
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
    age_seconds = (reference - claimed_at).total_seconds()
    return 0 <= age_seconds < stale_after_seconds


def _clear_stale_unreadable_marker(
    marker: Path,
    *,
    now: datetime | None,
    stale_after_seconds: float,
) -> bool:
    try:
        stat = marker.stat()
    except FileNotFoundError:
        return False
    reference = now or datetime.now(UTC)
    modified_at = datetime.fromtimestamp(stat.st_mtime, UTC)
    age_seconds = (reference - modified_at).total_seconds()
    if age_seconds < stale_after_seconds:
        return False
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


@dataclass(frozen=True)
class DirectoryPruneResult:
    """What one bounded prune pass over a directory did."""

    examined: int = 0
    deleted: int = 0
    truncated: bool = False
    """True when the pass stopped at its entry bound with the directory unfinished."""


def prune_directory_by_age(
    target: Path,
    *,
    cutoff: float,
    max_entries: int,
    matches: Callable[[str], bool] | None = None,
) -> DirectoryPruneResult:
    """Delete files older than the cutoff, bounded to one pass.

    Blocking: the caller must keep this off the event loop thread. `matches`
    selects entries by name and defaults to every entry; the bound counts every
    entry read, because reading is the cost the bound exists to limit.

    Entries are examined in directory order, which puts the oldest first, so a
    truncated pass deletes the oldest of the backlog and the next pass resumes
    where this one stopped.
    """
    examined = 0
    deleted = 0
    truncated = False
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                if examined >= max_entries:
                    truncated = True
                    break
                examined += 1
                if matches is not None and not matches(entry.name):
                    continue
                if _prune_entry(entry, cutoff=cutoff):
                    deleted += 1
    except OSError:
        # A missing or unreadable directory is not an error worth losing the
        # maintenance loop over; the next pass tries again.
        return DirectoryPruneResult(examined=examined, deleted=deleted)
    return DirectoryPruneResult(examined=examined, deleted=deleted, truncated=truncated)


def prune_processed_envelope_markers(
    processed_dir: Path | None = None,
    *,
    now: float | None = None,
    retention_seconds: float = PROCESSED_MARKER_RETENTION_SECONDS,
    max_entries: int = PROCESSED_MARKER_PRUNE_MAX_ENTRIES,
) -> DirectoryPruneResult:
    """Delete marker files past the retention window, bounded to one pass.

    Blocking: the caller must keep this off the event loop thread. Age comes
    from the file's mtime rather than its stored processed_at because a marker
    is written once and never rewritten, and reading every marker to parse a
    timestamp would cost an open and a JSON parse per entry.

    Every entry is a candidate: nothing but markers is written here.
    """
    target = processed_dir if processed_dir is not None else get_processed_envelope_dir()
    cutoff = (now if now is not None else time.time()) - retention_seconds
    return prune_directory_by_age(target, cutoff=cutoff, max_entries=max_entries)


def _prune_entry(entry: os.DirEntry[str], *, cutoff: float) -> bool:
    """Delete one directory entry when it is a file older than the cutoff."""
    try:
        if not entry.is_file(follow_symlinks=False):
            return False
        if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
            return False
        os.unlink(entry.path)
    except FileNotFoundError:
        # Another pass or a concurrent writer got there first.
        return False
    except OSError:
        # One bad entry must not end the pass: the rest of the directory is
        # still prunable and the loop has to survive it.
        return False
    return True


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
