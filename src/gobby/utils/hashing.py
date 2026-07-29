"""Hash validation helpers shared across Gobby domains."""

from __future__ import annotations

from typing import TypeGuard


def is_sha256(value: object) -> TypeGuard[str]:
    """Return whether value is a canonical lowercase SHA-256 hex digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
