"""Canonical task-reference formatting for plan validation."""

from __future__ import annotations


def normalize_task_ref(ref: str) -> str:
    """Normalize a bare numeric task reference to its canonical ``#N`` form."""
    stripped = ref.strip()
    if stripped.isdecimal():
        return f"#{stripped}"
    return stripped


__all__ = ["normalize_task_ref"]
