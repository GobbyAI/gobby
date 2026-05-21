"""Task category constants shared outside storage."""

from __future__ import annotations

# Categories whose manifest entries may opt into deterministic TDD wrapping.
TDD_ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({"code", "config"})

# Categories that expansion may emit as executable leaves. These all start at
# development in build stage manifests.
DEVELOPMENT_FORWARD_LEAF_CATEGORIES: frozenset[str] = frozenset(
    {"code", "config", "docs", "refactor", "test"}
)

# Categories the build lifecycle can automate as direct leaf targets. Expansion
# output is intentionally narrower; use DEVELOPMENT_FORWARD_LEAF_CATEGORIES.
AUTOMATED_LEAF_CATEGORIES: frozenset[str] = frozenset(
    DEVELOPMENT_FORWARD_LEAF_CATEGORIES | {"planning", "research"}
)

__all__ = [
    "AUTOMATED_LEAF_CATEGORIES",
    "DEVELOPMENT_FORWARD_LEAF_CATEGORIES",
    "TDD_ELIGIBLE_CATEGORIES",
]
