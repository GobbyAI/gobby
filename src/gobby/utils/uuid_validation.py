"""UUID validation helpers."""

from __future__ import annotations

import uuid


def is_full_uuid(value: str | None) -> bool:
    """Return whether a value is a 36-character UUID string."""
    if value is None or len(value) != 36:
        return False
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return True
