"""Shadow capture for decision consumers (plan 2.1).

Each shadow run appends one JSON line to ``~/.gobby/decisions/shadow/<consumer>.jsonl``.
Writes are best effort and never raise into the consumer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

MAX_LINES = 10_000
KEEP_LINES = 5_000

_line_counts: dict[Path, int] = {}
_warned = False


def record(consumer: str, record: Mapping[str, Any]) -> None:
    """Append one shadow record for ``consumer``; never raises into the caller."""
    global _warned
    try:
        _append(consumer, _stamp(consumer, record))
    except Exception:
        if not _warned:
            _warned = True
            logger.warning(
                "Shadow record write failed; later failures are not logged", exc_info=True
            )


def _stamp(consumer: str, record: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(record)
    body["id"] = str(uuid.uuid4())
    body["ts"] = time.time()
    body["consumer"] = consumer
    content = {key: body.get(key) for key in ("state", "questions", "propositions")}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    body["content_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return body


def _append(consumer: str, body: Mapping[str, Any]) -> None:
    directory = get_gobby_home() / "decisions" / "shadow"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{consumer}.jsonl"
    count = _line_counts[path] if path in _line_counts else _count_lines(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(body, sort_keys=True) + "\n")
    count += 1
    if count > MAX_LINES:
        _keep_newest(path, KEEP_LINES)
        count = KEEP_LINES
    _line_counts[path] = count


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _keep_newest(path: Path, keep: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)[-keep:]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
