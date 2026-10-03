"""Safe projection of persisted SRT metadata and violation records."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.agents.sandbox_policy import (
    RETAINED_SETTINGS_PATH_KEY,
    RETAINED_VIOLATION_PATH_KEY,
    SANDBOX_RETENTION_RELATIVE_PATH,
    managed_execution_root,
)
from gobby.paths import get_gobby_home

_MAX_EXPOSED_VIOLATIONS = 100
# A prefix this long identifies a denied command; 100 events stay near 200 KB.
_MAX_EXPOSED_COMMAND_CHARS = 2048
_MAX_COUNTED_VIOLATIONS = 10_000
_MAX_TRACKED_LOGS = 512
_MAX_TAIL_BYTES = 16 * 1024 * 1024
_TAIL_BLOCK_BYTES = 64 * 1024


def sandbox_list_record(raw: object, *, active: bool) -> dict[str, Any] | None:
    """Project the small sandbox summary without reading terminal run files."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    record = {
        "backend": raw.get("backend"),
        "enforced": bool(raw.get("enforced")),
        "runtime_version": raw.get("runtime_version"),
        "policy_hash": raw.get("policy_hash"),
    }
    if active:
        gobby_home = get_gobby_home()
        retention_root = gobby_home / SANDBOX_RETENTION_RELATIVE_PATH
        path = trusted_live_violation_path(raw.get("violation_path")) or _trusted_path(
            raw.get(RETAINED_VIOLATION_PATH_KEY), retention_root
        )
        count, _, truncated = _read_violations(path, include_events=False)
        record["violation_count"] = count
        if truncated:
            record["violation_count_truncated"] = True
    else:
        frozen_count = raw.get("violation_count")
        if (
            isinstance(frozen_count, int)
            and not isinstance(frozen_count, bool)
            and frozen_count >= 0
        ):
            record["violation_count"] = frozen_count
            if raw.get("violation_count_truncated") is True:
                record["violation_count_truncated"] = True
        else:
            # Historical terminal rows may predate frozen counts. Unknown is honest;
            # listing them must never scan a retained log on every poll.
            record["violation_count"] = None
    return record


def sandbox_record(
    resume_metadata: dict[str, Any] | None,
    *,
    include_events: bool,
) -> dict[str, Any] | None:
    if not isinstance(resume_metadata, dict):
        return None
    raw = resume_metadata.get("sandbox")
    if not isinstance(raw, dict):
        return None
    record = {
        "backend": raw.get("backend"),
        "enforced": bool(raw.get("enforced")),
        "runtime_version": raw.get("runtime_version"),
        "policy_hash": raw.get("policy_hash"),
    }
    gobby_home = get_gobby_home()
    retention_root = gobby_home / SANDBOX_RETENTION_RELATIVE_PATH
    retained_log = _trusted_path(raw.get(RETAINED_VIOLATION_PATH_KEY), retention_root)
    retained_settings = _trusted_path(raw.get(RETAINED_SETTINGS_PATH_KEY), retention_root)
    if retained_log is not None:
        record[RETAINED_VIOLATION_PATH_KEY] = str(retained_log)
    if retained_settings is not None:
        record[RETAINED_SETTINGS_PATH_KEY] = str(retained_settings)
    # The live log wins while the run root survives; the retained copy keeps the
    # count honest once the reaper deletes it.
    violation_path = trusted_live_violation_path(raw.get("violation_path")) or retained_log
    count, violations, count_truncated = _read_violations(
        violation_path,
        include_events=include_events,
    )
    # The terminal transition freezes the count before retention replaces the live log.
    if violation_path is None:
        frozen_count = raw.get("violation_count")
        if (
            isinstance(frozen_count, int)
            and not isinstance(frozen_count, bool)
            and frozen_count >= 0
        ):
            count = frozen_count
            count_truncated = raw.get("violation_count_truncated") is True
    record["violation_count"] = count
    if count_truncated:
        record["violation_count_truncated"] = True
    if include_events:
        record["violations"] = violations
    return record


def _trusted_path(raw_path: object, trusted_root: Path) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    root = trusted_root.resolve(strict=False)
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(root) or not resolved.is_file() or path.is_symlink():
        return None
    return resolved


