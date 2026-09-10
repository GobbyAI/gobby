"""Canonical task-reference formatting for plan validation."""

from __future__ import annotations

import re

_NUMERIC_TASK_REF = re.compile(r"#\d+")


def normalize_task_ref(ref: str) -> str:
    """Normalize a bare numeric task reference to its canonical ``#N`` form."""
    stripped = ref.strip()
    if stripped.isdecimal():
        return f"#{stripped}"
    return stripped


def is_placeholder_task_ref(ref: str) -> bool:
    """True when a deferral ``task_ref`` names no task yet (any non-``#N`` value)."""
    return _NUMERIC_TASK_REF.fullmatch(normalize_task_ref(ref)) is None


__all__ = ["is_placeholder_task_ref", "normalize_task_ref"]
