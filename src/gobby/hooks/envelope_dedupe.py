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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import psutil

from gobby.cli.utils import get_gobby_home

logger = logging.getLogger(__name__)

ENVELOPE_ID_HEADER: Final = "X-Gobby-Envelope-Id"
ENVELOPE_REPLAY_GRACE_SECONDS: Final = 120.0
ENVELOPE_PROCESSING_LEASE_TTL_SECONDS: Final = 15.0
_OWNER_CREATE_TIME_MATCH_SECONDS: Final = 1.0
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

STOP_REPLAY_EPOCH_FILENAME: Final = ".stop_replay_epoch"
_STOP_REPLAY_HOOK_TYPES: Final = frozenset(
    {
        "stop",
        "subagent_stop",
        "subagent_end",
        "session_end",
        "turn_end",
        "stop_failure",
    }
)
_FOUND_WORK_STOP_RULE: Final = "[block-terminal-validation-failure]"
_BLOCK_DECISIONS: Final = frozenset({"block", "deny"})


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
    now = datetime.now(UTC)
    try:
        pid, create_time = _owner_process_identity()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pid, create_time = os.getpid(), time.time()
    try:
        with marker.open("x", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "envelope_id": envelope_id,
                        "claimed_at": now.isoformat(),
                        "status": "processing",
                        "owner_token": str(uuid4()),
                        "owner_pid": pid,
                        "owner_create_time": create_time,
                        "renewed_at": now.isoformat(),
                        "lease_expires_at": _lease_expiry(now).isoformat(),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    except FileExistsError:
        return False
    return True


def envelope_processing_owner_token(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
) -> str | None:
    """Return the live processing lease token for an envelope, if present."""
    record = read_envelope_marker(envelope_id, processed_dir=processed_dir)
    if record is None or record.get("status") != "processing":
        return None
    token = record.get("owner_token")
    return token if isinstance(token, str) and token else None


def renew_envelope_processing_lease(
    envelope_id: str,
    owner_token: str,
    *,
    processed_dir: Path | None = None,
) -> bool:
    """Extend a live processing lease when the owner token still matches."""
    now = datetime.now(UTC)

    def _renew(record: dict[str, Any]) -> dict[str, Any]:
        record["renewed_at"] = now.isoformat()
        record["lease_expires_at"] = _lease_expiry(now).isoformat()
        return record

    return _cas_mutate_processing_marker(
        envelope_id,
        owner_token,
        processed_dir=processed_dir,
        writer=_renew,
    )


def finalize_envelope_processed(
    envelope_id: str,
    owner_token: str,
    *,
    response: Mapping[str, Any] | None = None,
    processed_dir: Path | None = None,
    hook_type: str | None = None,
) -> bool:
    """Compare-and-set a live processing lease into a terminal processed marker."""

    def _finalize(record: dict[str, Any]) -> dict[str, Any]:
        finalized: dict[str, Any] = {
            "envelope_id": envelope_id,
            "processed_at": datetime.now(UTC).isoformat(),
            "status": "processed",
        }
        if response is not None:
            finalized["response"] = dict(response)
        stored_hook_type = hook_type if hook_type else None
        if stored_hook_type is None:
            existing_hook_type = record.get("hook_type")
            if isinstance(existing_hook_type, str) and existing_hook_type:
                stored_hook_type = existing_hook_type
        if stored_hook_type:
            finalized["hook_type"] = stored_hook_type
        return finalized

    return _cas_mutate_processing_marker(
        envelope_id,
        owner_token,
        processed_dir=processed_dir,
        writer=_finalize,
    )


def release_envelope_processing_claim(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
    owner_token: str | None = None,
) -> bool:
    """Release a live processing claim so a retry can reclaim the envelope."""
    if not envelope_id:
        return False
    if owner_token:
        return _cas_mutate_processing_marker(
            envelope_id,
            owner_token,
            processed_dir=processed_dir,
            writer=lambda _record: None,
        )
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
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a stored terminal hook response for a processed envelope.

    STOP/turn_end blocks are replayed only inside ENVELOPE_REPLAY_GRACE_SECONDS
    and only if they were processed after the latest session_start epoch.
    Aged or pre-epoch stop blocks are dropped so the envelope can re-evaluate.
    """
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
    if not isinstance(response, dict):
        return None
    if _should_replay_processed_response(
        record,
        response,
        now=now,
        processed_dir=processed_dir,
    ):
        return response
    remove_envelope_marker(envelope_id, processed_dir=processed_dir)
    logger.debug("Dropped stale processed STOP/turn_end replay for envelope %s", envelope_id)
    return None


def bump_stop_replay_epoch(
    *,
    processed_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Start a new session_start epoch so older STOP/turn_end blocks are not replayed."""
    target = processed_dir if processed_dir is not None else get_processed_envelope_dir()
    at = now or datetime.now(UTC)
    path = target / STOP_REPLAY_EPOCH_FILENAME
    try:
        target.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        temp_path.write_text(at.isoformat() + "\n", encoding="utf-8")
        try:
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        logger.debug("Skipping stop-replay epoch bump at %s", path, exc_info=True)


def read_stop_replay_epoch(*, processed_dir: Path | None = None) -> datetime | None:
    """Return the latest session_start STOP-replay epoch, if one has been written."""
    target = processed_dir if processed_dir is not None else get_processed_envelope_dir()
    path = target / STOP_REPLAY_EPOCH_FILENAME
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _parse_iso_datetime(raw)


def mark_envelope_processed(
    envelope_id: str,
    *,
    response: Mapping[str, Any] | None = None,
    processed_dir: Path | None = None,
    hook_type: str | None = None,
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
    if existing is not None and existing.get("status") == "processing":
        return
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
    stored_hook_type = hook_type if hook_type else None
    if stored_hook_type is None and existing is not None:
        existing_hook_type = existing.get("hook_type")
        if isinstance(existing_hook_type, str) and existing_hook_type:
            stored_hook_type = existing_hook_type
    if stored_hook_type:
        record["hook_type"] = stored_hook_type

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


def remove_envelope_marker(
    envelope_id: str,
    *,
    processed_dir: Path | None = None,
) -> bool:
    """Remove the durable marker for an envelope."""
    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


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

    reference = now or datetime.now(UTC)
    if _owner_process_is_live(record):
        return True
    if not _lease_is_expired(record, now=reference, stale_after_seconds=stale_after_seconds):
        return True
    return False


def _lease_expiry(now: datetime) -> datetime:
    return now + timedelta(seconds=ENVELOPE_PROCESSING_LEASE_TTL_SECONDS)


def _lease_is_expired(
    record: Mapping[str, Any],
    *,
    now: datetime,
    stale_after_seconds: float,
) -> bool:
    expires_at = _parse_iso_datetime(record.get("lease_expires_at"))
    if expires_at is not None:
        return now >= expires_at
    claimed_at = _processing_claimed_at(record)
    if claimed_at is None:
        return True
    age_seconds = (now - claimed_at).total_seconds()
    return age_seconds < 0 or age_seconds >= stale_after_seconds


def _owner_process_identity() -> tuple[int, float]:
    process = psutil.Process()
    return int(process.pid), float(process.create_time())


def _owner_process_is_live(record: Mapping[str, Any]) -> bool:
    pid = record.get("owner_pid")
    create_time = record.get("owner_create_time")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    if isinstance(create_time, bool) or not isinstance(create_time, (int, float)):
        return False
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
        return abs(float(process.create_time()) - float(create_time)) < (
            _OWNER_CREATE_TIME_MATCH_SECONDS
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _restore_marker(claimed_marker: Path, marker: Path) -> None:
    if marker.exists():
        claimed_marker.unlink(missing_ok=True)
        return
    try:
        os.link(claimed_marker, marker)
    except FileExistsError:
        pass
    finally:
        claimed_marker.unlink(missing_ok=True)


def _cas_mutate_processing_marker(
    envelope_id: str,
    owner_token: str,
    *,
    processed_dir: Path | None,
    writer: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> bool:
    if not envelope_id or not owner_token:
        return False
    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    claimed_marker = marker.with_name(f".{marker.name}.{uuid4().hex}.cas")
    try:
        marker.rename(claimed_marker)
    except FileNotFoundError:
        return False
    try:
        try:
            record = json.loads(claimed_marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _restore_marker(claimed_marker, marker)
            return False
        if (
            not isinstance(record, dict)
            or record.get("status") != "processing"
            or record.get("owner_token") != owner_token
        ):
            _restore_marker(claimed_marker, marker)
            return False
        updated = writer(record)
        if updated is None:
            claimed_marker.unlink(missing_ok=True)
            return True
        temp_path = marker.with_name(f"{marker.name}.{uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(updated, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.replace(temp_path, marker)
        finally:
            temp_path.unlink(missing_ok=True)
        claimed_marker.unlink(missing_ok=True)
        return True
    except Exception:
        if claimed_marker.exists() and not marker.exists():
            _restore_marker(claimed_marker, marker)
        else:
            claimed_marker.unlink(missing_ok=True)
        raise


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
    return _parse_iso_datetime(record.get("claimed_at"))


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalized_hook_type(hook_type: object) -> str:
    if not isinstance(hook_type, str):
        return ""
    return hook_type.strip().casefold().replace("-", "_")


def _response_is_block(response: Mapping[str, Any]) -> bool:
    decision = response.get("decision")
    return isinstance(decision, str) and decision.casefold() in _BLOCK_DECISIONS


def _is_stop_or_turn_end_block(record: Mapping[str, Any], response: Mapping[str, Any]) -> bool:
    if not _response_is_block(response):
        return False
    hook_type = _normalized_hook_type(record.get("hook_type"))
    if hook_type in _STOP_REPLAY_HOOK_TYPES:
        return True
    reason = response.get("reason")
    return isinstance(reason, str) and _FOUND_WORK_STOP_RULE in reason


def _should_replay_processed_response(
    record: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    now: datetime | None,
    processed_dir: Path | None,
) -> bool:
    if not _is_stop_or_turn_end_block(record, response):
        return True
    processed_at = _parse_iso_datetime(record.get("processed_at"))
    if processed_at is None:
        return False
    reference = now or datetime.now(UTC)
    age_seconds = (reference - processed_at).total_seconds()
    if age_seconds < 0 or age_seconds >= ENVELOPE_REPLAY_GRACE_SECONDS:
        return False
    epoch = read_stop_replay_epoch(processed_dir=processed_dir)
    return epoch is None or processed_at >= epoch
