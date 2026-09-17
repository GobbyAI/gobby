"""Safe projection of persisted SRT metadata and violation records."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from gobby.agents.sandbox_policy import (
    RETAINED_SETTINGS_PATH_KEY,
    RETAINED_VIOLATION_PATH_KEY,
    SANDBOX_RETENTION_RELATIVE_PATH,
)
from gobby.paths import get_gobby_home

_MAX_EXPOSED_VIOLATIONS = 100
_MAX_COUNTED_VIOLATIONS = 10_000


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
    live_root = gobby_home / "run" / "sandbox"
    retention_root = gobby_home / SANDBOX_RETENTION_RELATIVE_PATH
    retained_log = _trusted_path(raw.get(RETAINED_VIOLATION_PATH_KEY), retention_root)
    retained_settings = _trusted_path(raw.get(RETAINED_SETTINGS_PATH_KEY), retention_root)
    if retained_log is not None:
        record[RETAINED_VIOLATION_PATH_KEY] = str(retained_log)
    if retained_settings is not None:
        record[RETAINED_SETTINGS_PATH_KEY] = str(retained_settings)
    # The live log wins while the run root survives; the retained copy keeps the
    # count honest once the reaper deletes it.
    violation_path = _trusted_path(raw.get("violation_path"), live_root) or retained_log
    count, violations, count_truncated = _read_violations(
        violation_path,
        include_events=include_events,
    )
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


def _read_violations(
    path: Path | None,
    *,
    include_events: bool,
) -> tuple[int, list[Any], bool]:
    if path is None:
        return 0, [], False
    recent: deque[Any] = deque(maxlen=_MAX_EXPOSED_VIOLATIONS)
    count = 0
    truncated = False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += 1
                if include_events:
                    recent.append(value)
                elif count >= _MAX_COUNTED_VIOLATIONS:
                    truncated = next(handle, None) is not None
                    break
    except OSError:
        return 0, [], False
    return count, list(recent), truncated
