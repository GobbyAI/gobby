"""Idempotency markers for ghook envelope delivery."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gobby.cli.utils import get_gobby_home

ENVELOPE_ID_HEADER: Final = "X-Gobby-Envelope-Id"


def get_processed_envelope_dir(inbox_dir: Path | None = None) -> Path:
    """Return the directory that stores processed envelope ID markers."""
    root = inbox_dir or get_gobby_home() / "hooks" / "inbox"
    return root / "processed"


def envelope_id_from_inbox_path(path: Path) -> str | None:
    """Return the durable envelope ID encoded in a ghook inbox filename."""
    stem = path.stem
    return stem or None


def is_envelope_processed(envelope_id: str, *, processed_dir: Path | None = None) -> bool:
    """Return whether an envelope ID has already been handled by the daemon."""
    if not envelope_id:
        return False
    return _processed_marker_path(envelope_id, processed_dir=processed_dir).exists()


def mark_envelope_processed(envelope_id: str, *, processed_dir: Path | None = None) -> None:
    """Persist a processed marker for an envelope ID."""
    if not envelope_id:
        return

    marker = _processed_marker_path(envelope_id, processed_dir=processed_dir)
    if marker.exists():
        return

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "envelope_id": envelope_id,
                "processed_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _processed_marker_path(envelope_id: str, *, processed_dir: Path | None = None) -> Path:
    digest = hashlib.sha256(envelope_id.encode("utf-8")).hexdigest()
    return (
        get_processed_envelope_dir() / f"{digest}.json"
        if processed_dir is None
        else processed_dir / f"{digest}.json"
    )