def trusted_live_violation_path(raw_path: object) -> Path | None:
    """Accept only a run's SRT violation log within a daemon-owned run root."""
    roots = (
        (managed_execution_root(), False),
        (get_gobby_home() / "run" / "sandbox", True),
    )
    for root, legacy in roots:
        path = _trusted_path(raw_path, root)
        if path is None:
            continue
        parts = path.relative_to(root.resolve(strict=False)).parts
        if len(parts) == 3 and parts[1:] == ("logs", "violations.jsonl"):
            return path
        if legacy and len(parts) == 2 and parts[1] == "violations.jsonl":
            return path
    return None


def _read_violations(
    path: Path | None,
    *,
    include_events: bool,
) -> tuple[int, list[Any], bool]:
    if path is None:
        return 0, [], False
    count, truncated = _count_violation_lines(path)
    return count, _recent_violations(path) if include_events else [], truncated


def _recent_violations(path: Path) -> list[Any]:
    """Decode only the newest events; a log can reach gigabytes, so never parse it whole."""
    recent: list[Any] = []
    pending = b""
    try:
        with path.open("rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            floor = max(0, position - _MAX_TAIL_BYTES)
            while position > floor and len(recent) < _MAX_EXPOSED_VIOLATIONS:
                size = min(_TAIL_BLOCK_BYTES, position - floor)
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + pending).split(b"\n")
                # Above the file start the first piece may continue in the block before it;
                # at the byte ceiling that partial line is dropped.
                pending = lines.pop(0) if position > 0 else b""
                for line in reversed(lines):
                    if len(recent) == _MAX_EXPOSED_VIOLATIONS:
                        break
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line.decode("utf-8", errors="replace"))
                        recent.append(_bounded_command(event))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []
    recent.reverse()
    return recent


def _bounded_command(event: Any) -> Any:
    """Keep a command prefix; logs already written carry commands of ~59 KB each."""
    if not isinstance(event, dict):
        return event
    command = event.get("command")
    if not isinstance(command, str) or len(command) <= _MAX_EXPOSED_COMMAND_CHARS:
        return event
    return {
        **event,
        "command": command[:_MAX_EXPOSED_COMMAND_CHARS],
        "command_length": len(command),
        "command_truncated": True,
    }


@dataclass
class _ViolationCount:
    """Progress through one append-only log: complete lines before ``offset`` are counted."""

    identity: tuple[int, int]
    offset: int = 0
    lines: int = 0
    capped: bool = False
    size: int = -1
    mtime_ns: int = -1
    result: tuple[int, bool] = (0, False)


_violation_counts: dict[str, _ViolationCount] = {}
_violation_counts_lock = threading.Lock()


def _count_violation_lines(path: Path) -> tuple[int, bool]:
    """Count nonblank log lines, resuming where the last count of this log stopped.

    A live log grows while its run is polled, so each poll reads only the bytes appended
    since the last one; an unterminated last line is re-read until it ends (#23279).
    Bytes skip decoding, which halved the cold count on a 1 GB log.
    """
    key = str(path)
    with _violation_counts_lock:
        # Stat under the lock: a size taken before another poll advanced the offset
        # would read as truncation and rescan the whole log.
        try:
            stat = path.stat()
        except OSError:
            return 0, False
        identity = (stat.st_dev, stat.st_ino)
        progress = _violation_counts.pop(key, None)
        if progress is None or progress.identity != identity or stat.st_size < progress.offset:
            progress = _ViolationCount(identity)
        if (progress.size, progress.mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            try:
                progress.result = _advance_violation_count(path, progress)
            except OSError:
                return 0, False
            progress.size, progress.mtime_ns = stat.st_size, stat.st_mtime_ns
        _violation_counts[key] = progress
        if len(_violation_counts) > _MAX_TRACKED_LOGS:
            del _violation_counts[next(iter(_violation_counts))]
        return progress.result


def _advance_violation_count(path: Path, progress: _ViolationCount) -> tuple[int, bool]:
    with path.open("rb") as handle:
        handle.seek(progress.offset)
        if progress.capped:
            return progress.lines, bool(handle.read(1))
        for line in handle:
            if not line.endswith(b"\n"):
                return progress.lines + bool(line.strip()), False
            progress.offset += len(line)
            if line.strip():
                progress.lines += 1
                if progress.lines >= _MAX_COUNTED_VIOLATIONS:
                    progress.capped = True
                    return progress.lines, bool(handle.read(1))
    return progress.lines, False
