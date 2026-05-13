"""Small SQL string helpers for dynamically sized bound-parameter lists."""

from __future__ import annotations


def sql_placeholders(count: int, separator: str = ",") -> str:
    """Return a placeholder list for bound SQL parameters."""

    if count < 1:
        raise ValueError("count must be greater than or equal to 1")
    return separator.join("?" for _ in range(count))


__all__ = ["sql_placeholders"]
