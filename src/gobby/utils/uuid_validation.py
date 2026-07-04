"""UUID validation helpers."""

from __future__ import annotations

import uuid


def parse_uuid_reference(value: object) -> uuid.UUID | None:
    """Parse a UUID-like reference without raising on invalid input."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def is_full_uuid(value: str | None) -> bool:
    """Return whether a value is a 36-character UUID string."""
    if value is None or len(value) != 36:
        return False
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return True
