"""Task category constants shared outside storage."""

from __future__ import annotations

# Categories whose manifest entries may opt into deterministic TDD wrapping.
TDD_ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({"code", "config"})

# Categories eligible for automated leaf creation during task expansion.
AUTOMATED_LEAF_CATEGORIES: frozenset[str] = frozenset(
    {"code", "config", "docs", "planning", "refactor", "research", "test"}
)

__all__ = ["AUTOMATED_LEAF_CATEGORIES", "TDD_ELIGIBLE_CATEGORIES"]
