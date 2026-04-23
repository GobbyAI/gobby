"""Shared helpers for YAML-based agent wiring tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _field(entry: object, name: str) -> object | None:
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)


def find_step(steps: Iterable[Any], name: str) -> Any | None:
    return next((step for step in steps if getattr(step, "name", None) == name), None)
