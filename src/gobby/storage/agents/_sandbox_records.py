"""Safe projection of persisted SRT metadata and violation records."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

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
    violation_path = _trusted_violation_path(raw.get("violation_path"))
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


def _trusted_violation_path(raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    root = (get_gobby_home() / "run" / "sandbox").resolve(strict=False)
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
