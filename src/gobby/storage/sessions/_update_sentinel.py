"""Sentinel helpers for nullable session updates."""

from __future__ import annotations

from typing import TypeGuard


class UnsetType:
    """Represent an omitted update separately from an explicit ``None``."""

    __slots__ = ()


UNSET = UnsetType()


def is_set[T](value: T | UnsetType) -> TypeGuard[T]:
    """Return whether a nullable update value was explicitly supplied."""
    return not isinstance(value, UnsetType)
