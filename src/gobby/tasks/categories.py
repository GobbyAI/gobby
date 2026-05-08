"""Task category constants shared outside storage."""

from __future__ import annotations

# Categories whose manifest entries may opt into deterministic TDD wrapping.
TDD_ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({"code", "config"})

__all__ = ["TDD_ELIGIBLE_CATEGORIES"]
